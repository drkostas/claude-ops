"""Refuse a command that signals many processes at once (a PreToolUse hook, on Bash).

One mistyped pattern or one stray `-1` can end every process a session is allowed to end, and that
includes the ones the machine's own remote access depends on (the network, the SSH daemon, the screen
sharing server). Nothing can be done from the outside once they are gone. This hook refuses the wide
shapes and has no approval path, because a person who is away cannot give one:

- `kill -1`, `kill 0`, `kill 1`, a negative pid (a process group) unless a process of that group
  descends from the Claude Code process running the hook (a background task the session started),
  and a negative pid built from a variable (only `-$!` is let through; write the group's number);
- `killall`, `killall5`, `skill`, `pkill -u`/`-U` (a whole user) and `pkill -v` (everything else);
- a pattern kill (`pkill`, or `pgrep`, `ps` or `pidof` feeding `kill` through `$(...)`, backticks or
  `xargs`) whose pattern matches a protected process, or matches more than a few processes. The
  matching processes are read with `pgrep`, and a pattern that is read as a regular expression is also
  tried against the protected names, labels and paths, so a pattern that would reach one is refused
  even when it is not running;
- any `kill` with more than a few targets.

It lets through what is narrow: `kill <pid>`, `kill -TERM $!`, a few literal pids, and
`lsof -ti:<port> | xargs kill`. The programs `bash -c`, `sh -c`, `zsh -c` and `eval` are read inside.
The refusal names the safe way. The pids themselves are checked by `claude_ops.killguard`, which a
tool can call before it signals anything.

The protected processes are configuration, in a JSON file (`--config`, else the environment variable
`CLAUDE_OPS_WIDE_KILL_CONFIG`, else `~/.config/claude-ops/wide-kill.json`):

    {
      "protected": {
        "names": ["sshd", "tailscaled"],
        "labels": ["com.example.tunnel"],
        "paths": ["/usr/sbin/sshd"]
      },
      "max_targets": 3,
      "message": "Anything else you want the refusal to say."
    }

Without a file the structural rules still apply and nothing is protected by name.

    python3 -P -m claude_ops.hooks.wide_kill [--config FILE]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable

from .. import killguard
from . import commandline

DEFAULT_CONFIG = "~/.config/claude-ops/wide-kill.json"
SAFE_WAY = ("End one process you started by its pid (`kill <pid>`), or the server on a port with "
            "`lsof -ti:<port> | xargs kill`.")

#: programs that signal many processes by their nature
ALWAYS_WIDE = {"killall", "killall5", "skill", "slay"}
#: options of `xargs` that take a value
XARGS_VALUE = {"-n", "-L", "-l", "-I", "-i", "-P", "-s", "-d", "-E", "-e", "-a", "-J", "-R", "-S",
               "--max-args", "--max-procs", "--delimiter", "--max-lines", "--max-chars", "--arg-file"}
#: `pgrep`/`pkill` options that take a value, and the ones that select by user
PGREP_VALUE = {"-u", "-U", "-P", "-g", "-G", "-s", "-t", "-F", "-c", "--uid", "--euid", "--parent",
               "--pgroup", "--group", "--session", "--terminal", "--pidfile", "--ns", "--nslist"}
PGREP_USER = {"-u", "-U", "--uid", "--euid"}
#: a signal given to `pkill` (`-9`, `-TERM`, `-HUP`)
PKILL_SIGNAL = re.compile(r"-(?:\d+|[A-Z]{3,}\d*)")
MAX_DEPTH = 3
RESOLVE_TIMEOUT_S = 3


def load(path: str | None) -> dict:
    path = path or os.environ.get("CLAUDE_OPS_WIDE_KILL_CONFIG") or DEFAULT_CONFIG
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def protected(cfg: dict) -> dict[str, list[str]]:
    p = cfg.get("protected") if isinstance(cfg.get("protected"), dict) else {}
    return {k: [str(x) for x in (p.get(k) or []) if str(x)] for k in ("names", "labels", "paths")}


def max_targets(cfg: dict) -> int:
    try:
        return max(1, int(cfg.get("max_targets", killguard.MAX_TARGETS)))
    except (TypeError, ValueError):
        return killguard.MAX_TARGETS


def processes(args: list[str]) -> list[str] | None:
    """The processes `pgrep args` lists (a line each, after the pid), or None when it cannot be read.
    Only this hook's own process and its parent are left out (`pgrep` never lists itself)."""
    flag = "-l" if sys.platform == "darwin" else "-a"
    try:
        out = subprocess.run(["pgrep", flag, *args], capture_output=True, text=True,
                             timeout=RESOLVE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 1):
        return None
    mine = {str(os.getpid()), str(os.getppid())}
    lines = []
    for line in out.stdout.splitlines():
        pid, _, rest = line.strip().partition(" ")
        if pid in mine:
            continue
        lines.append(rest)
    return lines


