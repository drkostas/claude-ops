"""Refuse a command that would overwrite a file nothing can bring back (a PreToolUse hook).

Some programs write a file of their own choosing, with no path on the command line and no question
asked. When that file is hand-written and no repository tracks it, one such command is an
irreversible loss, and the loss is quiet: the file is still there, with other contents. A tool that
prints "run `<program> init` to update" is an instruction to do exactly that, so the most likely
reader of the warning is the one with the most to lose.

This hook refuses such a command before it runs (exit 2, with the reason sent back to the model). It
reads the Bash tool call's command line, finds every simple command on it (after `&&`, in a pipeline,
on a later line), and refuses when a rule's words stand at the start of one AND at least one of the
files that rule names exists and is not tracked by git. A file git tracks goes through, because a
checkout brings it back, so the rule narrows itself to the case that cannot be undone. A dated copy
beside the file is not history and does not count.

The rules are in a JSON file:

    {"rules": [
      {"command": "sometool init",
       "files": ["~/.config/sometool/NOTES.md", "~/.config/sometool/GUIDE.md"],
       "message": "Copy the file aside first, then run it under a throwaway HOME to read what it writes."}
    ]}

`command` is the words the command must begin with, the program matched by its base name, so
`/usr/local/bin/sometool init -g` and `sometool init --global` both match `sometool init` while
`sometool hook` and `sometool initialize` do not. Leading assignments and wrappers (`VAR=x`, `env`,
`nohup`, `time`, `sudo`, `nice`, `stdbuf`, `caffeinate`, `setsid`, `command`) are stepped over.
`files` are the paths that command writes, `~` being the home folder. `message` is added to the
refusal: say there how to proceed deliberately, since a refusal with no way through is one people
route around.

The file is `--config`, else `CLAUDE_OPS_NO_RESTORE_CONFIG`, else
`~/.config/claude-ops/no-restore.json`. With no rules nothing is refused, a rule that cannot be read
is skipped, and on anything unexpected the call goes through: a broken hook must never stop work.

    claude-ops hook no-restore        # reads the tool call on stdin
    python3 -P -m claude_ops.hooks.no_restore [--config FILE]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from . import commandline

DEFAULT_CONFIG = "~/.config/claude-ops/no-restore.json"
CONFIG_ENV = "CLAUDE_OPS_NO_RESTORE_CONFIG"
#: a word that may stand before the program of a command, with the flags it takes a value for
WRAPPERS = {"env": ("-u", "--unset", "-C", "--chdir"), "nohup": (), "time": (), "sudo": ("-u", "-g", "-U"),
            "nice": ("-n",), "ionice": ("-c", "-n"), "stdbuf": ("-i", "-o", "-e"), "caffeinate": ("-t",),
            "setsid": (), "command": (), "builtin": ()}
#: NAME=value in front of a command
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def rules(argv: list[str]) -> list[dict]:
    """The rules, from `--config`, the environment or the default file; [] when none is readable."""
    path = argv[argv.index("--config") + 1] if "--config" in argv[:-1] else os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    listed = got.get("rules") if isinstance(got, dict) else None
    for rule in listed if isinstance(listed, list) else []:
        if not isinstance(rule, dict):
            continue
        words, files = str(rule.get("command", "")).split(), rule.get("files")
        if words and isinstance(files, list) and files:
            out.append({"words": words, "files": [str(f) for f in files], "message": str(rule.get("message", ""))})
    return out


def program(words: list[str]) -> list[str]:
    """The command without the assignments and wrappers in front of it (`VAR=x env -u A sometool`)."""
    i = 0
    while i < len(words):
        name = os.path.basename(words[i])
        if ASSIGNMENT.match(words[i]):
            i += 1
        elif name in WRAPPERS:
            takes_value = WRAPPERS[name]
            i += 1
            while i < len(words) and words[i].startswith("-") and words[i] != "--":
                i += 2 if words[i] in takes_value else 1
            if i < len(words) and words[i] == "--":
                i += 1
        else:
            break
    return words[i:]


def begins_with(words: list[str], rule_words: list[str]) -> bool:
    """The rule's words stand at the start of this command, its program read by base name."""
    words = program(words)
    if len(words) < len(rule_words):
        return False
    return os.path.basename(words[0]) == rule_words[0] and words[1:len(rule_words)] == rule_words[1:]


def restorable(path: str) -> bool:
    """A copy of this file can be brought back, which here means git tracks it."""
    folder = os.path.dirname(path) or "."
    try:
        done = subprocess.run(["git", "-C", folder, "ls-files", "--error-unmatch", "--", os.path.basename(path)],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True                     # cannot tell, so do not refuse
    return done.returncode == 0


def at_risk(rule: dict) -> list[str]:
    """The rule's files that exist and that nothing can bring back."""
    out = []
    for name in rule["files"]:
        path = os.path.expanduser(name)
        if os.path.isfile(path) and not restorable(path):
            out.append(path)
    return out


def check(payload: dict, argv: list[str] | None = None) -> str | None:
    """Why this tool call is refused, or None."""
    if payload.get("tool_name") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command", ""))
    got = rules(list(argv or []))
    if not command or not got:
        return None
    for simple in commandline.parse(command):
        for rule in got:
            if not begins_with(simple["words"], rule["words"]):
                continue
            files = at_risk(rule)
            if not files:
                continue
            said = " ".join(rule["words"])
            more = f" (and {len(files) - 1} more)" if len(files) > 1 else ""
            why = (f"`{said}` writes {files[0]}{more} itself, and git does not track that file, so this "
                   "would overwrite a hand-written file nothing can bring back")
            return f"{why}. {rule['message']}" if rule["message"] else why
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        payload = json.load(sys.stdin)
        why = check(payload, args)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
