"""answer_sizes: how long each tool's answers were, read from Claude Code transcripts.

A tool whose answer can grow past what a session should read whole (a list, a log, a queue) needs a
limit, and a tool whose answer is one row does not. Which is which is a measurement before it is a
decision, and the transcripts already hold every answer a session received. This reads them and keeps
only lengths, per tool, never the text.

Two things make the figures honest:

- Each answer is dated by its own transcript line, never by its file. A long-lived session's file is
  new while its first answers can be months old, so a file's date stands in for the answer's date only
  when it is older than the window (then nothing in it is new enough, and the file is skipped).
- An MCP server's answer often reaches the transcript wrapped as `{"result": "<the tool's text>"}`, with
  every quote and line break escaped once more. The wrapper is taken off before the length is counted,
  so a figure is the tool's own answer rather than about a tenth more.

A measured maximum is what happened in the window, not a bound on what can happen. Use it to find the
tools worth a look, and read a tool's code before calling its answer small. When several answers share
the largest length exactly, the tool reports it as a cut, since answers of real sizes have one largest.

    claude-ops answer-sizes --prefix mcp__myserver__ [--days 30] [--limit 20000] [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from .paths import PROJECTS

#: the answer length, in characters, over which a tool's answer is counted as over the limit
LIMIT = 20000


def answer_text(content) -> str:
    """The text a tool_result block holds, with an MCP `{"result": "..."}` wrapper taken off."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type", "text") == "text")
    else:
        return ""
    if text.startswith('{"result"'):
        try:
            inner = json.loads(text)
        except ValueError:
            return text
        if isinstance(inner, dict) and list(inner) == ["result"] and isinstance(inner["result"], str):
            return inner["result"]
    return text


def sizes_in(lines, prefix: str, since: str = "") -> list[tuple[str, int]]:
    """(tool, length) for each answer in one transcript's `lines` whose tool's name starts with `prefix`
    and whose line is dated at or after `since` (an ISO 8601 time in UTC, as transcripts write it).
    The tool is named by the tool_use block the answer replies to, so a name may hold any characters."""
    names: dict[str, str] = {}
    out: list[tuple[str, int]] = []
    for line in lines:
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        # only a line naming a wanted tool, or a reply once one was named, is worth parsing
        if not (prefix in text or (names and '"tool_result"' in text)):
            continue
        try:
            rec = json.loads(text)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and str(b.get("name") or "").startswith(prefix) and b.get("id"):
                names[b["id"]] = b["name"][len(prefix):]
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in names:
                if since and str(rec.get("timestamp") or "") < since:
                    continue
                out.append((names[b["tool_use_id"]], len(answer_text(b.get("content")))))
    return out


def summary(pairs, limit: int = LIMIT) -> list[dict]:
    """Per tool, the number of answers, the largest, the 99th percentile, how many were over `limit`, and
    how many share that largest length exactly (`at_max`), largest first.

    Answers of real sizes have one largest. Several answers of exactly the same largest length are how a
    cut looks from the reader's side (the tool or a layer under it stopped every long answer at one
    length), so `cut` is true when `at_max` is more than one, and that `max` is the cut's length rather
    than a size any answer reached. This needs no knowledge of any limit."""
    by: dict[str, list[int]] = {}
    for tool, n in pairs:
        by.setdefault(tool, []).append(n)
    rows = []
    for tool, v in by.items():
        v.sort()
        at_max = sum(1 for n in v if n == v[-1])
        rows.append({"tool": tool, "count": len(v), "max": v[-1], "p99": v[min(len(v) - 1, int(len(v) * 0.99))],
                     "over": sum(1 for n in v if n > limit), "at_max": at_max, "cut": at_max > 1})
    return sorted(rows, key=lambda r: (-r["max"], r["tool"]))


def measure(prefix: str, *, days: float = 30, limit: int = LIMIT, root: "Path | None" = None,
            now: "float | None" = None) -> dict:
    """Every transcript under `root` (subagents' too), read for the answers of the last `days` days."""
    root = Path(root or PROJECTS)
    cutoff = (now if now is not None else time.time()) - days * 86400
    since = dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    pairs: list[tuple[str, int]] = []
    files = 0
    for path in sorted(root.rglob("*.jsonl")):
        try:
            if path.stat().st_mtime < cutoff:
                continue          # nothing in a file last written before the window can be in it
            with path.open(encoding="utf-8", errors="replace") as fh:
                pairs.extend(sizes_in(fh, prefix, since))
        except OSError:
            continue
        files += 1
    return {"prefix": prefix, "since": since, "days": days, "limit": limit, "files": files,
            "tools": summary(pairs, limit)}


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops answer-sizes",
                                description="how long each tool's answers were, read from the transcripts "
                                            "(lengths only, never the text)")
    p.add_argument("--prefix", required=True, help="the tools to measure, by the start of their name, "
                                                   "e.g. mcp__myserver__")
    p.add_argument("--days", type=float, default=30, help="answers of this many last days (default 30)")
    p.add_argument("--limit", type=int, default=LIMIT, help=f"count answers longer than this (default {LIMIT})")
    p.add_argument("--root", help="the transcripts folder (default ~/.claude/projects)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    out = measure(a.prefix, days=a.days, limit=a.limit, root=Path(a.root) if a.root else None)
    if a.json:
        print(json.dumps(out, indent=1))
        return 0
    print(f"{out['files']} transcripts, answers since {out['since']} UTC, over means longer than {a.limit}")
    for r in out["tools"]:
        print(f"{r['tool']:32} answers {r['count']:6}  max {r['max']:7}  p99 {r['p99']:7}  over {r['over']}"
              + (f"  ({r['at_max']} answers end at exactly {r['max']}, which is how a cut looks)" if r["cut"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
