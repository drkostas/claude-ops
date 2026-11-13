"""meter: what each Claude Code chat on this machine spends, read three ways, and never acted on.

The usage endpoint gives one account's percentages. It says nothing about which chat spent them, so
this keeps a small store of its own, and labels every figure by how it is known:

  official   the statusline payload Claude Code hands its status line command: the limits, the context
             window, the cost and the prompt-cache block. Written at most once a minute per session, so a
             command that runs on every render costs nothing (`take`).
  measured   the transcripts, for history and for what telemetry leaves out (`read_all`), and Claude
             Code's own telemetry through one OTLP collector on this machine (`serve`). The Prometheus
             exporter binds one port, so many sessions cannot share it, and OTLP is the route that works.
  estimated  the weighted figure (`weighted`), the client's local weighting of a response, read from
             its code. Anthropic documents no formula, so it is a strong prior and not a contract.

One API response is written as several transcript lines, each repeating the same usage, so a response
is counted by its message id, or every figure comes out about twice too big. A session's subagents write
their own files under `<session>/subagents/`, and those count too.

What a chat spent on each piece of work (`attribute`) comes from the work item ids its turns name. The caller
says what an id looks like and which tools carry one (`Marks`), and the reader keeps only the ids it matched
and where they were, never the text around them.

Three states everywhere: a reading that is absent is unknown, never zero. The store lives at
~/.local/state/claude-ops/meter.sqlite3 unless CLAUDE_OPS_METER_DB says otherwise. It holds numbers and
names, never message text.
"""
from __future__ import annotations

import bisect
import datetime as dt
import gzip
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(os.environ.get("CLAUDE_OPS_METER_DB") or Path.home() / ".local" / "state" / "claude-ops" / "meter.sqlite3")
#: the status line is written at most this often per session
EVERY = 60.0
#: how long the status line rows and the telemetry rows are kept. Responses are kept for good, since
#: Claude Code cleans its own transcripts after about 30 days and they are the only history.
KEEP_DAYS = 45
PORT = 4318

SCHEMA = """
CREATE TABLE IF NOT EXISTS statusline(
  session_id TEXT NOT NULL, at REAL NOT NULL, model TEXT, cwd TEXT, context_tokens INTEGER, context_pct REAL,
  cost_usd REAL, limits TEXT, hit_ratio REAL, expected_rebuilds REAL, miss_causes TEXT, payload TEXT,
  PRIMARY KEY(session_id, at));
CREATE INDEX IF NOT EXISTS statusline_at ON statusline(at);
CREATE TABLE IF NOT EXISTS responses(
  message_id TEXT PRIMARY KEY, session_id TEXT, at REAL, model TEXT, input INTEGER, output INTEGER,
  cache_read INTEGER, cache_write INTEGER, subagent INTEGER NOT NULL DEFAULT 0, cwd TEXT,
  shown INTEGER NOT NULL DEFAULT 0, agent TEXT);
CREATE INDEX IF NOT EXISTS responses_at ON responses(at);
CREATE INDEX IF NOT EXISTS responses_session ON responses(session_id, at);
CREATE TABLE IF NOT EXISTS prompts(session_id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(session_id, at));
CREATE TABLE IF NOT EXISTS marks(
  session_id TEXT NOT NULL, at REAL NOT NULL, place TEXT NOT NULL, tag TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(session_id, at, place, tag));
CREATE TABLE IF NOT EXISTS agents(
  session_id TEXT NOT NULL, agent_id TEXT NOT NULL, tool_use_id TEXT NOT NULL, PRIMARY KEY(session_id, agent_id));
CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS reloads(session_id TEXT NOT NULL, at REAL NOT NULL, chars INTEGER NOT NULL, PRIMARY KEY(session_id, at));
CREATE TABLE IF NOT EXISTS compactions(session_id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(session_id, at));
CREATE TABLE IF NOT EXISTS positions(path TEXT PRIMARY KEY, read_to INTEGER NOT NULL, seam TEXT, size INTEGER);
CREATE TABLE IF NOT EXISTS standing(
  folder TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, tokens INTEGER NOT NULL,
  PRIMARY KEY(folder, at, kind, name));
CREATE TABLE IF NOT EXISTS telemetry(
  minute INTEGER NOT NULL, session_id TEXT NOT NULL DEFAULT '', account TEXT NOT NULL DEFAULT '',
  metric TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', query_source TEXT NOT NULL DEFAULT '',
  agent TEXT NOT NULL DEFAULT '', skill TEXT NOT NULL DEFAULT '', plugin TEXT NOT NULL DEFAULT '',
  mcp_server TEXT NOT NULL DEFAULT '', mcp_tool TEXT NOT NULL DEFAULT '', value REAL NOT NULL,
  PRIMARY KEY(minute, session_id, account, metric, kind, model, query_source, agent, skill, plugin, mcp_server, mcp_tool));
"""


#: how long an OTLP exporter waits for an answer before it gives up on a request (OTEL_EXPORTER_OTLP_TIMEOUT,
#: 10 s by default), when env_block sets no other
EXPORTER_TIMEOUT_S = 10.0
#: how long the collector waits for another writer before it answers 503, which an OTLP exporter retries. A
#: reading pass holds the write lock while it reads one transcript, which can take seconds on a busy machine,
#: and a wait of 5 s ended in "database is locked" and a traceback, with that minute's figures lost.
#: ⛔ It stays under the exporter's timeout. A handler still waiting after the exporter gave up would save a
#: batch the exporter then sends again, and save_metrics adds on conflict, so it would count twice.
COLLECT_WAIT_S = 8.0


