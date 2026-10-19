"""detach — start a command in its own session, so it outlives the chat that started it.

A command a chat starts in the background belongs to the chat's session and process group. When the
chat ends, or its shell is closed, the command is ended with it, and a long job (a test run, a
release) stops half done. macOS has no `setsid` command, and `nohup cmd &` only ignores the hangup
signal: the command still shares the chat's group, so a kill of that group ends it too.

`detach` starts the command as the leader of a new session, with stdin from /dev/null and stdout and
stderr appended to a log file, and returns at once with its pid, which is also its session and
group id. To stop it later, signal that group: `kill -TERM -<pid>`.

    claude-ops detach --log FILE [--cwd DIR] -- COMMAND [ARGS...]    # prints the pid
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence


def detach(argv: Sequence[str], log: str, *, cwd: str | None = None,
           env: Mapping[str, str] | None = None) -> int:
    """Start `argv` in a new session with its output appended to `log`; return its pid.

    The pid is also the new session's id and its process group's id. Raises FileNotFoundError when
    the program does not exist and ValueError for an empty command, before anything starts."""
    if not argv:
        raise ValueError("no command to start")
    folder = os.path.dirname(os.path.abspath(log))
    os.makedirs(folder, exist_ok=True)
    with open(log, "ab") as out:
        proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                cwd=cwd, env=None if env is None else dict(env),
                                start_new_session=True, close_fds=True)
    return proc.pid


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops detach",
                                description="start a command in its own session; print its pid")
    p.add_argument("--log", required=True, help="file the command's output is appended to")
    p.add_argument("--cwd", help="folder to start the command in")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")
    a = p.parse_args(argv)
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    try:
        pid = detach(cmd, a.log, cwd=a.cwd)
    except (ValueError, OSError) as e:
        print(f"claude-ops detach: {e}", file=sys.stderr)
        return 2
    print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
