"""detach — start a command in its own session, so it outlives the chat that started it.

A command a chat starts in the background belongs to the chat's session and process group. When the
chat ends, or its shell is closed, the command is ended with it, and a long job (a test run, a
release) stops half done. macOS has no `setsid` command, and `nohup cmd &` only ignores the hangup
signal: the command still shares the chat's group, so a kill of that group ends it too. A job started
with `nohup ... & disown` from a chat was ended by SIGTERM (exit 143) when the chat was restarted.

`detach` starts a small supervisor (detach_supervisor.py, run by its path) as the leader of a new
session, with stdin from /dev/null, and returns at once with its pid, which is also the session's
and the group's id. The supervisor runs the command in that session with its stdout and stderr
appended to a log file, waits for it, and then appends one end line to the same log:

    detach end: exit 0 at 2026-11-14T13:30:01+0200 (pid 4242)
    detach end: signal SIGTERM (15) at 2026-11-14T13:31:12+0200 (pid 4242)

So a waiter waits for the job by its log (`until grep -q '^detach end:' LOG; do sleep 5; done`)
instead of a pid, which a restarted chat no longer knows. The line is written whatever ended the
command, a signal included: the supervisor passes SIGTERM, SIGINT and SIGHUP on to the command and
writes the line once it has ended. Only a SIGKILL of the supervisor itself leaves no line.

To stop the job later, signal the group: `kill -TERM -<pid>`. The group is recorded for the session
that started it (killguard.recorded_groups), so the wide-kill hook lets that kill through.

    claude-ops detach [--log FILE] [--cwd DIR] -- COMMAND [ARGS...]    # prints the pid

Without `--log` the log is a new file in a logs folder of its own, named for the time and the
program (`detach-20261128T090500Z-pytest.log`), under `~/Library/Logs/claude-ops/` on macOS and
`$XDG_STATE_HOME/claude-ops/logs/` (or `~/.local/state/claude-ops/logs/`) elsewhere. The path is
printed on standard error, and standard output stays the pid. A program that reads the log later
(a wait, a check) needs a place it is allowed to read, and a default inside the usual logs folder is
one that needs no choosing.

A site that limits where its logs may be read from lists the folders in `CLAUDE_OPS_DETACH_LOG_ROOTS`
(folders separated by the path separator). A log outside every one of them is refused before
anything starts, and the refusal names the folders, so the wrong path fails now and not when
something later waits on a file it can never read. Unset, nothing is limited.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence

from . import killguard

#: what every end line starts with, at the start of a line of the log
END = "detach end:"

#: the supervisor, run by its path with `python -I`: a short line in ps, and nothing on its import
#: path but the standard library (detach_supervisor.py says why)
SUPERVISOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "detach_supervisor.py")


def _program(argv0: str, cwd: str | None, env: Mapping[str, str] | None) -> str | None:
    """The program `argv0` names as the supervisor will find it, or None when there is none."""
    if os.sep in argv0:
        path = argv0 if os.path.isabs(argv0) or not cwd else os.path.join(cwd, argv0)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    search = (env if env is not None else os.environ).get("PATH", os.defpath)
    return shutil.which(argv0, path=search)


#: folders a log may be in, separated by the path separator; unset means any folder
LOG_ROOTS_ENV = "CLAUDE_OPS_DETACH_LOG_ROOTS"


def default_log_dir(env: Mapping[str, str] | None = None) -> str:
    """The folder a log goes in when none is named (see the module text)."""
    env = os.environ if env is None else env
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Logs", "claude-ops")
    state = env.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "claude-ops", "logs")


def default_log(argv0: str, env: Mapping[str, str] | None = None) -> str:
    """A new log path for the program `argv0`, named for the time and the program."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in os.path.basename(argv0))[:40]
    return os.path.join(default_log_dir(env), f"detach-{stamp}-{name or 'job'}.log")


def check_log_root(log: str, env: Mapping[str, str] | None = None) -> None:
    """Raise ValueError when CLAUDE_OPS_DETACH_LOG_ROOTS lists folders and `log` is in none of them.

    The log is compared by its real path, so a link to a folder outside the list does not pass."""
    roots = [r for r in (env if env is not None else os.environ).get(LOG_ROOTS_ENV, "").split(os.pathsep) if r]
    if not roots:
        return
    where = os.path.realpath(os.path.dirname(os.path.abspath(log)))
    for r in roots:
        real = os.path.realpath(os.path.expanduser(r))
        if where == real or where.startswith(real.rstrip(os.sep) + os.sep):
            return
    raise ValueError(f"the log {log} is outside the folders a log may be in "
                     f"({', '.join(roots)}); name a log inside one of them")


def detach(argv: Sequence[str], log: str, *, cwd: str | None = None,
           env: Mapping[str, str] | None = None) -> int:
    """Start `argv` in a new session with its output appended to `log`, and an end line appended
    when it ends (see the module text); return the session's pid.

    The pid is the supervisor's, which is the new session's id and its process group's id. Raises
    FileNotFoundError when the program does not exist and ValueError for an empty command, before
    anything starts."""
    if not argv:
        raise ValueError("no command to start")
    if _program(argv[0], cwd, env) is None:
        raise FileNotFoundError(f"no such program: {argv[0]}")
    log = os.path.abspath(log)
    check_log_root(log)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "ab"):
        pass
    proc = subprocess.Popen([sys.executable, "-I", SUPERVISOR, log, *argv],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, cwd=cwd,
                            env=None if env is None else dict(env),
                            start_new_session=True, close_fds=True)
    # remember the group for this session, so a later kill of it is known to be the session's own
    # (killguard.recorded_groups); a failure here never stops the command
    try:
        killguard.record_group(proc.pid)
    except Exception:       # noqa: BLE001
        pass
    return proc.pid


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops detach",
                                description="start a command in its own session; print its pid. "
                                            f"A line starting '{END}' is appended to the log when "
                                            "the command ends, with its exit code or signal.")
    p.add_argument("--log", help="file the command's output is appended to (default, a new file in "
                                 "the package's logs folder, printed on standard error)")
    p.add_argument("--cwd", help="folder to start the command in")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")
    a = p.parse_args(argv)
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    log = a.log or (default_log(cmd[0]) if cmd else None)
    if log is None:
        print("claude-ops detach: no command to start", file=sys.stderr)
        return 2
    try:
        pid = detach(cmd, log, cwd=a.cwd)
    except (ValueError, OSError) as e:
        print(f"claude-ops detach: {e}", file=sys.stderr)
        return 2
    if not a.log:
        print(f"detach log {log}", file=sys.stderr)
    print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