def connect(db: "Path | str | None" = None, *, timeout: float = 5) -> sqlite3.Connection:
    path = Path(db or DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.executescript(SCHEMA)
    older = bool(tables) and ("compactions" not in tables or "prompts" not in tables)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(responses)")]
    if "shown" not in columns:
        conn.execute("ALTER TABLE responses ADD COLUMN shown INTEGER NOT NULL DEFAULT 0")
        older = True
    if "agent" not in columns:
        conn.execute("ALTER TABLE responses ADD COLUMN agent TEXT")
        older = True
    if older:
        # A store from before these figures: each transcript is read once more to fill them, and the counts stay.
        conn.execute("DELETE FROM positions")
        conn.commit()
    return conn


# ------------------------------------------------------------------------------ the status line
def _number(v) -> "float | None":
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def limits_of(payload: dict) -> dict:
    """The limits in a statusline payload, each as {"percent", "resets_at"} with the reset in epoch
    seconds. A limit the payload does not carry is left out, and an empty result is unknown."""
    out = {}
    for name, w in (payload.get("rate_limits") or {}).items():
        if isinstance(w, dict) and _number(w.get("used_percentage")) is not None:
            out[name] = {"percent": float(w["used_percentage"]), "resets_at": _number(w.get("resets_at"))}
    return out


def reading(payload: dict, now: float) -> dict:
    """The row one statusline payload gives. Every field may be missing, and a missing one stays None."""
    cw = payload.get("context_window") or {}
    use = cw.get("current_usage") or {}
    parts = [_number(use.get(k)) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    model = payload.get("model") or {}
    cache = payload.get("prompt_cache") or {}
    miss = cache.get("last_miss_cause") or {}
    causes = miss.get("causes") if isinstance(miss, dict) else None
    return {"session_id": payload.get("session_id"), "at": now,
            "model": model.get("id") if isinstance(model, dict) else model,
            "cwd": (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd"),
            "context_tokens": int(sum(p for p in parts if p)) if any(p is not None for p in parts) else None,
            "context_pct": _number(cw.get("used_percentage")),
            "cost_usd": _number((payload.get("cost") or {}).get("total_cost_usd")),
            "limits": json.dumps(limits_of(payload)) if limits_of(payload) else None,
            "hit_ratio": _number(cache.get("hit_ratio")), "expected_rebuilds": _number(cache.get("expected_rebuilds")),
            "miss_causes": json.dumps(causes) if causes else None}


def _marker(session_id: str, db: "Path | str | None") -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    return Path(db or DB).parent / "meter-seen" / safe


def take(payload: dict, *, db: "Path | str | None" = None, every: float = EVERY, now: "float | None" = None,
         keep_payload: bool = True) -> bool:
    """Write one statusline reading, at most once per `every` seconds per session. Returns whether it wrote."""
    sid = payload.get("session_id")
    if not sid:
        return False
    now = time.time() if now is None else now
    mark = _marker(sid, db)
    try:
        if now - mark.stat().st_mtime < every:
            return False
    except OSError:
        mark.parent.mkdir(parents=True, exist_ok=True)
    row = reading(payload, now)
    # the payload names the transcript and the folders, and holds no message text
    row["payload"] = json.dumps(payload) if keep_payload else None
    with connect(db) as conn:
        conn.execute("INSERT OR REPLACE INTO statusline(session_id, at, model, cwd, context_tokens, context_pct, cost_usd, "
                     "limits, hit_ratio, expected_rebuilds, miss_causes, payload) VALUES (:session_id, :at, :model, :cwd, "
                     ":context_tokens, :context_pct, :cost_usd, :limits, :hit_ratio, :expected_rebuilds, :miss_causes, :payload)", row)
    conn.close()
    mark.touch()
    os.utime(mark, (now, now))
    return True


def statusline_main(stdin=None, stdout=None) -> int:
    """The status line command. Reads the payload, keeps it, prints nothing unless CLAUDE_OPS_METER_LINE
    is set, and never fails, since a traceback would show in every chat's status line."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        payload = json.loads(stdin.read() or "{}")
        take(payload)
        if os.environ.get("CLAUDE_OPS_METER_LINE"):
            r = reading(payload, time.time())
            print(f"{(r['context_tokens'] or 0) // 1000}k context", file=stdout)
    except Exception:  # noqa: BLE001 - see the docstring
        pass
    return 0


# ------------------------------------------------------------------------------ the transcripts
_USAGE = b'"usage"'
_RELOAD = b'"instructions"'
_COMPACTED = b'"compact_boundary"'
_QUEUED = b'"queued_command"'
_USER = b'"user"'
# a key a tool's result has and a prompt never does (inside a string a quote is escaped, so these match keys only)
_RESULT = b'"tool_use_id"'
_ATTACHMENT = b'"attachment"'


def _when(s) -> "float | None":
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse(raw: bytes) -> "dict | None":
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _response(d: dict, subagent: bool = False, agent: "str | None" = None) -> "dict | None":
    m = d.get("message")
    use = m.get("usage") if isinstance(m, dict) else None
    mid = m.get("id") if isinstance(m, dict) else None
    if not mid or not isinstance(use, dict):
        return None
    shown = sum(len(json.dumps(b)) for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") != "thinking")
    return {"message_id": mid, "session_id": d.get("sessionId"), "at": _when(d.get("timestamp")), "shown": shown,
            "model": m.get("model"), "cwd": d.get("cwd"), "subagent": int(subagent or bool(d.get("isSidechain"))),
            "input": int(use.get("input_tokens") or 0), "output": int(use.get("output_tokens") or 0),
            "cache_read": int(use.get("cache_read_input_tokens") or 0),
            "cache_write": int(use.get("cache_creation_input_tokens") or 0), "agent": agent}


def _keep(out: dict, new: dict) -> None:
    """One more line of a response: its largest figures are kept, with the time of its first line."""
    old = out.get(new["message_id"])
    if old:
        for k in ("input", "output", "cache_read", "cache_write"):
            new[k] = max(new[k], old[k])
        new["at"] = old["at"] or new["at"]
        new["shown"] += old["shown"]
    out[new["message_id"]] = new


def responses_in(raw_lines, *, subagent: bool = False) -> dict:
    """The responses in some transcript lines, by message id. A response is written as several lines
    repeating one usage, whose output count grows, so the largest figures of each are kept."""
    out: dict[str, dict] = {}
    for raw in raw_lines:
        d = _parse(raw) if _USAGE in raw else None
        new = _response(d, subagent) if d else None
        if new:
            _keep(out, new)
    return out


_SAVE = ("INSERT INTO responses(message_id, session_id, at, model, input, output, cache_read, cache_write, subagent, cwd) "
         "VALUES (:message_id, :session_id, :at, :model, :input, :output, :cache_read, :cache_write, :subagent, :cwd) "
         "ON CONFLICT(message_id) DO UPDATE SET input=max(input, excluded.input), output=max(output, excluded.output), "
         "cache_read=max(cache_read, excluded.cache_read), cache_write=max(cache_write, excluded.cache_write), "
         "subagent=min(subagent, excluded.subagent)")


_SHOWN = "UPDATE responses SET shown = max(shown, ?) WHERE message_id = ?"
_AGENT = "UPDATE responses SET agent = coalesce(agent, ?) WHERE message_id = ?"


def _reload(d: dict) -> "tuple | None":
    at = d.get("attachment") if d.get("type") == "attachment" else None
    when = _when(d.get("timestamp")) if isinstance(at, dict) and at.get("type") == "instructions" else None
    return (d["sessionId"], when, len(json.dumps(at))) if when is not None and d.get("sessionId") else None


def reloads_in(raw_lines) -> list[tuple]:
    """The instruction files a conversation was handed again, each as (session, time, characters). No text is kept."""
    out = []
    for raw in raw_lines:
        d = _parse(raw) if _RELOAD in raw else None
        got = _reload(d) if d else None
        if got:
            out.append(got)
    return out


def _compaction(d: dict) -> "tuple | None":
    if d.get("type") == "system" and d.get("subtype") == "compact_boundary" and d.get("sessionId"):
        when = _when(d.get("timestamp"))
        return (d["sessionId"], when) if when is not None else None
    return None


def compactions_in(raw_lines) -> list[tuple]:
    """Each compaction a conversation's lines record, as (session, time)."""
    out = []
    for raw in raw_lines:
        d = _parse(raw) if _COMPACTED in raw else None
        got = _compaction(d) if d else None
        if got:
            out.append(got)
    return out


# ------------------------------------------------------------------------------ what each turn names
MARKS = Path(os.environ.get("CLAUDE_OPS_METER_MARKS") or Path.home() / ".config" / "claude-ops" / "meter-marks.json")
#: what an id looks like unless the configuration says otherwise: one token, never a piece of text
ID = r"[A-Za-z0-9_.:/#-]{1,100}"
#: the tools whose call starts work that runs on after it, besides any call run in the background
STARTERS = ("Agent", "Task")
_NOTICE = re.compile(r"<tool-use-id>([A-Za-z0-9_-]{1,100})</tool-use-id>")


class Marks:
    """Which work items a chat's turns name, as the caller sets them.

    `prompt` is a regular expression looked for in the text of each prompt, whose first group is an id (a pasted
    ticket that starts with "Ticket ABC-12", say). `calls` maps a tool's name to the fields of its input that hold
    an id. An id is kept only when the whole of it matches `id`. `skip` lists windows of a conversation, each
    {"session", "from", "to"} with ISO times, where nothing is marked, no prompt is kept and every response is
    unattributed. With no prompt and no
    calls, the reader still keeps the turns and the work started in the background, which name no item."""

    def __init__(self, prompt: str = "", calls: "dict | None" = None, id: str = ID, skip=()) -> None:  # noqa: A002
        self.prompt = re.compile(prompt) if prompt else None
        if self.prompt is not None and self.prompt.groups < 1:
            raise ValueError("the prompt pattern needs a group around the id")
        self.calls = {str(name): tuple(str(f) for f in fields) for name, fields in (calls or {}).items()}
        self.id = re.compile(id)
        self.skip = []
        for s in skip:
            start, end = (_when(s.get("from")), _when(s.get("to"))) if isinstance(s, dict) else (None, None)
            if start is None or end is None or not s.get("session"):
                raise ValueError(f"a skipped window needs a session, a from and a to: {s!r}")
            self.skip.append((str(s["session"]), start, end))
        self.fingerprint = json.dumps({"prompt": prompt, "calls": {k: list(v) for k, v in self.calls.items()}, "id": id,
                                       "skip": [list(s) for s in self.skip]}, sort_keys=True)

    @classmethod
    def load(cls, path: "Path | str | None" = None) -> "Marks":
        """The configuration in `path` (CLAUDE_OPS_METER_MARKS, or ~/.config/claude-ops/meter-marks.json): a JSON
        object with the same names. No file marks no ids, and a file that cannot be read is refused."""
        p = Path(path or MARKS)
        try:
            d = json.loads(p.read_text())
        except FileNotFoundError:
            return cls()
        if not isinstance(d, dict):
            raise ValueError(f"{p} does not hold an object")
        return cls(prompt=d.get("prompt") or "", calls=d.get("calls") or {}, id=d.get("id") or ID, skip=d.get("skip") or ())

    def skipped(self, session_id: str, at: float) -> bool:
        return any(s == session_id and start <= at < end for s, start, end in self.skip)

    def ids(self, text: str) -> list[str]:
        if self.prompt is None:
            return []
        return [m.group(1) for m in self.prompt.finditer(text) if m.group(1) and self.id.fullmatch(m.group(1))]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _named(text: str, marks: Marks) -> list[tuple[str, str, str]]:
    got = [("prompt", tag, "") for tag in marks.ids(text)]
    if "<task-notification>" in text:
        got += [("notice", tag, "") for tag in _NOTICE.findall(text)]
    return got


def marks_of(d: dict, marks: Marks) -> tuple[bool, list[tuple[str, str, str]]]:
    """What one transcript line says about the turns: whether it is a prompt, which starts a turn, and its marks as
    (place, tag, message id). The places are `prompt` (an id in a prompt's text, or in a message that arrived while
    a turn ran), `call` (an id in a tool's input), `started` (a call whose work runs on after it, in the background
    or as a subagent, tagged with the call's own id) and `notice` (the notice that background work ended, tagged
    with the id of the call that started it). A subagent's own lines give nothing, since its spend goes to the call
    that started it."""
    if d.get("isSidechain"):
        return False, []
    kind = d.get("type")
    if kind == "user":
        content = (d.get("message") or {}).get("content")
        result = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        if d.get("isMeta") or d.get("isCompactSummary") or result:
            return False, []
        return True, _named(_text(content), marks)
    if kind == "attachment":
        a = d.get("attachment") or {}
        return False, _named(_text(a.get("prompt")), marks) if a.get("type") == "queued_command" else []
    out: list[tuple[str, str, str]] = []
    m = d.get("message") if kind == "assistant" else None
    for b in (m.get("content") or []) if isinstance(m, dict) else []:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        given = b.get("input") if isinstance(b.get("input"), dict) else {}
        for field in marks.calls.get(b.get("name"), ()):
            value = given.get(field)
            if isinstance(value, str) and marks.id.fullmatch(value.strip()):
                out.append(("call", value.strip(), m.get("id") or ""))
        if b.get("id") and (given.get("run_in_background") is True or b.get("name") in STARTERS):
            out.append(("started", str(b["id"]), m.get("id") or ""))
    return False, out


def _wants(raw: bytes) -> bool:
    """Whether a line can hold anything the reader keeps. A tool's result never does, and results are most of the bytes."""
    return (_USAGE in raw or _RELOAD in raw or _COMPACTED in raw or _QUEUED in raw
            or (_USER in raw and _RESULT not in raw and _ATTACHMENT not in raw))


def _started_by(path: Path) -> "str | None":
    """The call that started a subagent, from the note Claude Code writes beside its transcript."""
    try:
        meta = json.loads(path.with_name(path.name[:-len(".jsonl")] + ".meta.json").read_text())
    except (OSError, ValueError):
        return None
    use = meta.get("toolUseId") if isinstance(meta, dict) else None
    return str(use) if use else None


def read_file(conn: sqlite3.Connection, path: Path, marks: "Marks | None" = None) -> int:
    """The responses a transcript gained since the last read, saved, with the prompts and marks of its turns
    (`marks_of`). It reads from the remembered position when the bytes before it still hash the same
    (sessions._seam), and from the start for a file replaced by another one. A line still being written is read
    again next time. The file is read one line at a time, so a large transcript is never held whole, and a tool's
    result is parsed only when it carries a usage figure. Returns the responses saved."""
    from .sessions import _seam  # noqa: PLC0415 - a status line render must not import this
    marks = Marks() if marks is None else marks
    st = path.stat()
    row = conn.execute("SELECT read_to, seam, size FROM positions WHERE path = ?", (str(path),)).fetchone()
    if row and row[2] == st.st_size:
        return 0
    side = path.parent.name == "subagents"
    agent = path.name[:-len(".jsonl")].removeprefix("agent-") if side else None
    found: dict = {}
    reloads, cuts, prompts, kept = [], [], [], []
    with path.open("rb") as fh:
        start = row[0] if row and row[0] <= st.st_size and _seam(fh, row[0]) == row[1] else 0
        fh.seek(start)
        read_to = start
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            read_to += len(raw)
            d = _parse(raw) if _wants(raw) else None
            if d is None:
                continue
            new = _response(d, side, agent)
            if new:
                _keep(found, new)
            for got, into in ((_reload(d), reloads), (_compaction(d), cuts)):
                if got:
                    into.append(got)
            sid, at = d.get("sessionId"), _when(d.get("timestamp"))
            if side or not sid or at is None or marks.skipped(sid, at):
                continue
            prompt, named = marks_of(d, marks)
            if prompt:
                prompts.append((sid, at))
            kept.extend((sid, at, place, tag, mid) for place, tag, mid in named)
        seam = _seam(fh, read_to)
    if found:
        conn.executemany(_SAVE, list(found.values()))
        conn.executemany(_SHOWN, [(r["shown"], r["message_id"]) for r in found.values()])
        conn.executemany(_AGENT, [(r["agent"], r["message_id"]) for r in found.values() if r["agent"]])
    conn.executemany("INSERT OR REPLACE INTO reloads(session_id, at, chars) VALUES (?, ?, ?)", reloads)
    conn.executemany("INSERT OR REPLACE INTO compactions(session_id, at) VALUES (?, ?)", cuts)
    conn.executemany("INSERT OR IGNORE INTO prompts(session_id, at) VALUES (?, ?)", prompts)
    conn.executemany("INSERT OR IGNORE INTO marks(session_id, at, place, tag, message_id) VALUES (?, ?, ?, ?, ?)", kept)
    use = _started_by(path) if side else None
    if use:
        conn.execute("INSERT OR REPLACE INTO agents(session_id, agent_id, tool_use_id) VALUES (?, ?, ?)",
                     (path.parent.parent.name, agent, use))
    conn.execute("INSERT OR REPLACE INTO positions(path, read_to, seam, size) VALUES (?, ?, ?, ?)",
                 (str(path), read_to, seam, st.st_size))
    return len(found)


def _settle(conn: sqlite3.Connection, marks: Marks) -> None:
    """Match the store to `marks`. When the configuration changed, the marks it decides are dropped (and the prompts
    in a window now skipped), and every transcript is read again from its start."""
    was = conn.execute("SELECT value FROM settings WHERE name = 'marks'").fetchone()
    if was and was[0] == marks.fingerprint:
        return
    conn.execute("DELETE FROM marks WHERE place IN ('prompt', 'call')")
    for sid, start, end in marks.skip:
        conn.execute("DELETE FROM marks WHERE session_id = ? AND at >= ? AND at < ?", (sid, start, end))
        conn.execute("DELETE FROM prompts WHERE session_id = ? AND at >= ? AND at < ?", (sid, start, end))
    conn.execute("DELETE FROM positions")
    conn.execute("INSERT OR REPLACE INTO settings(name, value) VALUES ('marks', ?)", (marks.fingerprint,))
    conn.commit()


def read_all(*, root: "Path | None" = None, db: "Path | str | None" = None, since: "float | None" = None,
             marks: "Marks | None" = None) -> dict:
    """Read every transcript under `root` that changed, a session's subagents included. With `since`, files
    last written before it are passed over. Each file is committed when it is read, so a read stopped halfway keeps
    what it finished. `marks` says what the turns name (`Marks.load()` when None). After it
    changes, every file is read again, and the ids in a file older than `since` come back with a read that has no
    `since`. Returns {"files", "responses"}."""
    from .paths import PROJECTS  # noqa: PLC0415
    root = Path(root or PROJECTS)
    marks = Marks.load() if marks is None else marks
    files = responses = 0
    conn = connect(db)
    try:
        _settle(conn, marks)
        for pattern in ("*/*.jsonl", "*/*/subagents/*.jsonl"):
            for path in root.glob(pattern):
                try:
                    if since is not None and path.stat().st_mtime < since:
                        continue
                    got = read_file(conn, path, marks)
                except OSError:
                    continue
                files += 1
                responses += got
                # each file on its own, so a read that is stopped keeps every file it finished
                conn.commit()
        conn.commit()
    finally:
        conn.close()
    return {"files": files, "responses": responses}


#: the client's own weighting of a model (its code, 2.1.316), a prior and not a contract
TIERS = (("fable", 10), ("mythos", 10), ("opus", 5), ("haiku", 1))
OTHER_TIER = 3


def tier(model: "str | None") -> int:
    m = (model or "").lower()
    return next((t for word, t in TIERS if word in m), OTHER_TIER)


def weighted(model: "str | None", input_: int, output: int, cache_read: int, cache_write: int) -> float:
    """One response in the client's weighting: an estimate, see the module's note."""
    return (cache_read + input_ * 10 + cache_write * 12.5 + output * 50) * tier(model)


# ------------------------------------------------------------------------------ telemetry
#: which OTLP attribute fills which column. A column is a name or a number, never a prompt.
ATTRS = {"session.id": "session_id", "user.account_uuid": "account", "type": "kind", "model": "model",
         "query_source": "query_source", "agent.name": "agent", "skill.name": "skill", "plugin.name": "plugin",
         "mcp_server.name": "mcp_server", "mcp_tool.name": "mcp_tool"}
COLUMNS = ("minute", "session_id", "account", "metric", "kind", "model", "query_source", "agent", "skill", "plugin",
           "mcp_server", "mcp_tool")
CUMULATIVE = 2


def _attrs(items) -> dict:
    out = {}
    for a in items or []:
        v = a.get("value") or {}
        val = next(iter(v.values()), None) if v else None
        if a.get("key") in ATTRS and val is not None:
            out[ATTRS[a["key"]]] = str(val)
    return out


def metric_rows(body: dict, seen: "dict | None" = None) -> list[dict]:
    """The data points of an OTLP metrics body, one row a point. A cumulative series is turned into its
    change since `seen` last held it, since the store sums by the minute."""
    rows = []
    seen = {} if seen is None else seen
    for rm in body.get("resourceMetrics") or []:
        base = _attrs((rm.get("resource") or {}).get("attributes"))
        for sm in rm.get("scopeMetrics") or []:
            for m in sm.get("metrics") or []:
                data = m.get("sum") or m.get("gauge") or {}
                for p in data.get("dataPoints") or []:
                    value = _number(p.get("asDouble"))
                    if value is None and p.get("asInt") is not None:
                        value = float(p["asInt"])
                    if value is None:
                        continue
                    row = {c: "" for c in COLUMNS}
                    row.update(base)
                    row.update(_attrs(p.get("attributes")))
                    row["metric"] = m.get("name") or ""
                    row["minute"] = int(int(p.get("timeUnixNano") or 0) / 1e9 // 60)
                    if data.get("aggregationTemporality") == CUMULATIVE:
                        key = tuple(row[c] for c in COLUMNS if c != "minute")
                        value, seen[key] = value - seen.get(key, 0.0), value
                    if value:
                        rows.append({**row, "value": value})
    return rows


def save_metrics(conn: sqlite3.Connection, rows: list[dict]) -> None:
    cols = ", ".join(COLUMNS)
    names = ", ".join(f":{c}" for c in COLUMNS)
    conn.executemany(f"INSERT INTO telemetry({cols}, value) VALUES ({names}, :value) "  # noqa: S608 - fixed names
                     f"ON CONFLICT({cols}) DO UPDATE SET value = value + excluded.value", rows)


def env_block(port: int = PORT) -> dict:
    """The environment that turns Claude Code's telemetry on and points it at the collector. It takes effect
    when a session starts. Project settings do not set these, so they go in the environment or managed settings."""
    return {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "none",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json", "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}",
            "OTEL_METRIC_EXPORT_INTERVAL": "60000", "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "true"}


MANAGED = Path("/Library/Application Support/ClaudeCode/managed-settings.json" if sys.platform == "darwin"
               else "/etc/claude-code/managed-settings.json")


def turn_on(*, path: "Path | str | None" = None, port: int = PORT, run=None, env: "dict | None" = None) -> bool:
    """Put telemetry's environment into this machine's managed settings, so each chat sends to the collector from
    its next start. Everything else in the file stays. Writing there takes root, so this goes through sudo with no
    prompt, and fails rather than wait for a password. An unreadable file is refused, never replaced. Returns
    whether the file changed."""
    import subprocess  # noqa: PLC0415 - a status line render must not import this
    run = run or subprocess.run
    path = Path(path or MANAGED)
    try:
        now = json.loads(path.read_text())
    except FileNotFoundError:
        now = {}
    if not isinstance(now, dict):
        raise ValueError(f"{path} does not hold a settings object")
    want = {**now, "env": {**(now.get("env") or {}), **env_block(port)}}
    if want == now:
        return False
    run(["sudo", "-n", "/bin/mkdir", "-p", str(path.parent)], check=True, env=env)
    run(["sudo", "-n", "/usr/bin/tee", str(path)], input=json.dumps(want, indent=2) + "\n", text=True, check=True,
        stdout=subprocess.DEVNULL, env=env)
    return True


def serve(*, host: str = "127.0.0.1", port: int = PORT, db: "Path | str | None" = None, server=None) -> None:
    """The one OTLP collector. It binds this machine only, takes http/json metrics, and answers anything else
    with a refusal the exporter logs."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def _answer(self, code: int, body: bytes = b"{}", headers: "dict | None" = None) -> None:
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if "json" not in (self.headers.get("Content-Type") or ""):
                return self._answer(415, b'{"error": "this collector takes http/json"}')
            if self.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            if self.path.rstrip("/") == "/v1/metrics":
                try:
                    rows = metric_rows(json.loads(raw), seen)
                except ValueError:
                    return self._answer(400, b'{"error": "not json"}')
                conn = None
                try:
                    # inside the try, so a lock met while connect sets the store up answers 503 too
                    conn = connect(db, timeout=COLLECT_WAIT_S)
                    save_metrics(conn, rows)
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e) and "busy" not in str(e):
                        raise
                    # nothing was saved, so the exporter may send the same minutes again
                    if conn is not None:
                        conn.rollback()
                    return self._answer(503, b'{"error": "the store is busy, send it again"}',
                                        {"Retry-After": str(int(COLLECT_WAIT_S))})
                finally:
                    if conn is not None:
                        conn.close()
            return self._answer(200)

    (server or ThreadingHTTPServer)((host, port), Handler).serve_forever()


# ------------------------------------------------------------------------------ what it knows
def prune(*, db: "Path | str | None" = None, keep_days: int = KEEP_DAYS, now: "float | None" = None) -> dict:
    """Drop status line, telemetry and standing rows past `keep_days`. Responses stay, see KEEP_DAYS."""
    cut = (time.time() if now is None else now) - keep_days * 86400
    conn = connect(db)
    try:
        a = conn.execute("DELETE FROM statusline WHERE at < ?", (cut,)).rowcount
        b = conn.execute("DELETE FROM telemetry WHERE minute < ?", (int(cut // 60),)).rowcount
        c = conn.execute("DELETE FROM standing WHERE at < ?", (cut,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return {"statusline": a, "telemetry": b, "standing": c}


def _cache_block(payload: "str | None") -> dict:
    """What a chat's newest payload says beyond the row's columns: its name, and the prompt-cache counters since
    its process started, among them the tokens a rewrite would cost if its cache went cold."""
    try:
        d = json.loads(payload) if payload else {}
    except ValueError:
        return {}
    pc = d.get("prompt_cache") or {}
    return {"session_name": d.get("session_name"), "cache_ttl": pc.get("ttl"), "cache_requests": pc.get("requests"),
            "cache_misses": pc.get("misses"), "misses_by_cause": pc.get("miss_causes") or None,
            "miss_recache_tokens": pc.get("miss_recache_tokens"), "recache_tokens_if_cold": pc.get("recache_tokens_if_cold")}


def _prefixes(conn: sqlite3.Connection, sessions: list[str]) -> list[tuple[str, int]]:
    """The context each session's first response paid for, which is its standing prefix. A subagent's is left out."""
    if not sessions:
        return []
    marks = ",".join("?" * len(sessions))
    return conn.execute(
        f"SELECT r.session_id, r.input + r.cache_read + r.cache_write FROM responses r JOIN "  # noqa: S608 - only marks
        f"(SELECT session_id, min(at) AS at FROM responses WHERE subagent = 0 AND session_id IN ({marks}) GROUP BY session_id) f "
        "ON r.session_id = f.session_id AND r.at = f.at WHERE r.subagent = 0", sessions).fetchall()


CHARS_PER_TOKEN = 3.0  # only to split a response's output into what it showed and what it thought
COMPACTED = 0.7  # a prompt that falls below this share of the one before it was cleared, with no compaction's row


def growth(conn: sqlite3.Connection, session_id: str) -> "dict | None":
    """Where a chat's prompt grew since its last compaction, from the responses' own usage. A call's prompt is
    the one before it, plus that response's output, plus what arrived in between, so `output_kept`, `arrived` and
    `reloaded` add up to `grown` exactly. `reloaded` is what arrived with instruction files read again (a resumed
    conversation is handed each file that changed, whole, and keeps it until it is compacted). `thinking` is
    estimated, the part of the output its visible blocks do not account for. A subagent's calls are left out. A
    compaction after the last response means nothing has grown since, so the answer is None until the chat responds."""
    rows = [r for r in conn.execute(
        "SELECT at, input + cache_read + cache_write, output, shown FROM responses "
        "WHERE session_id = ? AND subagent = 0 AND at IS NOT NULL ORDER BY at", (session_id,)) if r[1]]
    if not rows:
        return None
    cut = [r[0] for r in conn.execute("SELECT at FROM compactions WHERE session_id = ? ORDER BY at", (session_id,))]
    if cut and cut[-1] > rows[-1][0]:
        return None
    start = compactions = 0
    for i in range(1, len(rows)):
        if rows[i][1] < rows[i - 1][1] * COMPACTED or any(rows[i - 1][0] < m <= rows[i][0] for m in cut):
            start, compactions = i, compactions + 1
    marks = [r[0] for r in conn.execute("SELECT at FROM reloads WHERE session_id = ? ORDER BY at", (session_id,))]
    kept = shown = arrived = reloaded = reloads = 0
    stretch = rows[start:]
    for before, after in zip(stretch, stretch[1:]):
        came = after[1] - before[1] - before[2]
        kept += before[2]
        shown += min(before[2], before[3] / CHARS_PER_TOKEN)
        if any(before[0] < m <= after[0] for m in marks):
            reloaded += came
            reloads += 1
        else:
            arrived += came
    return {"since": stretch[0][0], "calls": len(stretch), "floor": stretch[0][1], "prompt": stretch[-1][1],
            "grown": stretch[-1][1] - stretch[0][1], "output_kept": kept, "thinking": round(kept - shown),
            "arrived": arrived, "reloaded": reloaded, "reloads": reloads, "compactions": compactions}


def per_call_around(session_id: str, at: float, *, calls: int = 10, db: "Path | str | None" = None) -> dict:
    """What a chat's calls cost on average just before and just after a moment, such as a compaction: each side's
    mean `weighted`, over up to `calls` of its own responses, and `change`, the share the cost per call rose (below 0
    when it fell), None until each side has `calls` responses or when they do not all share one model. A subagent's calls are left out, and so is the call
    that wrote the summary, which a transcript does not record."""
    conn = connect(db)
    try:
        q = "SELECT model, input, output, cache_read, cache_write FROM responses WHERE session_id = ? AND subagent = 0 "
        before = conn.execute(q + "AND at < ? ORDER BY at DESC LIMIT ?", (session_id, at, calls)).fetchall()
        after = conn.execute(q + "AND at >= ? ORDER BY at LIMIT ?", (session_id, at, calls)).fetchall()
    finally:
        conn.close()
    mean = lambda rows: sum(weighted(*r) for r in rows) / len(rows) if rows else None  # noqa: E731
    was, now = mean(before), mean(after)
    # a call's cost carries its model's tier, so a model changed on either side reads as a gain or a loss
    one_model = len({r[0] for r in before + after}) == 1
    known = len(before) >= calls and len(after) >= calls and was and one_model
    return {"before": was, "after": now, "calls_before": len(before), "calls_after": len(after),
            "change": now / was - 1 if known else None}


def chat_day(session_id: str, *, db: "Path | str | None" = None, since: "float | None" = None, until: "float | None" = None,
             now: "float | None" = None) -> dict:
    """One chat over a window: the prompt at each of its own responses, its compactions, what it and its subagents
    spent in the report's weighting, and where its prompt grew. A subagent's prompt is its own, so the curve leaves
    it out. An empty window gives an empty curve and no spend, never an error. `unknown_since` is a compaction the chat
    has not responded since: the curve's last point is from before it, and the prompt is unknown until the next response."""
    now = time.time() if now is None else now
    since = now - 86400 if since is None else since
    until = now if until is None else until
    conn = connect(db)
    try:
        curve: list[tuple[float, int]] = []
        chat, subagents = {"responses": 0, "weighted": 0.0}, {"responses": 0, "weighted": 0.0}
        for at, model, input_, output, read, write, sub in conn.execute(
                "SELECT at, model, input, output, cache_read, cache_write, subagent FROM responses "
                "WHERE session_id = ? AND at >= ? AND at < ? ORDER BY at", (session_id, since, until)):
            side = subagents if sub else chat
            side["responses"] += 1
            side["weighted"] += weighted(model, input_, output, read, write)
            if not sub and input_ + read + write:
                curve.append((at, input_ + read + write))
        marks = [r[0] for r in conn.execute("SELECT at FROM compactions WHERE session_id = ? AND at >= ? AND at < ? ORDER BY at",
                                            (session_id, since, until))]
        unknown_since = marks[-1] if marks and (not curve or marks[-1] > curve[-1][0]) else None
        return {"session_id": session_id, "since": since, "until": until, "curve": curve, "compactions": marks, "unknown_since": unknown_since,
                "chat": chat, "subagents": subagents, "growth": growth(conn, session_id)}
    finally:
        conn.close()


#: what a window's spend went on
CAUSES = ("reread", "expired", "output", "written", "fresh")


def by_cause(*, db: "Path | str | None" = None, since: "float | None" = None, until: "float | None" = None,
             now: "float | None" = None) -> dict:
    """Where a window's spend went, per chat and per cause, in the report's weighting. The causes add up to it. `reread`
    is the conversation read back from cache on each call, `output` is what the responses wrote, thinking included, and
    `written` is what each call added to the cache. A cache write larger than the read beside it replaced the cached
    prefix. That is `fresh` for a conversation's first response in the window, a subagent's, or the first after a
    compaction, and `expired` otherwise, because the prefix ran out while the chat sat idle. Which of the two is judged
    from the shape of the calls, so the split between them is estimated. The window defaults to the last 7 days."""
    now = time.time() if now is None else now
    since = now - 7 * 86400 if since is None else since
    until = now if until is None else until
    conn = connect(db)
    try:
        cuts: dict = {}
        for sid, at in conn.execute("SELECT session_id, at FROM compactions WHERE at >= ? AND at < ?", (since, until)):
            cuts.setdefault(sid, []).append(at)
        chats: dict = {}
        before: dict = {}
        for sid, at, model, input_, output, read, write, sub in conn.execute(
                "SELECT session_id, at, model, input, output, cache_read, cache_write, subagent FROM responses "
                "WHERE at >= ? AND at < ? ORDER BY session_id, at", (since, until)):
            c = chats.setdefault(sid or "", {"session_id": sid, "responses": 0, "weighted": 0.0, "causes": dict.fromkeys(CAUSES, 0.0)})
            prompt, last = input_ + read + write, before.get(sid)
            cause = "written"
            if write > read:
                over = sub or last is None or prompt < last[1] * COMPACTED or any(last[0] < m <= at for m in cuts.get(sid, ()))
                cause = "fresh" if over else "expired"
            if not sub and prompt:
                before[sid] = (at, prompt)
            parts = {"reread": weighted(model, input_, 0, read, 0), "output": weighted(model, 0, output, 0, 0), cause: weighted(model, 0, 0, 0, write)}
            for name, part in parts.items():
                c["causes"][name] += part
            c["responses"] += 1
            c["weighted"] += sum(parts.values())
        causes = {name: sum(c["causes"][name] for c in chats.values()) for name in CAUSES}
        return {"since": since, "until": until, "responses": sum(c["responses"] for c in chats.values()), "weighted": sum(causes.values()),
                "causes": causes, "chats": sorted(chats.values(), key=lambda c: -c["weighted"])}
    finally:
        conn.close()


_STANDING_TABLES = {"estimated usage by category": ("category", 0, 1), "memory files": ("file", 1, 2), "skills": ("skill", 0, 2)}
FIRST_CALLS_DAYS = 14
_UNIT = {"": 1, "k": 1000, "m": 1000000}


def _tokens_cell(cell: str) -> "int | None":
    """A count as /context prints it, such as 581, 29.9k, ~150 or < 20."""
    m = re.fullmatch(r"[~<> ]*([\d.]+)([kmKM]?)", cell.strip())
    return round(float(m.group(1)) * _UNIT[m.group(2).lower()]) if m else None


def standing_parts(text: str) -> list[tuple[str, str, int]]:
    """The tables of Claude Code's /context as (kind, name, tokens), where kind is category, file or skill."""
    parts, table = [], None
    for line in text.splitlines():
        if line.startswith("#"):
            table = _STANDING_TABLES.get(line.lstrip("# ").strip().lower())
        elif table and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            kind, name, tokens = table
            if len(cells) > max(name, tokens) and (n := _tokens_cell(cells[tokens])) is not None:
                parts.append((kind, cells[name], n))
    return parts


def read_standing(folder: "Path | str", *, db: "Path | str | None" = None, now: "float | None" = None, env: "dict | None" = None,
                  run=None, claude: str = "claude", timeout: float = 60.0) -> "list | None":
    """Ask Claude Code what a chat started in `folder` loads before its first message, and keep the answer. `/context`
    in print mode spends nothing, and its figures are Claude Code's own estimates. Hooks and MCP servers are off and the
    session is not saved, so the run leaves no trace and takes a second or two. `env` needs HOME and USER, since without
    its sign-in Claude Code falls back to a rougher estimate that came out 15% smaller. A folder that is gone or an answer
    with no tables keeps nothing and returns None, since a size that was not read is unknown and not zero."""
    import subprocess  # noqa: PLC0415 - a status line render must not import this
    run = run or subprocess.run
    if not Path(folder).is_dir():
        return None
    try:
        got = run([claude, "-p", "/context", "--no-session-persistence", "--strict-mcp-config", "--settings",
                   json.dumps({"disableAllHooks": True}), "--output-format", "json"], cwd=str(folder), env=env, capture_output=True, text=True, timeout=timeout,
                  stdin=subprocess.DEVNULL)
        parts = standing_parts(json.loads(got.stdout).get("result") or "")
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return None
    if not parts:
        return None
    now = time.time() if now is None else now
    conn = connect(db)
    try:
        conn.executemany("INSERT OR REPLACE INTO standing(folder, at, kind, name, tokens) VALUES (?, ?, ?, ?, ?)",
                         [(str(folder), now, *part) for part in parts])
        conn.commit()
    finally:
        conn.close()
    return parts


def standing(*, db: "Path | str | None" = None) -> dict:
    """The newest reading of each folder, as {folder: {"at": when, "parts": [(kind, name, tokens)]}}."""
    conn = connect(db)
    try:
        got: dict = {}
        for folder, at, kind, name, tokens in conn.execute(
                "SELECT s.folder, s.at, s.kind, s.name, s.tokens FROM standing s JOIN "
                "(SELECT folder, max(at) at FROM standing GROUP BY folder) n ON s.folder = n.folder AND s.at = n.at"):
            got.setdefault(folder, {"at": at, "parts": []})["parts"].append((kind, name, tokens))
        return got
    finally:
        conn.close()


def busiest_folders(*, db: "Path | str | None" = None, days: float = 7.0, limit: int = 8, now: "float | None" = None) -> list[str]:
    """The folders the most responses were made in over the last `days`, busiest first."""
    now = time.time() if now is None else now
    conn = connect(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT cwd FROM responses WHERE subagent = 0 AND at >= ? AND cwd IS NOT NULL AND cwd != '' "
            "GROUP BY cwd ORDER BY count(*) DESC LIMIT ?", (now - days * 86400, limit))]
    finally:
        conn.close()


def _known(reading: dict) -> tuple[list, int]:
    """A folder's parts that a person can trim, and their sum. Claude Code's estimate of the rest of what it loads
    leaves things out, so the rest comes from what first calls really paid."""
    named = [(kind, name, tokens) for kind, name, tokens in reading["parts"] if kind == "file" or (kind, name) == ("category", "Skills")]
    return named, sum(p[2] for p in named)


def carrying(*, db: "Path | str | None" = None, since: "float | None" = None, until: "float | None" = None,
             now: "float | None" = None) -> dict:
    """What the prefix every call reads again costs over a window, the last 7 days by default, split into its parts,
    from the newest reading of each folder (`read_standing`). A call reads a part from cache, and a call whose cache
    write is larger than its read wrote it. `weighted` is the report's weighting and `equivalents` the same tokens at
    cache read 0.1 and cache write 1.25 with no tier. `rest` is the median of what a conversation's first call paid
    beyond the named parts over the last FIRST_CALLS_DAYS, which holds the tool schemas, the system prompt, text from
    hooks and the first message. A conversation whose folder has no reading, perhaps because it is gone, is compared
    with the busiest folder's parts. Only chats in a folder with a reading are priced, and `responses_read` says how
    many responses that is out of `responses`. Subagents load less and are left out of the carrying, and counted in
    `weighted`."""
    import statistics  # noqa: PLC0415 - a status line render must not import this
    now = time.time() if now is None else now
    since = now - 7 * 86400 if since is None else since
    until = now if until is None else until
    readings = standing(db=db)
    known = {folder: _known(r) for folder, r in readings.items()}
    conn = connect(db)
    try:
        pace: dict = {}
        total = 0.0
        responses = 0
        for cwd, model, input_, output, read, write, sub in conn.execute(
                "SELECT cwd, model, input, output, cache_read, cache_write, subagent FROM responses WHERE at >= ? AND at < ?", (since, until)):
            total += weighted(model, input_, output, read, write)
            if sub:
                continue
            responses += 1
            if cwd in known:
                wrote = write > read
                p = pace.setdefault(cwd, {"responses": 0, "weighted": 0.0, "equivalents": 0.0})
                p["responses"] += 1
                p["weighted"] += weighted(model, 0, 0, 0, 1) if wrote else weighted(model, 0, 0, 1, 0)
                p["equivalents"] += 1.25 if wrote else 0.1
        firsts = conn.execute(
            "SELECT r.cwd, r.input + r.cache_read + r.cache_write FROM responses r JOIN "
            "(SELECT session_id, min(at) first FROM responses WHERE subagent = 0 GROUP BY session_id) f "
            "ON r.session_id = f.session_id AND r.at = f.first WHERE r.subagent = 0 AND f.first >= ? AND f.first < ?",
            (until - FIRST_CALLS_DAYS * 86400, until)).fetchall()
    finally:
        conn.close()
    paid = [row[1] for row in firsts]
    busiest = max(pace, key=lambda f: pace[f]["responses"], default=None)
    beyond = [max(prompt - known[cwd if cwd in known else busiest][1], 0) for cwd, prompt in firsts] if busiest else []
    rest = round(statistics.median(beyond)) if beyond else None
    parts: dict = {}
    for folder, (named, _sum) in known.items():
        for kind, name, tokens in named:
            parts.setdefault((kind, name), {}).update({folder: tokens})
    if rest is not None:
        parts[("rest", "rest")] = {folder: rest for folder in known}
    rows = []
    for (kind, name), folders in parts.items():
        got = {"kind": "skills" if kind == "category" else kind, "name": name, "tokens": max(folders.values()), "folders": folders, "weighted": 0.0, "equivalents": 0.0}
        for folder, tokens in folders.items():
            p = pace.get(folder)
            if p:
                got["weighted"] += tokens * p["weighted"]
                got["equivalents"] += tokens * p["equivalents"]
        rows.append(got)
    rows.sort(key=lambda r: -r["weighted"])
    return {"since": since, "until": until, "weighted": total, "responses": responses, "responses_read": sum(p["responses"] for p in pace.values()),
            "folders": {f: {"at": readings[f]["at"], "responses": pace.get(f, {}).get("responses", 0)} for f in readings},
            "first_call": round(statistics.median(paid)) if paid else None, "first_calls": len(paid), "rest": rest,
            "carried": {"weighted": sum(r["weighted"] for r in rows), "equivalents": sum(r["equivalents"] for r in rows)}, "parts": rows,
            "skills": sorted(((n, t) for k, n, t in readings[busiest]["parts"] if k == "skill"), key=lambda s: -s[1]) if busiest else []}


def report(*, db: "Path | str | None" = None, since: "float | None" = None, until: "float | None" = None,
           fresh_s: float = 600.0, now: "float | None" = None) -> dict:
    """Per chat over a window: responses, tokens and the weighted figure (measured, and estimated for the
    weight), with the newest official status line beside them, and each chat's share. `fresh` counts the
    chats whose status line is under `fresh_s` old, since coverage is itself unknown otherwise."""
    now = time.time() if now is None else now
    since = now - 86400 if since is None else since
    until = now if until is None else until
    conn = connect(db)
    try:
        chats: dict[str, dict] = {}
        for sid, model, input_, output, read, write, sub in conn.execute(
                "SELECT session_id, model, input, output, cache_read, cache_write, subagent FROM responses WHERE at >= ? AND at < ?",
                (since, until)):
            c = chats.setdefault(sid or "", {"session_id": sid, "responses": 0, "subagent_responses": 0, "input": 0,
                                             "output": 0, "cache_read": 0, "cache_write": 0, "weighted": 0.0, "models": {}})
            c["responses"] += 1
            c["subagent_responses"] += sub
            c["input"] += input_
            c["output"] += output
            c["cache_read"] += read
            c["cache_write"] += write
            w = weighted(model, input_, output, read, write)
            c["weighted"] += w
            c["models"][model or "unknown"] = c["models"].get(model or "unknown", 0.0) + w
        total = sum(c["weighted"] for c in chats.values())
        for c in chats.values():
            c["share"] = c["weighted"] / total if total else None
        latest = conn.execute(
            "SELECT s.session_id, s.at, s.model, s.context_tokens, s.context_pct, s.cost_usd, s.limits, s.hit_ratio, "
            "s.expected_rebuilds, s.miss_causes, s.payload FROM statusline s JOIN (SELECT session_id, max(at) AS at "
            "FROM statusline WHERE at < ? GROUP BY session_id) n ON s.session_id = n.session_id AND s.at = n.at",
            (until,)).fetchall()
        fresh = 0
        limits = None
        limits_at = None
        for sid, at, model, ctx, pct, cost, lim, hit, rebuilds, causes, payload in latest:
            if now - at < fresh_s:
                fresh += 1
            if lim and (limits_at is None or at > limits_at):
                limits, limits_at = json.loads(lim), at
            if sid in chats or since <= at:
                c = chats.setdefault(sid, {"session_id": sid, "responses": 0, "subagent_responses": 0, "input": 0, "output": 0,
                                           "cache_read": 0, "cache_write": 0, "weighted": 0.0, "models": {}, "share": None})
                c.update(statusline_at=at, model=model, context_tokens=ctx, context_pct=pct, cost_usd=cost, hit_ratio=hit,
                         expected_rebuilds=rebuilds, miss_causes=json.loads(causes) if causes else None,
                         **_cache_block(payload))
        for sid, prefix in _prefixes(conn, [k for k in chats if k]):
            chats[sid]["prefix_tokens"] = prefix
            chats[sid]["growth"] = growth(conn, sid)
        return {"since": since, "until": until, "weighted_total": total, "fresh_chats": fresh, "known_chats": len(latest),
                "official_limits": limits, "official_limits_at": limits_at,
                "chats": sorted(chats.values(), key=lambda c: -c["weighted"]),
                "labels": {"limits": "official", "tokens": "measured", "weighted": "estimated", "growth": "measured",
                           "thinking": "estimated"}}
    finally:
        conn.close()


# ------------------------------------------------------------------------------ what each response was for
#: how sure an answer is, surest first
KINDS = ("measured", "split", "shared", "linked", "unattributed")
COUNTS = ("input", "output", "cache_read", "cache_write")


class _Turns:
    """One conversation's turns and marks, to say which items a moment of it was for."""

    def __init__(self, conn: sqlite3.Connection, session_id: str, key) -> None:
        self.skips = _skips(conn, session_id)
        # a skipped window starts a turn of its own, so what follows it is not read as the turn before it
        self.starts = sorted({r[0] for r in conn.execute("SELECT at FROM prompts WHERE session_id = ?", (session_id,))}
                             | {start for start, _end in self.skips})
        first = dict(conn.execute("SELECT message_id, at FROM responses WHERE session_id = ? AND at IS NOT NULL", (session_id,)))
        self.started: dict[str, float] = {}
        self.notices: dict[int, list[str]] = {}
        named: dict[int, dict[float, set]] = {}
        for at, place, tag, mid in conn.execute("SELECT at, place, tag, message_id FROM marks WHERE session_id = ?", (session_id,)):
            # a call belongs to the response that made it, whose first line can come before the call's own line
            at = min(at, first.get(mid, at)) if mid else at
            if place == "started":
                self.started[tag] = at
            elif place == "notice":
                self.notices.setdefault(self.turn(at), []).append(tag)
            elif place in ("prompt", "call"):
                named.setdefault(self.turn(at), {}).setdefault(at, set()).add(key(tag))
        self.named = {turn: sorted(events.items()) for turn, events in named.items()}

    def turn(self, at: float) -> int:
        return bisect.bisect_right(self.starts, at) - 1

    def at(self, when: float, depth: int = 0) -> tuple[tuple, str]:
        """The items a moment was for, and how sure that is (KINDS)."""
        turn = self.turn(when)
        if turn < 0 or any(start <= when < end for start, end in self.skips):
            return (), "unattributed"
        events = self.named.get(turn)
        if events:
            every = set().union(*(items for _at, items in events))
            if len(every) == 1:
                return tuple(every), "measured"
            # the last item named at or before the moment, and before the first naming, the first named
            i = max(bisect.bisect_right([at for at, _items in events], when) - 1, 0)
            items = tuple(sorted(events[i][1]))
            return items, "split" if len(items) == 1 else "shared"
        got: set = set()
        if depth < 8:
            for tag in self.notices.get(turn, ()):
                if tag in self.started:
                    got.update(self.at(self.started[tag], depth + 1)[0])
        return (tuple(sorted(got)), "linked") if got else ((), "unattributed")


def _skips(conn: sqlite3.Connection, session_id: str) -> list[tuple[float, float]]:
    """The windows of a conversation the store was told to leave unmarked (`Marks.skip`)."""
    row = conn.execute("SELECT value FROM settings WHERE name = 'marks'").fetchone()
    try:
        skip = json.loads(row[0]).get("skip") if row else None
    except (ValueError, AttributeError):
        skip = None
    return [(float(start), float(end)) for sid, start, end in skip or () if sid == session_id]


def _window(since, until, now) -> tuple[float, float]:
    now = time.time() if now is None else now
    return (now - 86400 if since is None else since), (now if until is None else until)


def assignments(session_id: str, *, since: "float | None" = None, until: "float | None" = None, key=None,
                db: "Path | str | None" = None, now: "float | None" = None) -> list[dict]:
    """Which work items each of a chat's responses in a window was for, by what its turns name (see `marks_of`). A
    turn is the responses from one prompt to the next. When the turn names one item, each response is for it
    (`measured`). When it names several, each response is for the last item named at or before it, and the first
    named before that (`split`), shared evenly when that naming held several items at once, as a prompt that
    pasted several does (`shared`). A turn that names none but is a background job's notice is for what the turn
    that started the job was doing (`linked`). Any other response is `unattributed`, and so is every response in
    a skipped window (`Marks.skip`), which starts a turn of its own. A subagent's responses are placed where the
    call that started it was made. `key` maps a matched id to the one it stands for, for a scheme
    with short forms, and is the identity by default. Each answer gives the response's time, its turn (the prompt's
    time, None before the first), model, the four counts, whether a subagent made it, its items and its kind."""
    since, until = _window(since, until, now)
    key = key or (lambda tag: tag)
    conn = connect(db)
    try:
        turns = _Turns(conn, session_id, key)
        agents = dict(conn.execute("SELECT agent_id, tool_use_id FROM agents WHERE session_id = ?", (session_id,)))
        firsts = dict(conn.execute("SELECT agent, min(at) FROM responses WHERE session_id = ? AND agent IS NOT NULL "
                                   "GROUP BY agent", (session_id,)))
        rows = conn.execute("SELECT message_id, at, model, input, output, cache_read, cache_write, subagent, agent FROM responses "
                            "WHERE session_id = ? AND at >= ? AND at < ? ORDER BY at", (session_id, since, until)).fetchall()
    finally:
        conn.close()
    out = []
    for mid, at, model, input_, output, read, write, sub, agent in rows:
        # a subagent's spend goes where the call that started it was made, or failing that where it first answered
        where = turns.started.get(agents.get(agent), firsts.get(agent, at)) if agent else at
        items, kind = turns.at(where)
        turn = turns.turn(where)
        out.append({"message_id": mid, "at": at, "turn": turns.starts[turn] if turn >= 0 else None, "model": model,
                    "input": input_, "output": output, "cache_read": read, "cache_write": write, "subagent": bool(sub),
                    "items": list(items), "kind": kind})
    return out


def jobs(session_id: str, *, since: "float | None" = None, until: "float | None" = None,
         db: "Path | str | None" = None, now: "float | None" = None) -> list[dict]:
    """The work a chat's calls started to run on after them (run in the background, or a subagent), for the calls made
    in a window, oldest first. Each gives the call's id, when it was made, when its notice came back (None while none
    has, and for a subagent the chat waited for) and, for a subagent, its last response."""
    since, until = _window(since, until, now)
    conn = connect(db)
    try:
        first = dict(conn.execute("SELECT message_id, at FROM responses WHERE session_id = ? AND at IS NOT NULL", (session_id,)))
        noticed = dict(conn.execute("SELECT tag, min(at) FROM marks WHERE session_id = ? AND place = 'notice' GROUP BY tag",
                                    (session_id,)))
        last = dict(conn.execute("SELECT a.tool_use_id, max(r.at) FROM agents a JOIN responses r ON r.session_id = a.session_id "
                                 "AND r.agent = a.agent_id WHERE a.session_id = ? GROUP BY a.tool_use_id", (session_id,)))
        started = conn.execute("SELECT tag, at, message_id FROM marks WHERE session_id = ? AND place = 'started'",
                               (session_id,)).fetchall()
    finally:
        conn.close()
    out = []
    for tag, at, mid in started:
        at = min(at, first.get(mid, at)) if mid else at
        if since <= at < until:
            out.append({"tool_use_id": tag, "started": at, "noticed": noticed.get(tag), "last": last.get(tag)})
    return sorted(out, key=lambda j: j["started"])


def _shares(n: int, parts: int) -> list[int]:
    q, r = divmod(n, parts)
    return [q + (1 if i < r else 0) for i in range(parts)]


def _blank() -> dict:
    return {"responses": 0, **dict.fromkeys(COUNTS, 0), "weighted": 0.0}


def _add(block: dict, model: "str | None", counts) -> float:
    w = weighted(model, *counts)
    block["responses"] += 1
    for name, n in zip(COUNTS, counts):
        block[name] += n
    block["weighted"] += w
    return w


def attribute(session_id: str, *, since: "float | None" = None, until: "float | None" = None, key=None,
              db: "Path | str | None" = None, now: "float | None" = None) -> dict:
    """What a chat spent on each work item over a window, the last day by default, from `assignments`. Each item has
    the four counts, the weighted figure and how much of that each kind is, so a reader sees how sure it is. A
    response shared by several items is split evenly, the rest of a count going to the first items in order, and
    `responses` counts each response an item had a part in. The items and `unattributed` add up to `total`, the
    window's own counts, to the token, and those are what `report` gives for the chat."""
    since, until = _window(since, until, now)
    items: dict = {}
    unattributed, total = _blank(), _blank()
    kinds = dict.fromkeys(KINDS, 0.0)
    for r in assignments(session_id, since=since, until=until, key=key, db=db):
        counts = tuple(r[name] for name in COUNTS)
        _add(total, r["model"], counts)
        names = r["items"] or [None]
        for name, part in zip(names, zip(*(_shares(n, len(names)) for n in counts))):
            block = items.setdefault(name, {"item": name, **_blank(), "kinds": {}}) if name else unattributed
            w = _add(block, r["model"], part)
            kinds[r["kind"]] += w
            if name:
                block["kinds"][r["kind"]] = block["kinds"].get(r["kind"], 0.0) + w
    return {"session_id": session_id, "since": since, "until": until, "total": total, "kinds": kinds,
            "items": sorted(items.values(), key=lambda b: -b["weighted"]), "unattributed": unattributed}


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    what = argv[0] if argv else ""
    if what == "statusline":
        return statusline_main()
    raise SystemExit("usage: python3 -m claude_ops.meter statusline (the rest is `claude-ops meter`)")


if __name__ == "__main__":
    raise SystemExit(main())
