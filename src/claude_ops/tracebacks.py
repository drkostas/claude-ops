"""Python tracebacks in a log file: read only what is new since the last pass, and group them.

A long-running service that logs to a file collects tracebacks nobody reads: a handler that fails on
one kind of input, a query against a table that was renamed, a header that cannot be encoded. Each
one is printed, the service carries on, and the log grows. Reading the log by hand finds them, but
only when someone thinks to look.

`read_new` reads a log from a byte offset to its last complete line and returns the tracebacks it
found and the offset to start from next time, so a scheduled pass sees each traceback once. A
traceback still being written at the end of the file is left for the next pass. A log that shrank
(rotated or truncated) is read from the start. `group` folds tracebacks with the same signature
(the exception type and the deepest frame, preferring frames in your own code) and counts them.

    claude-ops tracebacks /var/log/app.err.log                 # every traceback, grouped
    claude-ops tracebacks app.log --prefer "*/myapp/*" --json  # the frame in your own code decides
    claude-ops tracebacks app.log --offset 81920               # only what came after that byte
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

START = "Traceback (most recent call last):"
#: the lines Python prints between the tracebacks of one chain
CHAIN = ("During handling of the above exception, another exception occurred:",
         "The above exception was the direct cause of the following exception:")
FRAME = re.compile(r'^\s+File "([^"]+)", line (\d+), in (.+)$')
EXC = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")
#: how much of a log one pass reads at most; a larger backlog is skipped from its start, and counted
MAX_BYTES = 8 * 1024 * 1024


@dataclass
class Trace:
    """One traceback: where it starts in the file, its frames (file, line, function), its exception,
    the exceptions earlier in its chain, and its text."""
    offset: int
    frames: list[tuple[str, int, str]] = field(default_factory=list)
    exc_type: str = ""
    message: str = ""
    chain: list[str] = field(default_factory=list)
    text: str = ""

    def frame(self, prefer: Callable[[str], bool] | None = None) -> tuple[str, int, str] | None:
        """The deepest frame whose file `prefer` accepts, else the deepest frame."""
        if prefer:
            for f in reversed(self.frames):
                if prefer(f[0]):
                    return f
        return self.frames[-1] if self.frames else None

    def signature(self, prefer: Callable[[str], bool] | None = None) -> str:
        """The exception type and the frame's file name and function, without the line number, which
        moves with every edit: "KeyError at handlers.py:render"."""
        f = self.frame(prefer)
        return f"{self.exc_type} at {Path(f[0]).name}:{f[2]}" if f else self.exc_type


def _exception(line: str) -> tuple[str, str]:
    m = EXC.match(line)
    return (m.group(1), (m.group(2) or "").strip()) if m else (line.split(" ", 1)[0], line)


def parse(text: str, base: int = 0) -> tuple[list[Trace], int | None]:
    """The complete tracebacks in `text`, and the offset (base + bytes) where an incomplete one at the
    end starts, or None when the text ends outside a traceback."""
    lines = text.splitlines(keepends=True)
    offs, at = [], base
    for ln in lines:
        offs.append(at)
        at += len(ln.encode("utf-8", "surrogateescape"))
    out: list[Trace] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].rstrip("\r\n") != START:
            i += 1
            continue
        t, start, prev, garbled = Trace(offset=offs[i]), i, None, False
        while True:
            i += 1
            frames = []
            while i < n and (lines[i][:1] in (" ", "\t") or lines[i].rstrip("\r\n") == ""):
                if lines[i].strip() == "" and i + 1 < n and lines[i + 1][:1] not in (" ", "\t"):
                    break
                m = FRAME.match(lines[i].rstrip("\r\n"))
                if m:
                    frames.append((m.group(1), int(m.group(2)), m.group(3).strip()))
                i += 1
            while i < n and lines[i].strip() == "":
                i += 1
            if i >= n:
                return out, offs[start]
            if lines[i].rstrip("\r\n") == START or lines[i].strip() in CHAIN:
                # two threads wrote at once: another traceback began where this one's exception
                # belongs. Keep what was complete (the chain so far), and read on from that line.
                garbled = True
                break
            exc_type, message = _exception(lines[i].rstrip("\r\n"))
            t.frames, t.exc_type, t.message = frames, exc_type, message
            i += 1
            j = i
            while j < n and lines[j].strip() == "":
                j += 1
            if j < n and lines[j].strip() in CHAIN:
                k = j + 1
                while k < n and lines[k].strip() == "":
                    k += 1
                if k >= n:
                    return out, offs[start]
                if lines[k].rstrip("\r\n") == START:
                    t.chain.append(f"{exc_type}: {message}" if message else exc_type)
                    prev = (frames, exc_type, message)
                    i = k
                    continue
            break
        if garbled:
            if prev is None:
                continue
            t.frames, t.exc_type, t.message = prev
            t.chain.pop()
        t.text = "".join(lines[start:i])
        out.append(t)
    return out, None


def read_new(path: str | Path, offset: int = 0, *, max_bytes: int = MAX_BYTES) -> tuple[list[Trace], int, int]:
    """(tracebacks, next offset, bytes skipped) for what `path` holds after `offset`, up to its last
    complete line. A file smaller than `offset` was rotated or truncated and is read from the start.
    More than `max_bytes` to read: the oldest part is skipped, from a line start, and its size returned."""
    p = Path(path)
    size = p.stat().st_size
    if size < offset:
        offset = 0
    skipped = 0
    with p.open("rb") as f:
        if size - offset > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
            skipped = f.tell() - offset
            offset = f.tell()
        else:
            f.seek(offset)
        data = f.read(max(0, size - offset))
    end = data.rfind(b"\n") + 1
    if end == 0:
        return [], offset, skipped
    traces, incomplete = parse(data[:end].decode("utf-8", "surrogateescape"), base=offset)
    return traces, (incomplete if incomplete is not None else offset + end), skipped


def group(traces: Iterable[Trace], prefer: Callable[[str], bool] | None = None) -> list[dict]:
    """One entry per signature, most frequent first: {signature, exc_type, message, frame, count,
    first, last, text}, where message, frame and text are the newest one's and first/last are offsets."""
    by: dict[str, dict] = {}
    for t in traces:
        s = t.signature(prefer)
        g = by.setdefault(s, {"signature": s, "count": 0, "first": t.offset})
        f = t.frame(prefer)
        g.update(exc_type=t.exc_type, message=t.message, last=t.offset, text=t.text,
                 frame={"file": f[0], "line": f[1], "function": f[2]} if f else None)
        g["count"] += 1
    return sorted(by.values(), key=lambda g: (-g["count"], g["first"]))


