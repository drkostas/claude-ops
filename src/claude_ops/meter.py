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

Three states everywhere: a reading that is absent is unknown, never zero. The store lives at
~/.local/state/claude-ops/meter.sqlite3 unless CLAUDE_OPS_METER_DB says otherwise. It holds numbers and
names, never message text.
"""
from __future__ import annotations

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
  shown INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS responses_at ON responses(at);
CREATE INDEX IF NOT EXISTS responses_session ON responses(session_id, at);
CREATE TABLE IF NOT EXISTS reloads(session_id TEXT NOT NULL, at REAL NOT NULL, chars INTEGER NOT NULL, PRIMARY KEY(session_id, at));
CREATE TABLE IF NOT EXISTS compactions(session_id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(session_id, at));
CREATE TABLE IF NOT EXISTS positions(path TEXT PRIMARY KEY, read_to INTEGER NOT NULL, seam TEXT, size INTEGER);
CREATE TABLE IF NOT EXISTS telemetry(
  minute INTEGER NOT NULL, session_id TEXT NOT NULL DEFAULT '', account TEXT NOT NULL DEFAULT '',
  metric TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', query_source TEXT NOT NULL DEFAULT '',
  agent TEXT NOT NULL DEFAULT '', skill TEXT NOT NULL DEFAULT '', plugin TEXT NOT NULL DEFAULT '',
  mcp_server TEXT NOT NULL DEFAULT '', mcp_tool TEXT NOT NULL DEFAULT '', value REAL NOT NULL,
  PRIMARY KEY(minute, session_id, account, metric, kind, model, query_source, agent, skill, plugin, mcp_server, mcp_tool));
"""


