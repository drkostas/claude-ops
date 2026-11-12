"""Rewrite a shell command to its rtk form before it runs (a PreToolUse hook on Bash).

rtk is a command-line proxy: `rtk git status`, `rtk grep foo src` or `rtk ls -la` run the usual
command and print a shorter form of its output, which costs a session fewer tokens to read. This hook
puts `rtk` in front of the commands it knows, so a session gets the short output without having to
remember the proxy. It does in the hook chain's own interpreter what a shell hook would do by starting
jq and sed several times for every command.

What it changes, and what it leaves alone:

- Only the first line of a command is judged and changed. A rule that looked at every line once
  rewrote a flagged `grep -oE` on line two (`F=...`, then a newline, then the grep), which rtk then
  refused, so later lines always run as written.
- A first line that already runs rtk (`rtk ...`, or a path ending in `/rtk `) is left alone, and so is
  any command with a here-document (`<<`), whose body is data the rules must not read.
- `grep` and `rg` are rewritten only in the plain `grep PATTERN [PATH]` form: no word of the first
  simple command may start with `-`. `rtk grep` takes `PATTERN [PATH] [EXTRA_ARGS]` and has its own
  `-c -l -m -t -v -u -h` with other meanings, and rejects most of the others, so a flagged grep goes
  to grep itself. When PATH is an existing folder and rtk is version 1 or later, `-r` is added, because
  rtk 1.x reads a folder path as a file ("Is a directory").
- `gh` is rewritten only for reads (`pr`, `issue` and `run` with `view`, `list`, `checks`, `status` or
  `diff`), and not with `--json`, `--jq` or `--template`, whose exact output a script needs. Writes
  (create, edit, comment, merge and the rest) go to gh itself, since some rtk versions misread a
  write's flags.
- `git commit` with a message file (`-F`, `--file`) goes to git itself: some rtk versions refused
  `-F` with a usage error and committed nothing.
- A rule that matches but would leave the command unchanged (two spaces after `git`, for example)
  gives no answer, so nothing is sent for a command that is not rewritten.

The answer is an `updatedInput` with the changed command. It gives no permission of its own unless
`--allow` is passed, in which case it also answers "allow" (as rtk's own hook does), and the
rewritten command then runs without the permission rules being asked. With no `rtk` on PATH (or the
binary `--rtk` names), or on anything unexpected, it gives no answer and the command runs as written:
a broken hook must never stop work.

    python3 -P -m claude_ops.hooks.rtk_rewrite [--allow] [--rtk BIN]
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Callable

#: the characters POSIX's [[:space:]] matches
_SP = r"[ \t\n\v\f\r]"
_END = rf"(?:{_SP}|\Z)"


def _word(*words: str) -> str:
    """`^w1 SP+ w2 ... (SP|end)`: the first line starts with these words."""
    return "^" + f"{_SP}+".join(words) + _END


#: (what the first line must match, the pattern replaced on it, its replacement), first match wins
RULES: list[tuple[str, str, str]] = [
    *[(_word("git", sub), f"^git {sub}", f"rtk git {sub}")
      for sub in ("status", "diff", "log", "add", "commit", "push", "pull", "branch", "fetch", "stash", "show")],
    (_word("gh", "(?:pr|issue|run)", "(?:view|list|checks|status|diff)"), "^gh ", "rtk gh "),
    *[(_word("cargo", sub), f"^cargo {sub}", f"rtk cargo {sub}") for sub in ("test", "build", "clippy")],
    (rf"^cat{_SP}+", "^cat ", "rtk read "),
    (rf"^(?:rg|grep){_SP}+", "^(?:rg|grep) ", "rtk grep "),
    (_word("ls"), "^ls", "rtk ls"),
    (rf"^(?:pnpm{_SP}+)?vitest{_END}", "^(?:pnpm )?vitest", "rtk vitest run"),
    (_word("pnpm", "test"), "^pnpm test", "rtk vitest run"),
    (_word("pnpm", "tsc"), "^pnpm tsc", "rtk tsc"),
    (rf"^(?:npx{_SP}+)?tsc{_END}", "^(?:npx )?tsc", "rtk tsc"),
    (_word("pnpm", "lint"), "^pnpm lint", "rtk lint"),
    (rf"^(?:npx{_SP}+)?eslint{_END}", "^(?:npx )?eslint", "rtk lint"),
    (rf"^(?:npx{_SP}+)?prettier{_END}", "^(?:npx )?prettier", "rtk prettier"),
    (rf"^(?:npx{_SP}+)?playwright{_END}", "^(?:npx )?playwright", "rtk playwright"),
    (_word("pnpm", "playwright"), "^pnpm playwright", "rtk playwright"),
    (rf"^(?:npx{_SP}+)?prisma{_END}", "^(?:npx )?prisma", "rtk prisma"),
    (_word("docker", "(?:ps|images|logs)"), "^docker ", "rtk docker "),
    (_word("kubectl", "(?:get|logs)"), "^kubectl ", "rtk kubectl "),
    (rf"^curl{_SP}+", "^curl ", "rtk curl "),
    (_word("pnpm", "(?:list|ls|outdated)"), "^pnpm ", "rtk pnpm "),
    (_word("pytest"), "^pytest", "rtk pytest"),
    (_word("python", "-m", "pytest"), "^python -m pytest", "rtk pytest"),
    (_word("ruff", "(?:check|format)"), "^ruff ", "rtk ruff "),
    (_word("pip", "(?:list|outdated|install|show)"), "^pip ", "rtk pip "),
    (_word("uv", "pip", "(?:list|outdated|install|show)"), "^uv pip ", "rtk pip "),
    *[(_word("go", sub), f"^go {sub}", f"rtk go {sub}") for sub in ("test", "build", "vet")],
    (_word("golangci-lint"), "^golangci-lint", "rtk golangci-lint"),
]
_RULES = [(re.compile(m), re.compile(p), r) for m, p, r in RULES]
_GH_EXACT = re.compile(rf"(?:^|{_SP})--(?:json|jq|template)(?:[ \t\n\v\f\r=]|\Z)")


def words(line: str) -> list[str]:
    """The line split on spaces and tabs, as `read -a` splits it (quotes are not read)."""
    return [w for w in re.split(r"[ \t]+", line) if w]


def _first_command(line: str) -> list[str]:
    """The words of the first simple command: up to a word that starts with a pipe, ; or &."""
    out = []
    for w in words(line):
        if w[0] in "|;&":
            break
        out.append(w)
    return out


def has_option(line: str) -> bool:
    """A word of the first simple command starts with "-" (a quoted argument that does counts too,
    which only skips a rewrite)."""
    return any(len(w) > 1 and w[0] == "-" for w in _first_command(line))


def takes_message_file(line: str) -> bool:
    """`git commit` here is given its message in a file (-F, -aF, -Ffile, --file, --file=...)."""
    return any(w.startswith("--file") or re.match(r"-[A-Za-z]*F", w) for w in _first_command(line)[2:])


def path_is_folder(line: str, cwd: str) -> bool:
    """The word after the pattern (`grep PATTERN PATH`) is an existing folder."""
    w = words(line)
    if len(w) < 3:
        return False
    path = w[2]
    if path[0] in "|;&>" or (len(path) > 1 and path[0] in "0123456789" and path[1] == ">"):
        return False
    path = path[1:] if path[0] in "\"'" else path
    path = path[:-1] if path and path[-1] in "\"'" else path
    if not path.startswith("/") and cwd:
        path = f"{cwd}/{path}"
    return bool(path) and os.path.isdir(path)


def rtk_major(rtk: str = "rtk") -> int | None:
    """The major version `rtk --version` prints ("rtk 1.1.0" is 1); None when it cannot be read."""
    try:
        out = subprocess.run([rtk, "--version"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    text = "\n".join(re.sub(r"^rtk ([0-9]+)\..*", r"\1", ln) for ln in out.split("\n")).strip()
    return int(text) if text.isdigit() else None


def rewrite(command: str, cwd: str = "", major: Callable[[], int | None] = rtk_major) -> str | None:
    """The command with its rtk form on the first line, or None when it is left as written.

    `major` gives rtk's major version, and is asked only for a plain grep on a folder."""
    if not isinstance(command, str):
        return None
    command = command.rstrip("\n")
    first = command.split("\n", 1)[0]
    if not command or first.startswith("rtk ") or "/rtk " in first or "<<" in command:
        return None
    for match, pattern, replacement in _RULES:
        if not match.search(first):
            continue
        if pattern.pattern == "^gh " and _GH_EXACT.search(first):
            return None
        if replacement == "rtk git commit" and takes_message_file(first):
            return None
        if replacement == "rtk grep ":
            if has_option(first):
                return None
            if path_is_folder(first, cwd) and (major() or 0) >= 1:
                replacement = "rtk grep -r "
        changed = pattern.sub(replacement, first, count=1)
        if changed == first:
            return None
        rest = command[len(first):]
        return (changed + rest).rstrip("\n")
    return None


def answer(payload: dict, allow: bool = False, rtk: str = "rtk",
           major: Callable[[], int | None] | None = None) -> dict | None:
    """The hook's answer for this tool call, or None when the command runs as written."""
    if payload.get("tool_name", "Bash") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or shutil.which(rtk) is None:
        return None
    new = rewrite(tool_input.get("command"), str(payload.get("cwd") or ""),
                  major or (lambda: rtk_major(rtk)))
    if new is None:
        return None
    spec: dict = {"hookEventName": "PreToolUse"}
    if allow:
        spec["permissionDecision"] = "allow"
        spec["permissionDecisionReason"] = "RTK auto-rewrite"
    spec["updatedInput"] = {**tool_input, "command": new}
    return {"hookSpecificOutput": spec}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    allow = "--allow" in args
    rtk = args[args.index("--rtk") + 1] if "--rtk" in args[:-1] else "rtk"
    try:
        payload = json.loads(sys.stdin.read())
        out = answer(payload, allow=allow, rtk=rtk) if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if out is not None:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