Resolver = Callable[[list[str]], "list[str] | None"]


def names_a_protected(text: str, prot: dict[str, list[str]]) -> str | None:
    """The protected entry a process line (a name, or a whole command line) names, or None."""
    for path in prot["paths"]:
        if path in text:
            return path
    for label in prot["labels"]:
        if label in text:
            return label
    for name in prot["names"]:
        if re.search(r"(?:^|/)" + re.escape(name) + r"(?:\s|$)", text):
            return name
    return None


def pattern_reaches(pattern: str, full: bool, exact: bool, fold: bool, prot: dict[str, list[str]]) -> str | None:
    """The protected entry a `pgrep`/`pkill` pattern would match, tried as a regular expression
    against the names (and, with -f, the labels and paths too), or None."""
    flags = re.IGNORECASE if fold else 0
    try:
        rx = re.compile(f"^(?:{pattern})$" if exact else pattern, flags)
    except re.error:
        return None
    for name in prot["names"]:
        if rx.search(name):
            return name
    if full:
        for entry in prot["labels"] + prot["paths"]:
            if rx.search(entry):
                return entry
    return None


def pgrep_args(args: list[str], signal_too: bool) -> tuple[list[str], str | None, bool, bool, bool, str | None]:
    """(the selection options, the pattern, -f, -x, -i, a reason this is wide) of a pgrep/pkill line."""
    keep: list[str] = []
    pattern = None
    full = exact = fold = False
    why = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in PGREP_USER or a.startswith(("--uid=", "--euid=")):
            why = "a whole user's processes"
        if a == "-v" or a == "--inverse":
            why = "every process except the ones matching"
        if signal_too and PKILL_SIGNAL.fullmatch(a):
            i += 1
            continue
        if signal_too and a == "--signal":
            i += 2
            continue
        if a.startswith("--"):
            keep.append(a)
            if a in PGREP_VALUE and i + 1 < len(args):
                keep.append(args[i + 1])
                i += 1
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            letters = a[1:]
            full = full or "f" in letters
            exact = exact or "x" in letters
            fold = fold or "i" in letters
            keep.append(a)
            if a in PGREP_VALUE and i + 1 < len(args):
                keep.append(args[i + 1])
                i += 1
            i += 1
            continue
        pattern = a
        keep.append(a)
        i += 1
    return keep, pattern, full, exact, fold, why


def pattern_kill(args: list[str], what: str, cfg: dict, resolve: Resolver, signal_too: bool,
                 first_only: bool = False) -> str | None:
    keep, pattern, full, exact, fold, why = pgrep_args(args, signal_too)
    if why:
        return f"`{what}` here selects {why}"
    prot = protected(cfg)
    if pattern is not None:
        hit = pattern_reaches(pattern, full, exact, fold, prot)
        if hit:
            return f"the pattern {pattern!r} matches the protected process {hit!r}"
    lines = None if any("__sub" in k for k in keep) else resolve(keep)
    if lines is None:
        return None
    for line in lines:
        hit = names_a_protected(line, prot)
        if hit:
            return f"the pattern {pattern!r} would reach the protected process {hit!r}" if pattern else \
                f"`{what}` would reach the protected process {hit!r}"
    if len(lines) > max_targets(cfg) and not first_only:
        return f"`{what}` would signal {len(lines)} processes, more than the {max_targets(cfg)} one command may"
    return None


def kill_targets(args: list[str], stdin: bool = False) -> list[str]:
    """The target words of a `kill` command line (what is left after its signal and options). Behind
    `xargs` the pids come on standard input, so a lone `-9` is a signal there."""
    targets: list[str] = []
    signal_seen = False
    after_options = False
    i = 0
    while i < len(args):
        a = args[i]
        if after_options:
            targets.append(a)
        elif a == "--":
            after_options = True
        elif a in ("-s", "-n"):
            signal_seen = True
            i += 1
        elif a in ("-l", "-L", "--list", "--table"):
            return []
        elif re.fullmatch(r"-\d+", a):
            if signal_seen or (i + 1 >= len(args) and not stdin):
                targets.append(a)
            else:
                signal_seen = True
        elif a.startswith("-") and len(a) > 1 and not a.startswith("-$"):
            signal_seen = True
        else:
            targets.append(a)
        i += 1
    return targets


