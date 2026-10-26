"""Refuse a pipe into a command that does not read its standard input (a PreToolUse hook).

Some programs are wrappers that take what they run as arguments and never look at their standard
input. Put one on the far side of a pipe and the shell is happy, the exit status is 0, and nothing
comes out. An agent reading that output has no way to tell it apart from a successful search that
found nothing, so the empty result becomes a fact: "there are no matches", stated with confidence
and wrong. The same happens to a here-document fed to such a wrapper, whose body is read by nobody.

`rtk proxy` is the default case. `rbw list | rtk proxy grep pypi` prints nothing at all, while
`rbw list > list.txt && grep pypi list.txt` prints two lines. A note in a conventions file does not
help, because the failure looks exactly like success; a refusal does.

This hook refuses (exit 2, with the reason sent back to the model):

- a simple command that a pipe feeds, whose program and leading words are one of the configured
  commands, with assignments and wrappers in front of it ignored (`cmd | VAR=1 env rtk proxy ...`)
  and the program matched by its file name, so an absolute path counts;
- a here-document given to one of them (`rtk proxy python3 - <<EOF`).

It does not refuse a pipe that one of them writes into (`rtk proxy git log | head` is fine, since
the wrapper's own output is piped onward), nor `||` or `&&` before one, nor a pipe inside a quoted
argument, which is the shape that works and the one to suggest: `rtk proxy bash -c 'cmd | grep x'`.
The command line is read by `commandline`, shared with the other hooks, so quoting, line
continuations and here-documents are read the way the shell reads them.

The commands are words matched against the start of a simple command, from `--command` (repeatable)
else from a JSON file:

    {
      "commands": ["rtk proxy"],
      "message": "Write the output to a file and search the file."
    }

The file is `--config`, else `CLAUDE_OPS_IGNORES_STDIN_CONFIG`, else
`~/.config/claude-ops/ignores-stdin.json`, and with neither the default `rtk proxy` is used. On
anything unexpected the call goes through: a broken hook must never stop work.

    python3 -P -m claude_ops.hooks.ignores_stdin [--command "rtk proxy" ...] [--config FILE]
"""
from __future__ import annotations

import json
import os
import sys

from . import commandline

#: the command this exists for, when nothing is configured
DEFAULT_COMMANDS = ("rtk proxy",)
DEFAULT_CONFIG = "~/.config/claude-ops/ignores-stdin.json"


def config(argv: list[str]) -> tuple[list[list[str]], str]:
    """(the commands that ignore their input, as word lists, the extra message)."""
    commands, path, i = [], None, 0
    while i < len(argv):
        if argv[i] == "--command" and i + 1 < len(argv):
            commands.append(argv[i + 1])
            i += 2
        elif argv[i] == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
            i += 2
        else:
            i += 1
    message = ""
    if not commands:
        path = path or os.environ.get("CLAUDE_OPS_IGNORES_STDIN_CONFIG") or DEFAULT_CONFIG
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                loaded = json.load(fh)
            commands = [str(c) for c in (loaded.get("commands") or [])]
            message = str(loaded.get("message") or "")
        except Exception:  # noqa: BLE001 - no file, or an unreadable one, leaves the default
            commands = []
    if not commands:
        commands = list(DEFAULT_COMMANDS)
    return [c.split() for c in commands if c.split()], message


def leading(words: list[str]) -> list[str]:
    """`words` without the assignments and wrappers in front of it, keeping the command itself.

    `commandline.unwrap` cannot serve here: it treats `rtk proxy` as one of the wrappers to strip,
    which is the very thing this hook has to see.
    """
    wrappers = getattr(commandline, "WRAPPERS", frozenset())
    i = 0
    while i < len(words):
        word = words[i]
        if word in wrappers or commandline.ASSIGNMENT.match(word):
            i += 1
        elif word == "env":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-u", "--unset", "-C", "--chdir") else 1
        else:
            break
    return words[i:]


def names(command: list[str]) -> list[str]:
    """A configured command as words, its program reduced to a file name for comparing."""
    return [os.path.basename(command[0]), *command[1:]] if command else []


def starts_with(words: list[str], command: list[str]) -> bool:
    """True when `words` runs `command`, whatever path or wrappers stand in front of it."""
    head = names(leading(words))
    return bool(command) and head[:len(command)] == command


def check_bash(command: str, commands: list[list[str]]) -> str | None:
    """Why this command line would lose its input, or None."""
    for step in commandline.parse(command):
        for wanted in commands:
            if not starts_with(step["words"], names(wanted)):
                continue
            said = " ".join(names(wanted))
            if step["piped"]:
                return f"a pipe into `{said}`"
            if step["heredoc"] is not None:
                return f"a here-document given to `{said}`"
    return None


def check(payload: dict, commands: list[list[str]]) -> str | None:
    if not commands or payload.get("tool_name", "") != "Bash":
        return None
    ti = payload.get("tool_input") or {}
    return check_bash(str(ti.get("command", "")), commands)


def main(argv: list[str] | None = None) -> int:
    try:
        commands, message = config(list(sys.argv[1:] if argv is None else argv))
        payload = json.load(sys.stdin)
        why = check(payload, commands)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}, which does not read its standard input, so this prints nothing at "
              "all and an empty result is indistinguishable from a search that found nothing. Put "
              "the pipeline inside the command instead (`rtk proxy bash -c 'cmd | grep x'`), or "
              "write the output to a file and read the file."
              + (f" {message}" if message else ""), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
