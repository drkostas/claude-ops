"""Keep a log under a size while the program that writes it runs.

Many programs open their log for appending once and never cut or rotate it, so the log only grows. Because
such a program opens the file with O_APPEND, each of its writes goes to the file's end, whatever the end is at
that moment. So the file can be emptied while the program runs, and its next lines start again at the top.
`cap` keeps the log's last part in `<log>.1` first, then empties the log. Nothing is stopped or restarted.

A program that writes at its own offset instead (a file opened without O_APPEND) would leave a hole of zero
bytes at the top after the cut, so cap only logs that are opened for appending.

    r = cap("/path/to/app.log", max_bytes=100 * 2**20, keep_bytes=5 * 2**20)
    if r.cut:
        print(f"{r.path}: {r.bytes} bytes, the last {r.kept_bytes} kept in {r.kept}")

    claude-ops log-cap FILE... [--max MB] [--keep MB] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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


def tail(path: Path | str, keep_bytes: int) -> bytes:
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


def cap(path: Path | str, *, max_bytes: int = MAX_BYTES, keep_bytes: int = KEEP_BYTES) -> Capped:
    """Cut the log at `path` when it is over `max_bytes`. Its last `keep_bytes`, from a line start, are written
    to `<log>.1`, replacing an older one, and then the log is emptied in place, so a program holding it open for
    appending goes on writing to it. A line written between those two steps is lost. A log at or under
    `max_bytes`, or no log at all, is left as it is."""
    log = Path(path)
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


def say(r: Capped, max_bytes: int) -> str:
    if r.cut:
        return f"{r.path}: {r.bytes} bytes, cut, the last {r.kept_bytes} kept in {r.kept}"
    return f"{r.path}: {r.bytes} bytes, at most {max_bytes}, left as it is"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops log-cap",
                                description="cut each log that is over a size, keeping its end in <log>.1, while "
                                            "the program that appends to it runs (exit 0)")
    p.add_argument("paths", nargs="+", metavar="FILE", help="a log opened for appending by the program that writes it")
    p.add_argument("--max", dest="max_mb", type=float, default=MAX_BYTES / 2**20,
                   help=f"cut a log over this many MB ({MAX_BYTES // 2**20})")
    p.add_argument("--keep", dest="keep_mb", type=float, default=KEEP_BYTES / 2**20,
                   help=f"MB of the log's end to keep in <log>.1 ({KEEP_BYTES // 2**20})")
    p.add_argument("--json", action="store_true", help="print one JSON list, an object for each file")
    a = p.parse_args(argv)
    max_bytes, keep_bytes = int(a.max_mb * 2**20), int(a.keep_mb * 2**20)
    got = [cap(f, max_bytes=max_bytes, keep_bytes=keep_bytes) for f in a.paths]
    if a.json:
        print(json.dumps([asdict(r) for r in got]))
    else:
        for r in got:
            print(say(r, max_bytes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
