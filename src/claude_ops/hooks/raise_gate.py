"""Stop hook. A report that follows a tool fault must say what was raised about it.

A session that uses tools it did not write is the only witness to their faults: the owner of a tool
cannot see the call, and the tool's own tests only ask what its author thought to ask. The cost of a
fault (a refusal with an opaque message, a schema learned by retrying) lands on the session that hit
it, while the benefit of reporting it lands on everyone else, so by default nobody pays that cost.
This hook makes the cost visible at the one moment it can be seen, the end of the turn.

It keeps the turn going when two things hold together:

- since the user last spoke, a call to a watched tool failed (its result was an error), and
- the final message has no line naming what was raised about it.

A watched tool is one whose name starts with one of `tool_prefixes`, or a Bash command that runs one
of `programs` (or `python -m` one of `modules`). A watched MCP tool counts on any error result. A watched
command counts only when its output also holds one of `bash_shapes`, because such a program may exit
with a failure on purpose to report a finding, and that is the program working. A result that holds one
of `ignore` is the session's own mistake and never counts (a call made before its tool's schema was
loaded, a command another hook refused). An ordinary shell error, a failing test or a git conflict is
not a watched tool, so it never counts.

The final message passes when it has a line that starts with the heading (default "Raised upstream")
and then either names an item id (eight hexadecimal characters with a digit among them), or says
`none` followed by a reason of at least two words. A session that judged a fault not worth an item has
still done the thinking and has to say why.

What it reads, from a JSON file:

    {
      "heading": "Raised upstream",
      "tool_prefixes": ["mcp__service__"],
      "programs": ["toolA", "toolB"],
      "modules": ["tool_a", "tool_b"],
      "bash_shapes": ["Traceback (most recent call last)", "usage:", "error:", "Blocked:"],
      "ignore": ["was not in your prompt", "Blocked: sleep"]
    }

The file is `--config`, else `CLAUDE_OPS_RAISE_GATE_CONFIG`, else `~/.config/claude-ops/raise-gate.json`.
With no file nothing is read and nothing is held. A fault that exits 0 (a traceback in the output of a
command that still succeeded) is not seen: the failure flag is the one fact the transcript records
about a result, and matching words in a success is reading prose. That includes any watched command
piped into another program, since a pipeline exits with its last command (`prog | tail -1` exits 0
when `prog` fails). This is not a property of the gate. Anything that reads a transcript for what
failed (a count of tool faults, a check built on the failure flag, a review of a session's history)
sees a success for a failed command whose output was piped into `tail` or `grep`, and nothing in
the transcript says so.

It answers with `hookSpecificOutput.additionalContext`, the channel for feedback that is not an
error, and it fails open on anything unexpected. It cannot loop: when the stop was already held once
(`stop_hook_active`), the second stop goes through.

    claude-ops hook raise-gate           # reads the Stop event on stdin
    python3 -P -m claude_ops.hooks.raise_gate [--config FILE]
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import commandline
from .ask_what_you_need import _is_tool_result, feedback

DEFAULT_CONFIG = "~/.config/claude-ops/raise-gate.json"
CONFIG_ENV = "CLAUDE_OPS_RAISE_GATE_CONFIG"
DEFAULT_HEADING = "Raised upstream"
ITEM_ID = re.compile(r"(?<![0-9a-f])(?=[0-9a-f]*\d)[0-9a-f]{8}(?![0-9a-f])", re.I)
NONE = re.compile(r"^\W*none\b\W*(.*)$", re.I)
NEXT_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s|\d+\.\s+\*{2}|\*{2}[^*]+\*{2}\s*$|---\s*$)")


class Config:
    def __init__(self, data: dict):
        def words(key: str) -> tuple[str, ...]:
            v = data.get(key) or ()
            return tuple(str(x) for x in v) if isinstance(v, (list, tuple)) else ()
        self.heading = str(data.get("heading") or DEFAULT_HEADING)
        self.tool_prefixes = words("tool_prefixes")
        self.programs = words("programs")
        self.modules = words("modules")
        self.bash_shapes = words("bash_shapes")
        self.ignore = words("ignore")

    def watches_anything(self) -> bool:
        return bool(self.tool_prefixes or self.programs or self.modules)


def load(path: str | None = None) -> Config | None:
    path = os.path.expanduser(path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return Config(data) if isinstance(data, dict) else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _turn_start(records: list[dict]) -> int:
    for i in range(len(records) - 1, -1, -1):
        if records[i].get("type") == "user" and not _is_tool_result(records[i]):
            return i
    return 0


def runs_watched(command: str, cfg: Config) -> bool:
    """True when a simple command of `command` runs a watched program, or `python -m` a watched module."""
    for words in commandline.simple_commands(command):
        words = commandline.unwrap(words)
        if not words:
            continue
        if os.path.basename(words[0]) in cfg.programs:
            return True
        if "-m" in words[1:] and os.path.basename(words[0]).startswith("python"):
            i = words.index("-m")
            if i + 1 < len(words) and words[i + 1].split(".")[0] in cfg.modules:
                return True
    return False


def faults(records: list[dict], cfg: Config) -> list[tuple[str, str]]:
    """(tool, error text) for each watched call of the turn that failed."""
    calls: dict[str, tuple[str, dict]] = {}
    found: list[tuple[str, str]] = []
    for rec in records[_turn_start(records):]:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                calls[b.get("id", "")] = (b.get("name", ""), b.get("input") or {})
            elif b.get("type") == "tool_result" and b.get("is_error") is True:
                name, inp = calls.get(b.get("tool_use_id", ""), ("", {}))
                text = _text(b.get("content"))
                if any(s in text for s in cfg.ignore):
                    continue
                if cfg.tool_prefixes and name.startswith(cfg.tool_prefixes):
                    found.append((name, text))
                elif name == "Bash" and isinstance(inp.get("command"), str) \
                        and any(s in text for s in cfg.bash_shapes) and runs_watched(inp["command"], cfg):
                    found.append((name, text))
    return found


def final_text(records: list[dict]) -> str:
    text = ""
    for rec in records[_turn_start(records):]:
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    text = b.get("text") or text
    return text


def raised_line(message: str, heading: str) -> str | None:
    """The line that starts with the heading and what follows it up to the next heading, or None."""
    pat = re.compile(rf"^\s{{0,3}}(?:\d+\.\s*)?[#*\s-]*{re.escape(heading)}\b[\s*:.\-–—]*(.*)$", re.I)
    lines = message.splitlines()
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            body = [m.group(1)]
            for nxt in lines[i + 1:]:
                if not nxt.strip() or NEXT_HEADING.match(nxt):
                    break
                body.append(nxt)
            return " ".join(" ".join(body).replace("*", " ").split())
    return None


def satisfied(message: str, heading: str) -> bool:
    body = raised_line(message, heading)
    if body is None:
        return False
    none = NONE.match(body)
    if none:
        return len(none.group(1).split()) >= 2
    return bool(ITEM_ID.search(body))


def check(records: list[dict], cfg: Config) -> str | None:
    """Why this turn may not end yet, or None when it may."""
    if not cfg.watches_anything():
        return None
    hit = faults(records, cfg)
    if not hit or satisfied(final_text(records), cfg.heading):
        return None
    tool, text = hit[0]
    first = " ".join(text.split())[:160]
    return (
        f"From the raise-gate hook. This turn a call to {tool} failed, and your message has no "
        f"'{cfg.heading}' line that says what was raised about it:\n\n"
        f"    {first}\n\n"
        f"Raise it now (a tool that is not yours is one you are the only witness to), then end the "
        f"message with '{cfg.heading}: <item id>'. If the fault was your own mistake or not worth an "
        f"item, write '{cfg.heading}: none, <the reason>'."
    )


def main(argv: list[str] | None = None) -> int:
    try:
        argv = sys.argv[1:] if argv is None else argv
        path = argv[argv.index("--config") + 1] if "--config" in argv else None
        cfg = load(path)
        if cfg is None:
            return 0
        event = json.load(sys.stdin)
        if event.get("stop_hook_active") or not event.get("transcript_path"):
            return 0
        with open(event["transcript_path"], encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        reason = check(records, cfg)
    except Exception:  # noqa: BLE001 - fail open
        return 0
    if reason:
        print(json.dumps(feedback(reason)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
