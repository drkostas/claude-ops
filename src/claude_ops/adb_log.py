"""Keep the adb server's log under a size while the server runs.

The adb server writes everything it does to one log file. It opens the file for appending when it starts and
never cuts or rotates it, and a new server appends to the same file, so the log only grows. A server that keeps
retrying a device it cannot reach writes a few lines every second, and over some weeks that is gigabytes.

Because the server opens the log for appending, each of its writes goes to the file's end, whatever the end is
at that moment. So the file can be emptied while the server runs, and its next lines start again at the top.
`cap` keeps the log's last part in `<log>.1` first, then empties the log. No server is stopped.

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
from dataclasses import asdict, dataclass
from pathlib import Path

#: a log over this many bytes is cut
MAX_BYTES = 100 * 2**20
#: how much of the log's end a cut keeps, in <log>.1
KEEP_BYTES = 5 * 2**20


@dataclass
class Capped:
    path: str
    bytes: int              # the log's size when it was read, 0 when there is none
    cut: bool
    kept: str | None = None
    kept_bytes: int = 0


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


def tail(path: Path, keep_bytes: int) -> bytes:
    """The last `keep_bytes` of a file, from the start of a line: a first line cut in the middle is left out."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        if size <= keep_bytes:
            f.seek(0)
            return f.read()
        f.seek(size - keep_bytes)
        data = f.read()
    nl = data.find(b"\n")
    return data[nl + 1:] if nl >= 0 else b""


def cap(path: Path | str | None = None, *, max_bytes: int = MAX_BYTES, keep_bytes: int = KEEP_BYTES) -> Capped:
    """Cut the adb server's log (`path`, else `server_log()`) when it is over `max_bytes`. Its last `keep_bytes`,
    from a line start, are written to `<log>.1`, replacing an older one, and then the log is emptied in place,
    so the running server goes on writing to it. A line the server writes between those two steps is lost. A
    log at or under `max_bytes`, or no log at all, is left as it is."""
    log = Path(path) if path is not None else server_log()
    try:
        size = log.stat().st_size
    except FileNotFoundError:
        return Capped(str(log), 0, False)
    if size <= max_bytes:
        return Capped(str(log), size, False)
    kept = log.with_name(log.name + ".1")
    data = tail(log, keep_bytes)
    tmp = kept.with_name(kept.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, kept)
    os.truncate(log, 0)
    return Capped(str(log), size, True, str(kept), len(data))


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
    if a.json:
        print(json.dumps(asdict(r)))
    elif r.cut:
        print(f"{r.path}: {r.bytes} bytes, cut, the last {r.kept_bytes} kept in {r.kept}")
    else:
        print(f"{r.path}: {r.bytes} bytes, at most {int(a.max_mb * 2**20)}, left as it is")
    return 0


if __name__ == "__main__":
    sys.exit(main())
