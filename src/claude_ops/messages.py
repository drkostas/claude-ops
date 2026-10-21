"""What the person typed into Claude Code chats, and what was said around it.

A transcript holds much more than the person's messages: tool results, task notifications, hook
feedback, skill text, compaction summaries, messages other sessions sent, and text that programs
typed into the terminal. And a message typed while Claude was still working is often NOT a "user"
record at all: it is a queue-operation ("enqueue"), and later maybe an attachment
("queued_command") or a "user" record with promptSource "queued". A reader that keeps only "user"
records misses a large part of what was asked (about a third of the mid-turn messages in one
measurement), and those asides are the ones most likely to be forgotten.

`person_messages` reads every shape, keeps only what a person typed, and folds the copies of one
queued message into one entry. `around` shows the conversation before and after any entry, so a
message can be read with what it answered and what was done about it.

    claude-ops messages --since 7d --chat build
    claude-ops messages --around <session id>:<line> --before 6 --after 10
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from . import sessions

ROOT = sessions.ROOT

#: text that Claude Code or a hook writes as a "user" record; none of it was typed by the person
SYSTEM_PREFIXES = (
    "<task-notification", "<system-reminder", "<local-command-", "<user-prompt-submit-hook",
    "<command-message>", "Caveat:", "[Request interrupted", "This session is being continued",
    "Another Claude session sent a message", "Your claude.ai usage limit", "Stop hook feedback:",
    "PreToolUse:", "PostToolUse:", "Base directory for this skill", "[Image", "(Re-invocation",
    "[Cross-session",
)
#: a message that opens with a bracketed tag ("[watcher] the build is green") was typed by a
#: program into the terminal. Callers can pass their own pattern.
PROGRAM_TAG = re.compile(r"^\[(?!Image\b|Request\b|Pasted\b)[A-Za-z][\w .:/-]{0,40}\]")
#: a paste is wrapped in <pasted_content id="..."> ... </pasted_content id="...">; a paste whose
#: closing tag is missing runs to the end of the text
_PASTE = re.compile(r"<pasted_content[^>]*>(.*?)(?:</pasted_content[^>]*>|\Z)", re.S)
_COMMAND = re.compile(r"<command-name>\s*(/[^<\s]*)\s*</command-name>")
_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
#: copies of one queued message arrive within this many seconds of each other
QUEUE_WINDOW = 6 * 3600
#: the cheap test a line must pass before it is parsed
_WANT = (b'"type":"user"', b'"type": "user"', b"queue-operation", b"queued_command")


def _at(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(x.get("text", "") for x in (content or [])
                     if isinstance(x, dict) and x.get("type") == "text")


def classify(text: str, program_tag: re.Pattern = PROGRAM_TAG) -> tuple[str, str]:
    """(kind, text) for the text of a user-side record. kind is "person", "pasted" (the person
    pasted something, maybe with words of their own), "command" (a slash command, given back as
    "/name args"), "program" (typed by a program) or "system"."""
    s = (text or "").strip()
    if not s:
        return "system", s
    m = _COMMAND.search(s)
    if m:
        a = _ARGS.search(s)
        return "command", (m.group(1) + (" " + a.group(1).strip() if a and a.group(1).strip() else ""))
    if s.startswith(SYSTEM_PREFIXES):
        return "system", s
    pastes = [p.strip() for p in _PASTE.findall(s)]
    outside = _PASTE.sub("", s).strip()
    if pastes and not outside:
        if all(program_tag.match(p) or p.startswith(SYSTEM_PREFIXES) for p in pastes):
            return "program", s
        return "pasted", s
    if program_tag.match(outside):
        return "program", s
    return ("pasted" if pastes else "person"), s


def _norm(text: str) -> str:
    return " ".join(text.split())


def _entry(d: dict, path: Path, line: int, program_tag: re.Pattern) -> dict | None:
    """One record as an entry of the timeline, or None for bookkeeping."""
    t = d.get("type")
    base = {"session": d.get("sessionId") or path.stem, "file": str(path), "line": line,
            "uuid": d.get("uuid"), "at": _at(d.get("timestamp"))}
    if t == "user":
        if d.get("isSidechain"):
            return None
        msg = d.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result"
                                             for x in content):
            out = "\n".join(_text(x.get("content")) if isinstance(x.get("content"), list)
                            else str(x.get("content") or "") for x in content
                            if isinstance(x, dict) and x.get("type") == "tool_result")
            return {**base, "role": "result", "how": None, "text": out}
        text = _text(content)
        # a pasted picture is an image block (its size note is a separate isMeta record)
        images = sum(1 for x in content if isinstance(x, dict) and x.get("type") == "image") \
            if isinstance(content, list) else 0
        if images:
            text = (text + f" [{images} image(s)]").strip()
        origin = (d.get("origin") or {}).get("kind")
        if (d.get("isMeta") or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly")
                or str(d.get("entrypoint") or "").startswith("sdk")):
            return {**base, "role": "system", "how": None, "text": text}
        if origin and origin != "human":
            role = "chat" if origin == "peer" else "system"
            return {**base, "role": role, "how": None, "text": text}
        # a prompt a schedule, a usage-limit reset or another session sent in
        if d.get("scheduledTaskId") or d.get("turnOrigin") not in (None, "human"):
            role = "chat" if d.get("turnOrigin") == "peer" else "system"
            return {**base, "role": role, "how": None, "text": text}
        kind, text = classify(text, program_tag)
        how = "queued" if d.get("promptSource") == "queued" else "typed"
        if kind in ("pasted", "command"):
            how = kind if how == "typed" else how
        role = "person" if kind in ("person", "pasted", "command") else kind
        return {**base, "role": role, "how": how, "text": text, "command": kind == "command",
                "source": "user"}
    if t == "queue-operation" and d.get("operation") == "enqueue":
        kind, text = classify(_text(d.get("content")), program_tag)
        role = "person" if kind in ("person", "pasted", "command") else kind
        return {**base, "role": role, "how": "queued", "text": text, "command": kind == "command",
                "source": "queue"}
    if t == "attachment":
        a = d.get("attachment") or {}
        if a.get("type") != "queued_command":
            return None
        kind, text = classify(_text(a.get("prompt")), program_tag)
        role = "person" if kind in ("person", "pasted", "command") else kind
        return {**base, "role": role, "how": "queued", "text": text, "command": kind == "command",
                "source": "queue"}
    if t == "assistant":
        parts = []
        for x in (d.get("message") or {}).get("content") or []:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text" and x.get("text", "").strip():
                parts.append(("assistant", x["text"]))
            elif x.get("type") == "tool_use":
                inp = x.get("input") or {}
                said = (inp.get("description") or inp.get("command") or inp.get("file_path")
                        or inp.get("prompt") or json.dumps(inp)[:200])
                parts.append(("tool", f"{x.get('name')}: {said}"))
        if not parts:
            return None
        # one assistant record usually holds one part; several are joined in order
        role = "assistant" if any(r == "assistant" for r, _ in parts) else "tool"
        return {**base, "role": role, "how": None, "text": "\n".join(
            (f"[{r}] " if r == "tool" and role == "assistant" else "") + s for r, s in parts)}
    return None


def timeline(path: Path | str, *, program_tag: re.Pattern = PROGRAM_TAG,
             only_person: bool = False) -> list[dict]:
    """Every entry of one transcript in order, with the copies of each queued message folded into
    the first (its `copies` lists the lines of the others)."""
    path = Path(path)
    out: list[dict] = []
    pending: dict[str, dict] = {}   # a queued message's text -> its entry, until its copy arrives
    try:
        fh = path.open("rb")
    except OSError:
        return out
    with fh:
        for line, raw in enumerate(fh, 1):
            if only_person and not any(w in raw for w in _WANT):
                continue
            try:
                d = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            e = _entry(d, path, line, program_tag)
            if e is None:
                continue
            if e.get("source") or (e["role"] in ("system", "chat") and e.get("text")):
                key = _norm(e.get("text") or "")
                first = pending.get(key)
                if first and e["at"] and first["at"] and e["at"] - first["at"] <= QUEUE_WINDOW:
                    # the same queued message again: as an absorbed attachment, a dequeued user
                    # record, or a second enqueue. The delivered record knows better who wrote it
                    # (a scheduled prompt is queued like a typed one), so its role wins.
                    first.setdefault("copies", []).append(line)
                    if e.get("source") == "user" or e["role"] in ("system", "chat"):
                        first["role"] = e["role"]
                    pending.pop(key, None)   # delivered: the next one with this text is new
                    continue
                if e.get("source") == "queue":
                    pending[key] = e
            out.append(e)
    return [e for e in out if e["role"] == "person"] if only_person else out


def parse_when(s: str | None, now: float | None = None) -> float | None:
    """"7d", "36h", "90m", or an ISO date or time (local time when it has no zone)."""
    if not s:
        return None
    now = time.time() if now is None else now
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([dhm])", s.strip())
    if m:
        return now - float(m.group(1)) * {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
    return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()


def select(idx: list[dict], *, chats: Iterable[str] = (), folders: Iterable[str] = (),
           session_ids: Iterable[str] = (), since: float | None = None) -> list[dict]:
    """The transcripts of the sessions index that match: a chat title (glob, any case), a folder
    (the folder or anything under it) or a session id (or its start). No filter keeps every one.
    A transcript last active before `since` cannot hold a newer message, so it is left out."""
    chats, folders, session_ids = list(chats), [f.rstrip("/") for f in folders], list(session_ids)
    out = []
    for e in idx:
        if since is not None and (e.get("active_at") or e.get("mtime") or 0) < since:
            continue
        title = (e.get("title") or "").lower()
        cwd = e.get("cwd") or ""
        wanted = (not chats and not folders and not session_ids)
        wanted |= any(fnmatch.fnmatch(title, c.lower()) for c in chats)
        wanted |= any(cwd == f or cwd.startswith(f + "/") for f in folders)
        wanted |= any(e["id"].startswith(s) for s in session_ids)
        if wanted:
            out.append(e)
    return out


def person_messages(transcripts: Iterable[dict | Path | str], *, since: float | None = None,
                    until: float | None = None, grep: str | None = None, commands: bool = False,
                    program_tag: re.Pattern = PROGRAM_TAG) -> list[dict]:
    """What the person typed, oldest first, across the given transcripts (index entries or
    paths). Slash commands are left out unless `commands`. Each entry carries `title` and `cwd`
    when it came from an index entry, and `id` ("<session>:<line>") for `around`."""
    rx = re.compile(grep, re.I) if grep else None
    out = []
    for t in transcripts:
        meta = t if isinstance(t, dict) else {"path": str(t)}
        for e in timeline(meta["path"], program_tag=program_tag, only_person=True):
            if e.get("command") and not commands:
                continue
            if since is not None and (e["at"] is None or e["at"] < since):
                continue
            if until is not None and (e["at"] is None or e["at"] >= until):
                continue
            if rx and not rx.search(e["text"]):
                continue
            e.update(id=f"{e['session']}:{e['line']}", title=meta.get("title"), cwd=meta.get("cwd"))
            out.append(e)
    return sorted(out, key=lambda e: e["at"] or 0)


def locate(message_id: str, root: Path = ROOT) -> tuple[Path, int]:
    """The transcript and line of "<session id or its start>:<line>"."""
    sid, _, line = message_id.rpartition(":")
    hits = sorted(root.glob(f"*/{sid}*.jsonl"))
    if len(hits) != 1:
        raise LookupError(f"{len(hits)} transcripts match {sid!r}")
    return hits[0], int(line)


def around(path: Path | str, line: int, *, before: int = 5, after: int = 5, tools: bool = False,
           results: bool = False, system: bool = False,
           program_tag: re.Pattern = PROGRAM_TAG) -> list[dict]:
    """The entries before and after the one at `line` (counted in entries shown, not lines), the
    entry itself marked `here`. Tool calls, tool results and system text are left out unless
    asked for; a folded queued copy finds the entry it was folded into."""
    shown = {"person", "program", "chat", "assistant"}
    shown |= {"tool"} if tools else set()
    shown |= {"result"} if results else set()
    shown |= {"system"} if system else set()
    rows = [e for e in timeline(path, program_tag=program_tag) if e["role"] in shown]
    at = next((i for i, e in enumerate(rows) if e["line"] == line or line in e.get("copies", ())), None)
    if at is None:
        at = next((i for i, e in enumerate(rows) if e["line"] > line), len(rows))
        rows.insert(at, {"line": line, "role": "here", "text": "(the line asked for is not shown)",
                         "at": None})
    out = rows[max(0, at - before): at + after + 1]
    for e in out:
        e["here"] = e is rows[at]
    return out


# ------------------------------------------------------------------------------------ the CLI

def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "????-??-?? ??:??"


def _show(text: str, width: int | None, indent: str = "    ") -> str:
    t = text if width is None or len(text) <= width else text[:width] + " ..."
    return "\n".join(indent + ln for ln in t.splitlines() or [""])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops messages",
                                description="What the person typed into chats, and the conversation around it.")
    p.add_argument("--chat", action="append", default=[], help="a chat title, globs allowed (repeat)")
    p.add_argument("--folder", action="append", default=[], help="a project folder (repeat)")
    p.add_argument("--session", action="append", default=[], help="a session id or its start (repeat)")
    p.add_argument("--since", help='"7d", "36h", "90m" or an ISO time (default: 7d)')
    p.add_argument("--until", help="the same forms, the end of the range")
    p.add_argument("--grep", help="keep messages matching this regular expression")
    p.add_argument("--commands", action="store_true", help="include slash commands")
    p.add_argument("--width", type=int, default=600, help="cut each message after this many characters")
    p.add_argument("--full", action="store_true", help="never cut a message")
    p.add_argument("-n", "--limit", type=int, default=0, help="only the newest N")
    p.add_argument("--json", action="store_true")
    p.add_argument("--around", metavar="SESSION:LINE", help="show the conversation around one message")
    p.add_argument("--before", type=int, default=5)
    p.add_argument("--after", type=int, default=5)
    p.add_argument("--tools", action="store_true", help="with --around: include tool calls")
    p.add_argument("--results", action="store_true", help="with --around: include tool results")
    a = p.parse_args(argv)
    width = None if a.full else a.width

    if a.around:
        path, line = locate(a.around)
        rows = around(path, line, before=a.before, after=a.after, tools=a.tools, results=a.results)
        if a.json:
            print(json.dumps(rows, indent=1))
            return 0
        for e in rows:
            mark = ">>" if e.get("here") else "  "
            print(f"{mark} {_when(e.get('at'))}  {e['role']:<9} line {e['line']}")
            print(_show(e.get("text") or "", width))
        return 0

    since = parse_when(a.since or "7d")
    until = parse_when(a.until)
    idx = select(sessions.build_index(), chats=a.chat, folders=a.folder, session_ids=a.session,
                 since=since)
    rows = person_messages(idx, since=since, until=until, grep=a.grep, commands=a.commands)
    if a.limit:
        rows = rows[-a.limit:]
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    for e in rows:
        print(f"{_when(e['at'])}  {(e.get('title') or '(untitled)')[:28]:<28}  {e['how']:<7} {e['id']}")
        print(_show(e["text"], width))
    print(f"{len(rows)} message(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