def session_groups() -> set[int]:
    """The process groups this session started (read once, when a command names a group)."""
    table = killguard.process_table()
    return killguard.started_groups(table) if table else set()


GROUP_HINT = "write the group's number; a group this session started is allowed"


def check_kill(args: list[str], cfg: dict, stdin: bool = False, owns: Callable[[], set[int]] = session_groups) -> str | None:
    targets = kill_targets(args, stdin)
    literal: list[int] = []
    for t in targets:
        if re.fullmatch(r"-?\d+", t):
            literal.append(int(t))
        elif t.startswith("-") and "$" in t and t != "-$!":
            return f"`kill {t}` signals a process group this command did not start ({GROUP_HINT})"
    own = owns() if any(p < -1 for p in literal) else set()
    why = killguard.check(literal, own_groups=own, max_targets=max_targets(cfg))
    if why:
        return why
    if len(targets) > max_targets(cfg):
        return f"{len(targets)} targets is more than the {max_targets(cfg)} one command may signal"
    return None


def grep_patterns(steps: list[list[str]]) -> tuple[list[str], bool]:
    """The patterns the `grep` steps of a pipeline select with (an inverted one selects nothing), and
    whether a step keeps only the first line."""
    patterns: list[str] = []
    first_only = False
    for words in steps:
        words = commandline.unwrap(words)
        if not words:
            continue
        prog, args = os.path.basename(words[0]), words[1:]
        if prog == "rtk" and args[:1] == ["proxy"]:
            prog, args = os.path.basename(args[1]) if len(args) > 1 else "", args[2:]
        if prog in ("grep", "egrep", "rg", "pgrep") and prog != "pgrep":
            if any(a in ("-v", "--invert-match") or (a.startswith("-") and not a.startswith("--") and "v" in a[1:]
                                                       and "e" not in a[1:]) for a in args):
                continue
            plain = [a for a in args if not a.startswith("-")]
            if plain:
                patterns.append(plain[0])
        elif prog == "head" and re.search(r"(?:^| )-(?:n ?)?1(?: |$)", " ".join(args)):
            first_only = True
    return patterns, first_only


def producers(steps: list[list[str]], cfg: dict, resolve: Resolver, strict: bool = True, direct: bool = True) -> str | None:
    """Why the pipeline (a list of commands, the first one producing the pids) selects too much. With `strict`, which is when the
    output goes straight to the kill (or into a variable the kill uses), more than a few matches are refused too, and
    with `direct` (the output is the kill's own target) so is a process listing with no grep pattern; otherwise only a protected process is."""
    first_only = grep_patterns(steps)[1] or not strict
    for words in steps:
        words = commandline.unwrap(words)
        if not words:
            continue
        prog, args = os.path.basename(words[0]), words[1:]
        if prog == "pgrep":
            why = pattern_kill(args, "pgrep", cfg, resolve, signal_too=False, first_only=first_only)
        elif prog == "pidof":
            why = None
            for n in [a for a in args if not a.startswith("-")]:
                why = why or pattern_kill(["-x", n], "pidof", cfg, resolve, signal_too=False,
                                                    first_only=first_only)
        elif prog == "ps":
            if re.search(r"(?:^|\s)-?p\b|(?:^|\s)--pid\b", " ".join(args)):
                continue
            patterns = grep_patterns(steps)[0]
            if not patterns:
                if direct:
                    return "a process listing feeding kill is a pattern kill"
                continue
            why = None
            for pat in patterns:
                why = why or pattern_kill(["-f", pat], "the process listing", cfg, resolve, signal_too=False,
                                          first_only=first_only)
        elif prog == "lsof":
            why = "`lsof -u` selects everything a user has open" \
                if any(a == "-u" or (a.startswith("-u") and len(a) > 2) for a in args) else None
        else:
            continue
        if why:
            return why
    return None


