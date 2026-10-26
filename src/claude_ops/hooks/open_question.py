"""Remember a session's open question box before its transcript holds it (a hook).

Claude Code writes a tool call to the session's transcript only once the call has returned. While
an AskUserQuestion box waits for an answer, the transcript does not name it: its newest records are
the ones before the question. A program that needs to know whether a session is waiting on a
question (to hold what it would send the session, or to show the question somewhere else) cannot
learn it from the transcript until somebody has answered.

Run as a hook, this module keeps the open call in a small file per session from the moment Claude
Code starts it:

- PreToolUse on AskUserQuestion writes `<folder>/<session id>.json` with the call's id, its
  questions, the time and the transcript's path;
- PostToolUse on AskUserQuestion removes it, when it names the same call;
- SessionStart and SessionEnd remove the session's file, since a box never outlives its process.

`open_question(session, records)` reads it back: the call, or None when there is none or when the
transcript's records already hold the call's result. A call is read as closed that way even when no
PostToolUse hook removed its file.

    "hooks": {
      "PreToolUse":  [{"matcher": "AskUserQuestion", "hooks": [{"type": "command",
                       "command": "python3 -m claude_ops.hooks.open_question"}]}],
      "PostToolUse": [{"matcher": "AskUserQuestion", "hooks": [{"type": "command",
                       "command": "python3 -m claude_ops.hooks.open_question"}]}],
      "SessionStart": [{"matcher": "", "hooks": [{"type": "command",
                       "command": "python3 -m claude_ops.hooks.open_question"}]}]
    }

The folder is `CLAUDE_OPS_QUESTIONS`, else `~/.local/state/claude-ops/questions`. The hook prints
nothing and always exits 0: a broken hook must never stop work.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path

TOOL = "AskUserQuestion"


def folder() -> Path:
    return Path(os.environ.get("CLAUDE_OPS_QUESTIONS")
                or Path.home() / ".local" / "state" / "claude-ops" / "questions")


def _file(session: str, state: Path | None) -> Path | None:
    # a session id is a file name here, so anything that could leave the folder is refused
    if not session or "/" in session or session.startswith("."):
        return None
    return (state or folder()) / f"{session}.json"


def record(payload: dict, state: Path | None = None) -> str | None:
    """Keep or drop the session's open call for one hook event. Returns "kept", "dropped" or None
    (nothing to do)."""
    event, session = payload.get("hook_event_name"), str(payload.get("session_id") or "")
    path = _file(session, state)
    if path is None or payload.get("agent_id"):
        return None
    if event == "PreToolUse" and payload.get("tool_name") == TOOL:
        call = str(payload.get("tool_use_id") or "")
        questions = (payload.get("tool_input") or {}).get("questions")
        if not call or not isinstance(questions, list):
            return None
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"session": session, "id": call, "questions": questions, "at": time.time(),
                       "transcript": payload.get("transcript_path")}, f)
        os.replace(tmp, path)
        return "kept"
    if event == "PostToolUse" and payload.get("tool_name") == TOOL:
        kept = _read(path)
        if kept is None or kept.get("id") != payload.get("tool_use_id"):
            return None
    elif event not in ("SessionStart", "SessionEnd"):
        return None
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return "dropped"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") else None


def answered(records: Iterable[dict], call: str) -> bool:
    """Whether a transcript's records hold the result of the tool call `call`."""
    for r in records:
        if r.get("type") != "user":
            continue
        content = (r.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                             and c.get("tool_use_id") == call for c in content):
            return True
    return False


def open_question(session: str, records: Iterable[dict] | None = None, *,
                  state: Path | None = None) -> dict | None:
    """The session's open AskUserQuestion call, {"id", "questions", "at", "transcript"}, or None when
    the hook kept none or `records` (the transcript's, when given) already hold its result."""
    path = _file(session, state)
    kept = _read(path) if path else None
    if kept is None or not isinstance(kept.get("questions"), list):
        return None
    if records is not None and answered(records, kept["id"]):
        return None
    return {k: kept.get(k) for k in ("id", "questions", "at", "transcript")}


def main() -> int:
    try:
        record(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
