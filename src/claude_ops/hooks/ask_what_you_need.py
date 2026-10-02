"""Stop hook. A report section that asks the user for something must be asked with AskUserQuestion.

If your chats end their reports with a section such as "What I need from you", the asks in it are
easy to miss as prose, and then they come back in the next report. This hook blocks the end of a
turn when that section says anything other than "Nothing" and no AskUserQuestion was called since
the user last spoke, and tells Claude why.

The heading is configurable with CLAUDE_OPS_ASK_HEADING (default "What I need from you").

It is a Stop hook because the problem is a tool that was NOT called, and the end of the turn is the
only moment that can be seen. It always fails open: an unexpected input lets the turn end, because
a hook that crashes would be worse than one missed question. And it cannot loop: when the stop was
already blocked once (`stop_hook_active`), the second stop goes through.
"""
from __future__ import annotations

import json
import os
import re
import sys

HEADING_TEXT = os.environ.get("CLAUDE_OPS_ASK_HEADING", "What I need from you")
NEXT_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s|\*{2}[^*]+\*{2}\s*$|---\s*$)")
NOTHING = re.compile(r"^\W*(nothing|none|no asks?|nothing for now)\b", re.I)


def heading_re(text: str = HEADING_TEXT) -> re.Pattern:
    words = r"\s+".join(re.escape(w) for w in text.split())
    return re.compile(rf"^\s{{0,3}}(?:#{{1,6}}\s*|\*{{1,2}}\s*)?{words}\b[:*\s]*$", re.I)


def section_text(message: str, heading: str = HEADING_TEXT) -> str | None:
    """The body of the section, or None when there is no such section."""
    pat = heading_re(heading)
    lines = message.splitlines()
    for i, line in enumerate(lines):
        if pat.match(line.strip()):
            body: list[str] = []
            for nxt in lines[i + 1:]:
                if NEXT_HEADING.match(nxt) and body:
                    break
                body.append(nxt)
            return "\n".join(body).strip()
    return None


def _is_tool_result(rec: dict) -> bool:
    """A tool result is stored as a user record, but it is not the user speaking."""
    content = (rec.get("message") or {}).get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def check(records: list[dict], heading: str = HEADING_TEXT) -> str | None:
    """The block reason for this turn, or None when the turn may end."""
    start = 0
    for i in range(len(records) - 1, -1, -1):
        if records[i].get("type") == "user" and not _is_tool_result(records[i]):
            start = i
            break
    asked, final_text = False, ""
    for rec in records[start:]:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            if rec.get("type") == "assistant" and isinstance(content, str):
                final_text = content
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                asked = True
            elif block.get("type") == "text" and rec.get("type") == "assistant":
                final_text = block.get("text") or final_text
    if asked:
        return None
    body = section_text(final_text, heading)
    if body is None or NOTHING.match(body):
        return None
    first = " ".join(body.split())[:160]
    return (
        f"Blocked by the ask-what-you-need hook. Your message has a '{heading}' section that is not "
        "'Nothing', and you did not call AskUserQuestion this turn:\n\n"
        f"    {first}\n\n"
        "Call AskUserQuestion now, one question per real decision (up to four), each with real "
        "options and your recommendation first. If an item is yours to settle (by reading a file, "
        "running a command or checking git), settle it instead and remove it from the section."
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if event.get("stop_hook_active") or not event.get("transcript_path"):
            return 0
        with open(event["transcript_path"], encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        reason = check(records)
    except Exception:  # noqa: BLE001 - fail open
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
