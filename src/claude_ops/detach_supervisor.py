"""The leader of a session `detach` starts: run one command, then add its end line to the log.

`detach` runs this file as `python -I <this file> LOG COMMAND [ARGS...]`, by its path, so `ps` shows
a short command line and a waiter or a stop can find its job by it. Isolated mode (-I) keeps the
job's folder, the script's folder, the user's site-packages and every PYTHON* variable off the
import path, so the supervisor imports only the standard library, whatever environment and folder
the command is given. It imports nothing from claude_ops for the same reason.

It runs the command in its own session with stdin from /dev/null and stdout and stderr appended to
LOG, passes SIGTERM, SIGINT and SIGHUP on to it, and when it ends appends one line to LOG:

    detach end: exit 0 at 2026-11-14T13:30:01+0200 (pid 4242)
    detach end: signal SIGTERM (15) at 2026-11-14T13:31:12+0200 (pid 4242)
"""
import os
import signal
import subprocess
import sys
import time


def run(log, argv):
    """Run `argv` with its output appended to `log`, then append the end line. Returns the line."""
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
        line = "detach end: %s at %s (pid %d)" % (what, when, os.getpid())
        out.write(lead + (line + "\n").encode())
    return line


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.stderr.write("usage: detach_supervisor.py LOG COMMAND [ARGS...]\n")
        raise SystemExit(2)
    run(sys.argv[1], sys.argv[2:])
