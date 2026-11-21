"""Keep the adb server's log under a size while the server runs.

The adb server writes everything it does to one log file. It opens the file for appending when it starts and
never cuts or rotates it, and a new server appends to the same file, so the log only grows. A server that keeps
retrying a device it cannot reach writes a few lines every second, and over some weeks that is gigabytes.

Because the server opens the log for appending, each of its writes goes to the file's end, whatever the end is
at that moment. So the file can be emptied while the server runs, and its next lines start again at the top.
`cap` keeps the log's last part in `<log>.1` first, then empties the log. No server is stopped. The cut itself
is `claude_ops.log_cap.cap`, which works on any log opened for appending, and this module only finds adb's.

    r = cap(max_bytes=100 * 2**20, keep_bytes=5 * 2**20)
    if r.cut:
        print(f"{r.path}: {r.bytes} bytes, the last {r.kept_bytes} kept in {r.kept}")

    claude-ops adb-log [--max MB] [--keep MB] [--path FILE] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from claude_ops import log_cap
from claude_ops.log_cap import KEEP_BYTES, MAX_BYTES, Capped, tail

__all__ = ["KEEP_BYTES", "MAX_BYTES", "Capped", "cap", "main", "server_log", "tail"]


def server_log(env: dict[str, str] | None = None) -> Path:
    """Where the adb server writes its log, by the rule adb itself follows: ANDROID_ADB_LOG_PATH when it is set,
    else adb.<uid>.log in TMPDIR, or in /tmp when TMPDIR is not set. On Windows it is adb.log in the temporary
    folder. The server takes these from the environment of the client that started it, so a caller whose own
    environment differs passes the path to `cap` instead."""
    env = os.environ if env is None else env
    if env.get("ANDROID_ADB_LOG_PATH"):
        return Path(env["ANDROID_ADB_LOG_PATH"])
    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "adb.log"
    return Path(env.get("TMPDIR") or "/tmp") / f"adb.{os.getuid()}.log"


def cap(path: Path | str | None = None, *, max_bytes: int = MAX_BYTES, keep_bytes: int = KEEP_BYTES) -> Capped:
    """Cut the adb server's log (`path`, else `server_log()`) when it is over `max_bytes`, by
    `claude_ops.log_cap.cap`. Its last `keep_bytes`, from a line start, are written to `<log>.1`, replacing an
    older one, and then the log is emptied in place, so the running server goes on writing to it. A line the
    server writes between those two steps is lost. A log at or under `max_bytes`, or no log at all, is left as
    it is."""
    return log_cap.cap(path if path is not None else server_log(), max_bytes=max_bytes, keep_bytes=keep_bytes)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops adb-log",
                                description="cut the adb server's log when it is over a size, keeping its end "
                                            "in <log>.1, while the server runs (exit 0)")
    p.add_argument("--max", dest="max_mb", type=float, default=MAX_BYTES / 2**20,
                   help=f"cut a log over this many MB ({MAX_BYTES // 2**20})")
    p.add_argument("--keep", dest="keep_mb", type=float, default=KEEP_BYTES / 2**20,
                   help=f"MB of the log's end to keep in <log>.1 ({KEEP_BYTES // 2**20})")
    p.add_argument("--path", default=None, help="the log (default: where adb puts it, from this environment)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    r = cap(a.path, max_bytes=int(a.max_mb * 2**20), keep_bytes=int(a.keep_mb * 2**20))
    print(json.dumps(asdict(r)) if a.json else log_cap.say(r, int(a.max_mb * 2**20)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
