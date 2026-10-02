"""Warn a long chat before its context fills, so it writes its own summary before compacting.

Two hooks work together:

- `context_monitor.py mark` runs on PostCompact. It records the transcript's size at that moment,
  because only what was written after the last compaction is still in the live context.
- `context_monitor.py check` runs on PostToolUse. When the live part of the transcript is large
  enough to matter, it counts its tokens with Anthropic's count_tokens API. Past the threshold it
  adds a message for Claude's next step: write a structured summary first, then run /compact with
  your compact prompt. It says this once per session, and again only after a compaction.

Settings (environment variables):
  CLAUDE_OPS_CONTEXT_LIMIT       the context size in tokens (default 1000000)
  CLAUDE_OPS_CONTEXT_THRESHOLD   warn at this many tokens (default 85% of the limit)
  CLAUDE_OPS_API_KEY_FILE        a file holding an Anthropic API key (default ~/.claude/anthropic-api-key)
  CLAUDE_OPS_COMPACT_PROMPT      a file with the /compact prompt (optional)
  CLAUDE_OPS_COUNT_MODEL         the model name for count_tokens (default claude-sonnet-5)

Below about 4 bytes per token's worth of the threshold, no API call is made at all.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

STATE = Path(os.environ.get("CLAUDE_OPS_STATE", Path.home() / ".local" / "state" / "claude-ops" / "context"))
LIMIT = int(os.environ.get("CLAUDE_OPS_CONTEXT_LIMIT", "1000000"))
THRESHOLD = int(os.environ.get("CLAUDE_OPS_CONTEXT_THRESHOLD", str(int(LIMIT * 0.85))))
KEY_FILE = Path(os.environ.get("CLAUDE_OPS_API_KEY_FILE", Path.home() / ".claude" / "anthropic-api-key"))
PROMPT_FILE = os.environ.get("CLAUDE_OPS_COMPACT_PROMPT", "")
MODEL = os.environ.get("CLAUDE_OPS_COUNT_MODEL", "claude-sonnet-5")
TAIL_CAP = 80 * 1024 * 1024  # with no compaction mark, only the last 80 MB can be live


def _log(line: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "monitor.log").open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")


def live_offset(session: str, size: int) -> int:
    mark = STATE / f"{session}.compact-offset"
    if mark.exists():
        try:
            return int(mark.read_text().strip() or 0)
        except ValueError:
            pass
    return max(0, size - TAIL_CAP)


def live_text(path: Path, offset: int, cap: int = 8_000_000) -> str:
    parts = []
    with path.open(encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        if offset:
            f.readline()  # a partial line at the boundary
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("message") or e
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        t = b.get("text") or b.get("content") or ""
                        if isinstance(t, str):
                            parts.append(t)
    return "\n".join(parts)[:cap]


def count_tokens(text: str) -> int:
    key = KEY_FILE.read_text().strip()
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages/count_tokens", data=body, method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return int(json.load(r).get("input_tokens", 0))


def nudge(count: int) -> str:
    prompt = Path(PROMPT_FILE).expanduser().read_text().strip() if PROMPT_FILE and Path(PROMPT_FILE).expanduser().exists() else ""
    msg = (f"Context is at {count:,} of {LIMIT:,} tokens ({round(100 * count / LIMIT)}%). Before anything else:\n"
           "1. Write a structured summary into the conversation: the active task, the decisions and why, the files "
           "and their state, errors and how they were solved, what is still open, and the user's corrections quoted word for word.\n"
           "2. Then run /compact")
    msg += f" with this prompt, unchanged:\n\n{prompt}" if prompt else "."
    return msg


def check(event: dict, counter=count_tokens) -> dict | None:
    path = Path(event.get("transcript_path") or "")
    session = event.get("session_id") or "default"
    if not path.is_file():
        return None
    size = path.stat().st_size
    flag = STATE / f"{session}.nudged"
    if flag.exists():
        # the transcript shrank by half: a compaction ran, so the warning may be given again
        if size < int(flag.read_text().strip() or 0) // 2:
            flag.unlink()
        else:
            return None
    offset = live_offset(session, size)
    if size - offset < THRESHOLD * 2.6:  # too small to be near the threshold, no API call
        return None
    text = live_text(path, offset)
    if not text:
        return None
    try:
        count = counter(text)
    except Exception as e:  # noqa: BLE001 - a failed count never blocks the chat
        _log(f"session={session} count_tokens failed: {e}")
        return None
    _log(f"session={session} live_tokens={count} live_bytes={size - offset} threshold={THRESHOLD}")
    if count < THRESHOLD:
        return None
    STATE.mkdir(parents=True, exist_ok=True)
    flag.write_text(str(size))
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": nudge(count)}}


def mark(event: dict) -> None:
    path = Path(event.get("transcript_path") or "")
    session = event.get("session_id")
    if not session or not path.is_file():
        return
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{session}.compact-offset").write_text(str(path.stat().st_size))
    (STATE / f"{session}.nudged").unlink(missing_ok=True)
    _log(f"session={session} compacted at {path.stat().st_size}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        event = json.load(sys.stdin)
        if argv[:1] == ["mark"]:
            mark(event)
        else:
            out = check(event)
            if out:
                print(json.dumps(out))
    except Exception:  # noqa: BLE001 - a hook must never break the chat
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