def split_substitutions(command: str) -> tuple[str, list[str]]:
    """The command with each `$(...)` and backtick substitution replaced by a marker word, and the
    text of each. Single-quoted text and here-document bodies are data and are never read."""
    command = commandline.without_heredoc_bodies(command)
    out: list[str] = []
    subs: list[str] = []
    n = len(command)
    i = 0
    single = double = False
    while i < n:
        c = command[i]
        if single:
            single = c != "'"
        elif c == "\\" and i + 1 < n:
            out.append(c)
            i += 1
            c = command[i]
        elif c == "'" and not double:
            single = True
        elif c == '"':
            double = not double
        elif command.startswith("$(", i) and not command.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (command[j] == "(") - (command[j] == ")")
                j += 1
            if depth == 0:
                subs.append(command[i + 2:j - 1])
                out.append(f"__sub{len(subs) - 1}__")
                i = j
                continue
        elif c == "`":
            j = command.find("`", i + 1)
            if j > 0:
                subs.append(command[i + 1:j])
                out.append(f"__sub{len(subs) - 1}__")
                i = j + 1
                continue
        out.append(c)
        i += 1
    return "".join(out), subs


def shell_script(words: list[str]) -> str | None:
    """The script given to `bash -c`, `sh -c`, `zsh -c` or `eval`, or None."""
    if not words:
        return None
    prog = os.path.basename(words[0])
    if prog == "eval":
        return " ".join(words[1:])
    if prog in ("bash", "sh", "zsh", "dash", "ksh"):
        for i, w in enumerate(words[1:], 1):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", w) and i + 1 < len(words):
                return words[i + 1]
            if not w.startswith("-"):
                return None
    return None


def check_command(command: str, cfg: dict, resolve: Resolver, depth: int = 0,
                  owns: Callable[[], set[int]] = session_groups) -> str | None:
    if "kill" not in command or depth > MAX_DEPTH:
        return None
    text, subs = split_substitutions(command)
    steps = commandline.parse(text)
    kills = False
    direct: set[int] = set()
    var_target = False
    for i, step in enumerate(steps):
        words = commandline.unwrap(step["words"])
        if not words:
            continue
        prog, args = os.path.basename(words[0]), words[1:]
        if prog in ALWAYS_WIDE:
            return f"`{prog}` signals every process of that name"
        if prog == "kill":
            kills = True
            why = check_kill(args, cfg, owns=owns)
            direct.update(int(m) for t in args for m in re.findall(r"__sub(\d+)__", t))
            var_target = var_target or any("$" in t and t != "-$!" for t in kill_targets(args))
        elif prog == "pkill":
            why = pattern_kill(args, "pkill", cfg, resolve, signal_too=True)
        elif prog == "xargs":
            why = None
            j = 1
            while j < len(words) and words[j].startswith("-"):
                j += 2 if words[j] in XARGS_VALUE else 1
            run = commandline.unwrap(words[j:])
            if run and os.path.basename(run[0]) in ALWAYS_WIDE:
                why = f"`xargs {os.path.basename(run[0])}` signals every process of that name"
            elif run and os.path.basename(run[0]) == "kill":
                kills = True
                k = i
                while k > 0 and steps[k]["piped"]:
                    k -= 1
                why = producers([st["words"] for st in steps[k:i]], cfg, resolve) if k < i else None
                if why is None:
                    why = check_kill(run[1:], cfg, stdin=True, owns=owns)
        else:
            script = shell_script(words)
            why = check_command(script, cfg, resolve, depth + 1, owns) if script else None
        if why:
            return why
    for n, sub in enumerate(subs):
        why = check_command(sub, cfg, resolve, depth + 1, owns)
        if why:
            return why
        if kills:
            why = producers([st["words"] for st in commandline.parse(split_substitutions(sub)[0])],
                             cfg, resolve, strict=n in direct or var_target, direct=n in direct)
            if why:
                return why
    return None


def check(payload: dict, cfg: dict, resolve: Resolver = processes,
          owns: Callable[[], set[int]] = session_groups) -> str | None:
    if payload.get("tool_name", "") != "Bash":
        return None
    return check_command(str((payload.get("tool_input") or {}).get("command", "")), cfg, resolve, owns=owns)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = None
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
    try:
        cfg = load(path)
        why = check(json.load(sys.stdin), cfg)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        extra = f" {cfg['message']}" if isinstance(cfg.get("message"), str) and cfg["message"] else ""
        print(f"Blocked: {why}. This command could end processes that keep the machine reachable, "
              f"and there is no approval for it. {SAFE_WAY}{extra}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