def connect(db: "Path | str | None" = None) -> sqlite3.Connection:
    path = Path(db or DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.executescript(SCHEMA)
    older = bool(tables) and "compactions" not in tables
    if "shown" not in [r[1] for r in conn.execute("PRAGMA table_info(responses)")]:
        conn.execute("ALTER TABLE responses ADD COLUMN shown INTEGER NOT NULL DEFAULT 0")
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


def _when(s) -> "float | None":
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def responses_in(raw_lines, *, subagent: bool = False) -> dict:
    """The responses in some transcript lines, by message id. A response is written as several lines
    repeating one usage, whose output count grows, so the largest figures of each are kept."""
    out: dict[str, dict] = {}
    for raw in raw_lines:
        if _USAGE not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        m = d.get("message") if isinstance(d, dict) else None
        use = m.get("usage") if isinstance(m, dict) else None
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid or not isinstance(use, dict):
            continue
        shown = sum(len(json.dumps(b)) for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") != "thinking")
        new = {"message_id": mid, "session_id": d.get("sessionId"), "at": _when(d.get("timestamp")), "shown": shown,
               "model": m.get("model"), "cwd": d.get("cwd"), "subagent": int(subagent or bool(d.get("isSidechain"))),
               "input": int(use.get("input_tokens") or 0), "output": int(use.get("output_tokens") or 0),
               "cache_read": int(use.get("cache_read_input_tokens") or 0),
               "cache_write": int(use.get("cache_creation_input_tokens") or 0)}
        old = out.get(mid)
        if old:
            for k in ("input", "output", "cache_read", "cache_write"):
                new[k] = max(new[k], old[k])
            new["at"] = old["at"] or new["at"]
            new["shown"] += old["shown"]
        out[mid] = new
    return out


_SAVE = ("INSERT INTO responses(message_id, session_id, at, model, input, output, cache_read, cache_write, subagent, cwd) "
         "VALUES (:message_id, :session_id, :at, :model, :input, :output, :cache_read, :cache_write, :subagent, :cwd) "
         "ON CONFLICT(message_id) DO UPDATE SET input=max(input, excluded.input), output=max(output, excluded.output), "
         "cache_read=max(cache_read, excluded.cache_read), cache_write=max(cache_write, excluded.cache_write), "
         "subagent=min(subagent, excluded.subagent)")


_SHOWN = "UPDATE responses SET shown = max(shown, ?) WHERE message_id = ?"
_RELOAD = b'"instructions"'


def reloads_in(raw_lines) -> list[tuple]:
    """The instruction files a conversation was handed again, each as (session, time, characters). No text is kept."""
    out = []
    for raw in raw_lines:
        if _RELOAD not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        at = d.get("attachment") if isinstance(d, dict) and d.get("type") == "attachment" else None
        when = _when(d.get("timestamp")) if isinstance(at, dict) and at.get("type") == "instructions" else None
        if when is not None and d.get("sessionId"):
            out.append((d["sessionId"], when, len(json.dumps(at))))
    return out


_COMPACTED = b'"compact_boundary"'


def compactions_in(raw_lines) -> list[tuple]:
    """Each compaction a conversation's lines record, as (session, time)."""
    out = []
    for raw in raw_lines:
        if _COMPACTED not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "system" and d.get("subtype") == "compact_boundary" and d.get("sessionId"):
            when = _when(d.get("timestamp"))
            if when is not None:
                out.append((d["sessionId"], when))
    return out


def read_file(conn: sqlite3.Connection, path: Path) -> int:
    """The responses a transcript gained since the last read, saved. It reads from the remembered position
    when the bytes before it still hash the same (sessions._seam), and from the start for a file replaced
    by another one. A line still being written is read again next time. Returns the responses saved."""
    from .sessions import _seam  # noqa: PLC0415 - a status line render must not import this
    st = path.stat()
    row = conn.execute("SELECT read_to, seam, size FROM positions WHERE path = ?", (str(path),)).fetchone()
    if row and row[2] == st.st_size:
        return 0
    with path.open("rb") as fh:
        start = row[0] if row and row[0] <= st.st_size and _seam(fh, row[0]) == row[1] else 0
        fh.seek(start)
        whole, read_to = [], start
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            whole.append(raw)
            read_to += len(raw)
        seam = _seam(fh, read_to)
    found = responses_in(whole, subagent=path.parent.name == "subagents")
    if found:
        conn.executemany(_SAVE, list(found.values()))
        conn.executemany(_SHOWN, [(r["shown"], r["message_id"]) for r in found.values()])
    conn.executemany("INSERT OR REPLACE INTO reloads(session_id, at, chars) VALUES (?, ?, ?)", reloads_in(whole))
    conn.executemany("INSERT OR REPLACE INTO compactions(session_id, at) VALUES (?, ?)", compactions_in(whole))
    conn.execute("INSERT OR REPLACE INTO positions(path, read_to, seam, size) VALUES (?, ?, ?, ?)",
                 (str(path), read_to, seam, st.st_size))
    return len(found)


def read_all(*, root: "Path | None" = None, db: "Path | str | None" = None, since: "float | None" = None) -> dict:
    """Read every transcript under `root` that changed, a session's subagents included. With `since`, files
    last written before it are passed over. Returns {"files", "responses"}."""
    from .paths import PROJECTS  # noqa: PLC0415
    root = Path(root or PROJECTS)
    files = responses = 0
    conn = connect(db)
    try:
        for pattern in ("*/*.jsonl", "*/*/subagents/*.jsonl"):
            for path in root.glob(pattern):
                try:
                    if since is not None and path.stat().st_mtime < since:
                        continue
                    got = read_file(conn, path)
                except OSError:
                    continue
                files += 1
                responses += got
                if files % 50 == 0:
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

        def _answer(self, code: int, body: bytes = b"{}") -> None:
            self.send_response(code)
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
                conn = connect(db)
                try:
                    save_metrics(conn, rows)
                    conn.commit()
                finally:
                    conn.close()
            return self._answer(200)

    (server or ThreadingHTTPServer)((host, port), Handler).serve_forever()


# ------------------------------------------------------------------------------ what it knows
def prune(*, db: "Path | str | None" = None, keep_days: int = KEEP_DAYS, now: "float | None" = None) -> dict:
    """Drop status line and telemetry rows past `keep_days`. Responses stay, see KEEP_DAYS."""
    cut = (time.time() if now is None else now) - keep_days * 86400
    conn = connect(db)
    try:
        a = conn.execute("DELETE FROM statusline WHERE at < ?", (cut,)).rowcount
        b = conn.execute("DELETE FROM telemetry WHERE minute < ?", (int(cut // 60),)).rowcount
        conn.commit()
    finally:
        conn.close()
    return {"statusline": a, "telemetry": b}


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
    estimated, the part of the output its visible blocks do not account for. A subagent's calls are left out."""
    rows = [r for r in conn.execute(
        "SELECT at, input + cache_read + cache_write, output, shown FROM responses "
        "WHERE session_id = ? AND subagent = 0 AND at IS NOT NULL ORDER BY at", (session_id,)) if r[1]]
    if not rows:
        return None
    cut = [r[0] for r in conn.execute("SELECT at FROM compactions WHERE session_id = ? ORDER BY at", (session_id,))]
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
    when it fell), None until each side has `calls` responses. A subagent's calls are left out, and so is the call
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
    known = len(before) >= calls and len(after) >= calls and was
    return {"before": was, "after": now, "calls_before": len(before), "calls_after": len(after),
            "change": now / was - 1 if known else None}


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


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    what = argv[0] if argv else ""
    if what == "statusline":
        return statusline_main()
    raise SystemExit("usage: python3 -m claude_ops.meter statusline (the rest is `claude-ops meter`)")


if __name__ == "__main__":
    raise SystemExit(main())
