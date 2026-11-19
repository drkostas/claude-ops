"""PreToolUse hook. Refuse a tool by its name, with a message saying what to use instead.

Some tools fit one setup and not another. A question box (AskUserQuestion) is one example: it waits in
one terminal, it is gone after a restart or a compaction, and a person who answers from somewhere
else never sees it. A setup that asks people another way (a task on their phone, a ticket) wants the
box refused, with words that send the session to the other way, instead of a rule the model may
forget.

The configuration is a JSON file (`--config`, else `CLAUDE_OPS_REFUSED_TOOLS_CONFIG`):

    {"refuse": [
      {"tools": "AskUserQuestion", "message": "Ask through the ticket tool instead, with the whole question in it."}
    ]}

`tools` is a regular expression matched against the whole tool name, as settings.json's `matcher` is.
The first entry that matches refuses the call with "Blocked: <message>" and exit 2. A tool no entry
names goes through. With no readable configuration, or an entry without a message, nothing is
refused: a broken hook must never stop work.

    python3 -P -m claude_ops.hooks.refused_tools --config FILE
"""
from __future__ import annotations

import json
import os
import re
import sys

CONFIG_ENV = "CLAUDE_OPS_REFUSED_TOOLS_CONFIG"


def entries(argv: list[str]) -> list[dict]:
    """The configured entries, each with a compiled pattern; [] when none is readable."""
    path = None
    if "--config" in argv and argv.index("--config") + 1 < len(argv):
        path = argv[argv.index("--config") + 1]
    path = path or os.environ.get(CONFIG_ENV)
    if not path:
        return []
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for e in (raw.get("refuse") if isinstance(raw, dict) else None) or []:
        if not isinstance(e, dict) or not str(e.get("message") or "").strip() or not e.get("tools"):
            continue
        try:
            out.append({"pattern": re.compile(str(e["tools"])), "message": " ".join(str(e["message"]).split())})
        except re.error:
            continue
    return out


def check(payload: dict, configured: list[dict]) -> str | None:
    """The message for this call's tool, or None when no entry names it."""
    name = str(payload.get("tool_name") or "")
    if not name:
        return None
    for e in configured:
        if e["pattern"].fullmatch(name):
            return e["message"]
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        payload = json.load(sys.stdin)
        why = check(payload, entries(argv))
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {payload.get('tool_name')} is not used here. {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