def globs(patterns: list[str]) -> Callable[[str], bool] | None:
    """A `prefer` test that accepts a file matching any of the shell patterns."""
    return (lambda f: any(fnmatch.fnmatch(f, p) for p in patterns)) if patterns else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops tracebacks", description=(
        "Python tracebacks in log files, grouped by exception type and deepest frame."))
    ap.add_argument("logs", nargs="+", help="log files to read")
    ap.add_argument("--offset", type=int, default=0, help="start at this byte (one log only)")
    ap.add_argument("--prefer", action="append", default=[], metavar="GLOB",
                    help="frames in files matching this decide the signature (repeatable)")
    ap.add_argument("--ignore", action="append", default=[], metavar="TYPE",
                    help="leave out this exception type, e.g. BrokenPipeError (repeatable)")
    ap.add_argument("--json", action="store_true", help="one JSON object per log")
    a = ap.parse_args(argv)
    if a.offset and len(a.logs) > 1:
        ap.error("--offset takes one log")
    prefer = globs(a.prefer)
    for log in a.logs:
        traces, nxt, skipped = read_new(log, a.offset)
        traces = [t for t in traces if t.exc_type not in a.ignore]
        groups = group(traces, prefer)
        if a.json:
            print(json.dumps({"log": log, "next_offset": nxt, "skipped_bytes": skipped,
                              "tracebacks": len(traces), "groups": groups}))
            continue
        note = f", {skipped} bytes skipped" if skipped else ""
        print(f"{log}: {len(traces)} traceback(s) in {len(groups)} group(s), next offset {nxt}{note}")
        for g in groups:
            f = g["frame"]
            where = f" ({f['file']}:{f['line']})" if f else ""
            print(f"  {g['count']:>5}  {g['signature']}{where}  {g['message'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
