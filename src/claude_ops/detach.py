"""detach — start a command in its own session, so it outlives the chat that started it.

A command a chat starts in the background belongs to the chat's session and process group. When the
chat ends, or its shell is closed, the command is ended with it, and a long job (a test run, a
release) stops half done. macOS has no `setsid` command, and `nohup cmd &` only ignores the hangup
signal: the command still shares the chat's group, so a kill of that group ends it too. A job started
with `nohup ... & disown` from a chat was ended by SIGTERM (exit 143) when the chat was restarted.

`detach` starts a small supervisor as the leader of a new session, with stdin from /dev/null, and
returns at once with its pid, which is also the session's and the group's id. The supervisor runs
the command in that session with its stdout and stderr appended to a log file, waits for it, and then
appends one end line to the same log:

    detach end: exit 0 at 2026-11-14T13:30:01+0200 (pid 4242)
    detach end: signal SIGTERM (15) at 2026-11-14T13:31:12+0200 (pid 4242)

So a waiter waits for the job by its log (`until grep -q '^detach end:' LOG; do sleep 5; done`)
instead of a pid, which a restarted chat no longer knows. The line is written whatever ended the
command, a signal included: the supervisor passes SIGTERM, SIGINT and SIGHUP on to the command and
writes the line once it has ended. Only a SIGKILL of the supervisor itself leaves no line.

To stop the job later, signal the group: `kill -TERM -<pid>`. The group is recorded for the session
that started it (killguard.recorded_groups), so the wide-kill hook lets that kill through.

    claude-ops detach --log FILE [--cwd DIR] -- COMMAND [ARGS...]    # prints the pid
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence

from . import killguard

#: what every end line starts with, at the start of a line of the log
END = "detach end:"

#: the supervisor, run with `python -I -c`: it imports only the standard library, so it works with
#: whatever environment the command is given. argv is [log, command...].
SUPERVISOR = r'''
import os, signal, subprocess, sys, time
log, argv = sys.argv[1], sys.argv[2:]
child = None
def forward(sig, frame):
    if child is not None and child.poll() is None:
        try:
            child.send_signal(sig)
        except ProcessLookupError:
            pass
for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(s, forward)
with open(log, "ab", buffering=0) as out:
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
        rc = child.wait()
        what = "exit %d" % rc if rc >= 0 else "signal %s (%d)" % (signal.Signals(-rc).name, -rc)
    except OSError as e:
        out.write(("detach: could not start %s: %s\n" % (argv[0], e)).encode())
        what = "exit 127"
    try:
        with open(log, "rb") as f:
            f.seek(-1, os.SEEK_END)
            lead = b"" if f.read(1) == b"\n" else b"\n"
    except OSError:
        lead = b""
    when = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out.write(lead + ("detach end: %s at %s (pid %d)\n" % (what, when, os.getpid())).encode())
'''


def _program(argv0: str, cwd: str | None, env: Mapping[str, str] | None) -> str | None:
    """The program `argv0` names as the supervisor will find it, or None when there is none."""
    if os.sep in argv0:
        path = argv0 if os.path.isabs(argv0) or not cwd else os.path.join(cwd, argv0)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    search = (env if env is not None else os.environ).get("PATH", os.defpath)
    return shutil.which(argv0, path=search)


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
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "ab"):
        pass
    proc = subprocess.Popen([sys.executable, "-I", "-c", SUPERVISOR, log, *argv],
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
