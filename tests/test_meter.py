"""meter: the status line kept at most once a minute, a response counted once however many lines repeat
it, a transcript read from its position, telemetry through the collector, and the report. All data invented."""
import gzip
import io
import json
import pathlib
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request

from claude_ops import meter

NOW = 1_800_000_000.0
PAYLOAD = {"session_id": "s-1", "model": {"id": "claude-opus-9", "display_name": "Opus"},
           "workspace": {"current_dir": "/tmp/project"},
           "context_window": {"used_percentage": 12.5, "current_usage": {"input_tokens": 10, "output_tokens": 500,
                              "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 2000}},
           "cost": {"total_cost_usd": 1.25},
           "rate_limits": {"five_hour": {"used_percentage": 4, "resets_at": NOW + 100},
                           "seven_day": {"used_percentage": 19.5, "resets_at": NOW + 1000}, "nothing": {"resets_at": 1}},
           "prompt_cache": {"hit_ratio": 0.97, "expected_rebuilds": 1, "last_miss_cause": {"causes": ["tools_changed"]}}}


def rows(db, q="SELECT * FROM statusline"):
    conn = meter.connect(db)
    try:
        return conn.execute(q).fetchall()
    finally:
        conn.close()


def test_a_reading_keeps_the_figures_and_a_missing_one_stays_unknown():
    r = meter.reading(PAYLOAD, NOW)
    assert (r["session_id"], r["model"], r["cwd"], r["context_tokens"], r["context_pct"], r["cost_usd"]) == \
        ("s-1", "claude-opus-9", "/tmp/project", 102010, 12.5, 1.25)
    assert json.loads(r["limits"]) == {"five_hour": {"percent": 4.0, "resets_at": NOW + 100},
                                       "seven_day": {"percent": 19.5, "resets_at": NOW + 1000}}
    assert (r["hit_ratio"], r["expected_rebuilds"], json.loads(r["miss_causes"])) == (0.97, 1.0, ["tools_changed"])
    bare = meter.reading({"session_id": "s-2", "context_window": {"current_usage": None}}, NOW)
    assert all(bare[k] is None for k in ("model", "context_tokens", "context_pct", "cost_usd", "limits", "hit_ratio",
                                         "expected_rebuilds", "miss_causes")), "absent is unknown, never 0"


def test_the_status_line_is_written_at_most_once_a_minute_per_session(tmp_path):
    db = tmp_path / "m.sqlite3"
    assert meter.take(PAYLOAD, db=db, now=NOW) is True
    assert meter.take(PAYLOAD, db=db, now=NOW + 59) is False
    assert meter.take({**PAYLOAD, "session_id": "s-2"}, db=db, now=NOW + 59) is True
    assert meter.take(PAYLOAD, db=db, now=NOW + 60) is True
    assert meter.take({}, db=db, now=NOW) is False
    assert len(rows(db)) == 3


def test_the_status_line_command_prints_nothing_and_never_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(meter, "DB", tmp_path / "m.sqlite3")
    out = io.StringIO()
    assert meter.statusline_main(io.StringIO(json.dumps(PAYLOAD)), out) == 0
    assert out.getvalue() == "" and len(rows(tmp_path / "m.sqlite3")) == 1
    assert meter.statusline_main(io.StringIO("not json"), out) == 0 and out.getvalue() == ""
    monkeypatch.setenv("CLAUDE_OPS_METER_LINE", "1")
    assert meter.statusline_main(io.StringIO(json.dumps(PAYLOAD)), out) == 0 and "102k context" in out.getvalue()


def line(mid, output, *, sid="s-1", model="claude-opus-9", at="2027-01-15T08:00:00Z", read=1000, write=10, side=False):
    return (json.dumps({"type": "assistant", "sessionId": sid, "timestamp": at, "cwd": "/tmp/project", "isSidechain": side,
                        "message": {"id": mid, "model": model, "content": [{"type": "text", "text": "words"}],
                                    "usage": {"input_tokens": 2, "output_tokens": output, "cache_read_input_tokens": read,
                                              "cache_creation_input_tokens": write}}}) + "\n").encode()


def test_a_response_written_on_several_lines_counts_once():
    got = meter.responses_in([line("m1", 5), line("m1", 90), line("m1", 40), line("m2", 7),
                              b'{"type": "user", "message": {"content": "usage"}}\n', b"not json usage\n"])
    assert sorted(got) == ["m1", "m2"] and got["m1"]["output"] == 90 and got["m1"]["cache_read"] == 1000
    assert "words" not in json.dumps(got), "no message text is kept"


def test_a_transcript_is_read_from_its_position_and_again_when_replaced(tmp_path):
    conn = meter.connect(tmp_path / "m.sqlite3")
    f = tmp_path / "a.jsonl"
    f.write_bytes(line("m1", 5) + line("m1", 9))
    assert meter.read_file(conn, f) == 1
    assert meter.read_file(conn, f) == 0, "a file that did not change is not read"
    start = conn.execute("SELECT read_to FROM positions").fetchone()[0]
    with f.open("ab") as fh:
        fh.write(line("m2", 3) + line("m3", 3)[:40])
    assert meter.read_file(conn, f) == 1
    assert conn.execute("SELECT read_to FROM positions").fetchone()[0] == start + len(line("m2", 3)), \
        "a line still being written is read next time"
    with f.open("ab") as fh:
        fh.write(line("m3", 3)[40:])
    assert meter.read_file(conn, f) == 1
    assert conn.execute("SELECT count(*), sum(output) FROM responses").fetchone() == (3, 15)
    f.write_bytes(line("m9", 1) + b"x" * 5000 + b"\n")
    assert meter.read_file(conn, f) == 1, "a file replaced by another is read from its start"
    assert conn.execute("SELECT count(*) FROM responses").fetchone()[0] == 4
    conn.close()


def test_every_chat_and_its_subagents_are_read_and_a_moved_copy_counts_once(tmp_path):
    root, db = tmp_path / "projects", tmp_path / "m.sqlite3"
    (root / "p1" / "s-1" / "subagents").mkdir(parents=True)
    (root / "p2").mkdir()
    (root / "p1" / "s-1.jsonl").write_bytes(line("m1", 5))
    (root / "p1" / "s-1" / "subagents" / "agent-a.jsonl").write_bytes(line("m2", 5, side=True))
    (root / "p2" / "s-1.jsonl").write_bytes(line("m1", 5))
    got = meter.read_all(root=root, db=db)
    assert got["files"] == 3
    assert rows(db, "SELECT message_id, subagent FROM responses ORDER BY 1") == [("m1", 0), ("m2", 1)]
    assert meter.read_all(root=root, db=db)["responses"] == 0
    (root / "p1" / "s-9.jsonl").write_bytes(line("m9", 1))
    assert meter.read_all(root=root, db=db, since=time.time() + 60)["files"] == 0, "an older file is passed over"


def test_the_weighting_is_the_clients():
    assert [meter.tier(m) for m in ("claude-fable-9", "claude-opus-9", "claude-sonnet-9", "claude-haiku-9", None)] == [10, 5, 3, 1, 3]
    assert meter.weighted("claude-opus-9", 1, 2, 3, 4) == (3 + 10 + 50 + 100) * 5


def attr(k, v):
    return {"key": k, "value": {"stringValue": v}}


def metrics(value, *, kind="cacheRead", temporality=1, tool=None, nanos=int(NOW * 1e9)):
    return {"resourceMetrics": [{"resource": {"attributes": [attr("user.email", "a@example.org"), attr("user.account_uuid", "acc-1")]},
            "scopeMetrics": [{"metrics": [{"name": "claude_code.token.usage", "sum": {"aggregationTemporality": temporality, "dataPoints": [
                {"asDouble": value, "timeUnixNano": str(nanos),
                 "attributes": [attr("session.id", "s-1"), attr("type", kind), attr("model", "claude-opus-9"), attr("query_source", "main")]
                 + ([attr("mcp_tool.name", tool)] if tool else [])}]}}]}]}]}


def test_telemetry_rows_keep_names_and_numbers_and_no_person():
    r = meter.metric_rows(metrics(50.0, tool="search"))
    assert len(r) == 1 and r[0]["value"] == 50.0 and r[0]["session_id"] == "s-1" and r[0]["account"] == "acc-1"
    assert (r[0]["kind"], r[0]["model"], r[0]["query_source"], r[0]["mcp_tool"]) == ("cacheRead", "claude-opus-9", "main", "search")
    assert "a@example.org" not in json.dumps(r), "an email address is not a column"
    seen = {}
    assert meter.metric_rows(metrics(50.0, temporality=2), seen)[0]["value"] == 50.0
    assert meter.metric_rows(metrics(80.0, temporality=2), seen)[0]["value"] == 30.0, "a cumulative series becomes its change"
    assert meter.metric_rows(metrics(80.0, temporality=2), seen) == [], "and no change is no row"


def test_the_collector_binds_this_machine_sums_by_the_minute_and_refuses_another_format(tmp_path):
    db = tmp_path / "m.sqlite3"
    made = {}

    from http.server import ThreadingHTTPServer

    def factory(addr, handler):
        made["server"] = ThreadingHTTPServer((addr[0], 0), handler)
        made["addr"] = addr
        return made["server"]

    t = threading.Thread(target=meter.serve, kwargs={"db": db, "server": factory}, daemon=True)
    t.start()
    while "server" not in made:
        time.sleep(0.01)
    assert made["addr"][0] == "127.0.0.1"
    url = f"http://127.0.0.1:{made['server'].server_address[1]}/v1/metrics"

    def post(body, headers):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    js = {"Content-Type": "application/json"}
    assert post(json.dumps(metrics(10.0)).encode(), js) == 200
    assert post(gzip.compress(json.dumps(metrics(5.0)).encode()), {**js, "Content-Encoding": "gzip"}) == 200
    assert post(b"\x00\x01", {"Content-Type": "application/x-protobuf"}) == 415
    assert post(b"not json", js) == 400
    made["server"].shutdown()
    assert rows(db, "SELECT session_id, account, kind, value FROM telemetry") == [("s-1", "acc-1", "cacheRead", 15.0)]


def test_the_environment_turns_telemetry_on_toward_this_machine():
    e = meter.env_block(4999)
    assert e["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1" and e["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4999"
    assert e["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json" and e["OTEL_LOGS_EXPORTER"] == "none"


def _as_root(calls):
    """What sudo would do, on a test's own file."""
    def run(argv, **kw):
        calls.append(argv)
        assert argv[:2] == ["sudo", "-n"], "never a password prompt"
        if argv[2].endswith("tee"):
            pathlib.Path(argv[3]).write_text(kw["input"])
    return run


def test_turning_on_keeps_the_rest_of_the_file_and_writes_only_once(tmp_path):
    f = tmp_path / "managed-settings.json"
    f.write_text(json.dumps({"keep": 1, "env": {"OTHER": "x", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://old"}}))
    calls = []
    assert meter.turn_on(path=f, port=4999, run=_as_root(calls)) is True
    got = json.loads(f.read_text())
    assert got["keep"] == 1 and got["env"]["OTHER"] == "x"
    assert got["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4999" and got["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    calls.clear()
    assert meter.turn_on(path=f, port=4999, run=_as_root(calls)) is False and calls == [], "already on writes nothing"


def test_turning_on_makes_a_missing_file_and_refuses_one_it_cannot_read(tmp_path):
    f = tmp_path / "folder" / "managed-settings.json"
    calls = []
    f.parent.mkdir()
    assert meter.turn_on(path=f, run=_as_root(calls)) is True and calls[0][2].endswith("mkdir")
    assert json.loads(f.read_text())["env"]["OTEL_METRICS_EXPORTER"] == "otlp"
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    calls.clear()
    try:
        meter.turn_on(path=bad, run=_as_root(calls))
    except ValueError:
        pass
    else:
        raise AssertionError("a file it cannot read must not be replaced")
    assert calls == [] and bad.read_text() == "not json"


def test_the_report_labels_its_figures_and_counts_the_chats_it_can_see(tmp_path):
    db = tmp_path / "m.sqlite3"
    conn = meter.connect(db)
    conn.executemany(meter._SAVE, [
        {"message_id": "m1", "session_id": "s-1", "at": NOW - 10, "model": "claude-opus-9", "input": 0, "output": 0, "cache_read": 1000, "cache_write": 0, "subagent": 0, "cwd": "/x"},
        {"message_id": "m2", "session_id": "s-1", "at": NOW - 10, "model": "claude-opus-9", "input": 0, "output": 0, "cache_read": 1000, "cache_write": 0, "subagent": 1, "cwd": "/x"},
        {"message_id": "m3", "session_id": "s-2", "at": NOW - 10, "model": "claude-haiku-9", "input": 0, "output": 0, "cache_read": 10000, "cache_write": 0, "subagent": 0, "cwd": "/y"},
        {"message_id": "m4", "session_id": "s-3", "at": NOW - 90000, "model": "claude-opus-9", "input": 0, "output": 0, "cache_read": 9, "cache_write": 0, "subagent": 0, "cwd": "/z"}])
    conn.commit()
    conn.close()
    meter.take(PAYLOAD, db=db, now=NOW - 30)
    meter.take({"session_id": "s-old"}, db=db, now=NOW - 5000)
    r = meter.report(db=db, now=NOW)
    by = {c["session_id"]: c for c in r["chats"]}
    assert r["weighted_total"] == 20000.0 and set(by) == {"s-1", "s-2", "s-old"}
    assert by["s-1"]["responses"] == 2 and by["s-1"]["subagent_responses"] == 1 and by["s-1"]["share"] == 0.5
    assert by["s-1"]["context_tokens"] == 102010 and by["s-1"]["hit_ratio"] == 0.97 and by["s-1"]["miss_causes"] == ["tools_changed"]
    assert by["s-2"].get("context_tokens") is None, "no status line is unknown, not 0"
    assert (r["fresh_chats"], r["known_chats"]) == (1, 2)
    assert r["official_limits"]["seven_day"]["percent"] == 19.5 and r["labels"]["weighted"] == "estimated"


def test_the_report_gives_each_chats_name_cache_counters_and_prefix(tmp_path):
    db = tmp_path / "m.sqlite3"
    conn = meter.connect(db)
    conn.executemany(meter._SAVE, [
        {"message_id": "a", "session_id": "s-1", "at": NOW - 3000, "model": "claude-opus-9", "input": 5, "output": 1, "cache_read": 100, "cache_write": 900, "subagent": 0, "cwd": "/x"},
        {"message_id": "b", "session_id": "s-1", "at": NOW - 10, "model": "claude-opus-9", "input": 1, "output": 1, "cache_read": 5000, "cache_write": 0, "subagent": 0, "cwd": "/x"},
        {"message_id": "c", "session_id": "s-1", "at": NOW - 4000, "model": "claude-haiku-9", "input": 1, "output": 1, "cache_read": 7, "cache_write": 0, "subagent": 1, "cwd": "/x"}])
    conn.commit()
    conn.close()
    meter.take({**PAYLOAD, "session_name": "north", "prompt_cache": {"ttl": "1h", "requests": 9, "misses": 2, "hit_ratio": 0.9,
                "miss_causes": {"tools_changed": 2}, "miss_recache_tokens": 800, "recache_tokens_if_cold": 5000}}, db=db, now=NOW - 5)
    c = meter.report(db=db, now=NOW)["chats"][0]
    assert (c["session_name"], c["cache_ttl"], c["cache_requests"], c["cache_misses"]) == ("north", "1h", 9, 2)
    assert c["misses_by_cause"] == {"tools_changed": 2} and (c["miss_recache_tokens"], c["recache_tokens_if_cold"]) == (800, 5000)
    assert c["prefix_tokens"] == 1005, "the first response of the chat itself, a subagent's left out"
    assert c["growth"]["floor"] == 1005 and c["growth"]["prompt"] == 5001, "the chat's own calls, in the report"
    meter.take({"session_id": "s-3", "prompt_cache": {}}, db=db, now=NOW - 4)
    bare = {x["session_id"]: x for x in meter.report(db=db, now=NOW)["chats"]}["s-3"]
    assert bare["session_name"] is None and bare["cache_requests"] is None and bare.get("prefix_tokens") is None


def _said(mid, at, input_, read, write, output, *blocks, session="s-1"):
    return json.dumps({"type": "assistant", "sessionId": session, "timestamp": at, "message": {
        "id": mid, "model": "claude-opus-9", "content": list(blocks),
        "usage": {"input_tokens": input_, "output_tokens": output, "cache_read_input_tokens": read,
                  "cache_creation_input_tokens": write}}}).encode() + b"\n"


def _handed_files(at, chars=3000, session="s-1"):
    return json.dumps({"type": "attachment", "sessionId": session, "timestamp": at,
                       "attachment": {"type": "instructions", "content": "x" * chars}}).encode() + b"\n"


def test_growth_splits_a_prompt_since_its_compaction_and_adds_up(tmp_path):
    f = tmp_path / "a.jsonl"
    think = {"type": "thinking", "thinking": "", "signature": "z" * 50}
    call = {"type": "tool_use", "name": "Bash", "input": {"command": "y" * 280}}
    f.write_bytes(b"".join([
        _said("old", "2027-01-15T08:00:00Z", 1, 900000, 100, 500),
        _said("floor", "2027-01-15T09:00:00Z", 1, 30000, 120000, 1000, think, call),
        _said("two", "2027-01-15T09:01:00Z", 1, 150000, 1500, 400, think),
        _handed_files("2027-01-15T09:02:00Z"),
        _said("three", "2027-01-15T09:02:30Z", 1, 151900, 40000, 200),
        _said("four", "2027-01-15T09:03:00Z", 1, 191900, 3000, 100),
    ]))
    conn = meter.connect(tmp_path / "m.sqlite3")
    meter.read_file(conn, f)
    g = meter.growth(conn, "s-1")
    assert (g["floor"], g["prompt"], g["calls"], g["compactions"]) == (150001, 194901, 4, 1)
    assert g["output_kept"] == 1600, "the last response's output has reached no prompt yet"
    assert g["reloaded"] == 191901 - 151501 - 400, "what arrived with the files, its response's output apart"
    assert g["reloads"] == 1
    assert g["output_kept"] + g["arrived"] + g["reloaded"] == g["grown"], "the split adds up to the growth"
    assert 0 < g["thinking"] < g["output_kept"], "the output less what its visible blocks show"
    assert "x" * 50 not in json.dumps(g), "no text is kept"
    assert meter.growth(conn, "nobody") is None
    conn.close()


def test_files_handed_with_a_compaction_belong_to_the_floor(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_bytes(_said("old", "2027-01-15T08:00:00Z", 1, 900000, 100, 500)
                  + _handed_files("2027-01-15T08:59:00Z")
                  + _said("floor", "2027-01-15T09:00:00Z", 1, 30000, 120000, 10)
                  + _said("next", "2027-01-15T09:01:00Z", 1, 150000, 20, 10))
    conn = meter.connect(tmp_path / "m.sqlite3")
    meter.read_file(conn, f)
    g = meter.growth(conn, "s-1")
    assert (g["reloads"], g["reloaded"], g["floor"]) == (0, 0, 150001)
    conn.close()


def test_a_compaction_is_read_from_its_own_row_however_little_it_took(tmp_path):
    f = tmp_path / "a.jsonl"
    boundary = json.dumps({"type": "system", "subtype": "compact_boundary", "sessionId": "s-1",
                           "timestamp": "2027-01-15T08:59:00Z"}).encode() + b"\n"
    f.write_bytes(_said("old", "2027-01-15T08:00:00Z", 1, 211000, 100, 500)
                  + boundary + _handed_files("2027-01-15T08:59:30Z")
                  + _said("floor", "2027-01-15T09:00:00Z", 1, 30000, 120000, 10)
                  + _said("next", "2027-01-15T09:01:00Z", 1, 150000, 20, 10))
    conn = meter.connect(tmp_path / "m.sqlite3")
    meter.read_file(conn, f)
    g = meter.growth(conn, "s-1")
    assert (g["compactions"], g["floor"], g["reloaded"], g["reloads"]) == (1, 150001, 0, 0), \
        "211k to 150k is a compaction, though it kept over 70%, and no arrival is negative"
    assert g["arrived"] == 150021 - 150001 - 10
    conn.close()


def test_an_older_store_gains_the_columns_and_reads_each_transcript_again(tmp_path):
    db = tmp_path / "m.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript("""CREATE TABLE responses(message_id TEXT PRIMARY KEY, session_id TEXT, at REAL, model TEXT, input INTEGER,
        output INTEGER, cache_read INTEGER, cache_write INTEGER, subagent INTEGER NOT NULL DEFAULT 0, cwd TEXT);
        CREATE TABLE positions(path TEXT PRIMARY KEY, read_to INTEGER NOT NULL, seam TEXT, size INTEGER);""")
    conn.execute("INSERT INTO responses(message_id, session_id, at, input, output, cache_read, cache_write) VALUES ('a', 's', 1, 1, 2, 3, 4)")
    conn.execute("INSERT INTO positions VALUES ('/x', 10, 'seam', 10)")
    conn.commit()
    conn.close()
    conn = meter.connect(db)
    assert conn.execute("SELECT output, shown FROM responses").fetchone() == (2, 0), "the counts stay"
    assert conn.execute("SELECT count(*) FROM positions").fetchone() == (0,)
    conn.close()
    conn = meter.connect(db)
    conn.execute("INSERT INTO positions VALUES ('/y', 1, 's', 1)")
    conn.commit()
    conn.close()
    conn = meter.connect(db)
    assert conn.execute("SELECT count(*) FROM positions").fetchone() == (1,), "only the first open reads again"
    conn.close()


def test_a_store_from_before_compactions_reads_each_transcript_again(tmp_path):
    db = tmp_path / "m.sqlite3"
    conn = meter.connect(db)
    conn.execute("DROP TABLE compactions")
    conn.execute("INSERT INTO positions VALUES ('/x', 10, 'seam', 10)")
    conn.commit()
    conn.close()
    conn = meter.connect(db)
    assert conn.execute("SELECT count(*) FROM positions").fetchone() == (0,)
    conn.close()


def test_prune_keeps_the_responses(tmp_path):
    db = tmp_path / "m.sqlite3"
    meter.take(PAYLOAD, db=db, now=NOW - 60 * 86400)
    meter.take(PAYLOAD, db=db, now=NOW)
    conn = meter.connect(db)
    conn.executemany(meter._SAVE, [{"message_id": "m1", "session_id": "s-1", "at": NOW - 60 * 86400, "model": "x", "input": 1, "output": 1, "cache_read": 1, "cache_write": 1, "subagent": 0, "cwd": None}])
    conn.commit()
    conn.close()
    assert meter.prune(db=db, now=NOW) == {"statusline": 1, "telemetry": 0, "standing": 0}
    assert len(rows(db, "SELECT 1 FROM responses")) == 1


def test_per_call_around_compares_the_calls_on_each_side(tmp_path):
    db = tmp_path / "m.sqlite3"
    conn = meter.connect(db)
    def add(mid, at, read, sub=0, session="s-1"):
        conn.execute("INSERT INTO responses(message_id, session_id, at, model, input, output, cache_read, cache_write, subagent) "
                     "VALUES (?, ?, ?, 'claude-opus-9', 0, 0, ?, 0, ?)", (mid, session, at, read, sub))
    for i in range(3):
        add(f"b{i}", NOW - 100 + i, 400000)
        add(f"a{i}", NOW + 100 + i, 100000)
    add("sub", NOW - 1, 9, sub=1)
    add("other", NOW + 1, 9, session="s-2")
    conn.commit()
    conn.close()
    got = meter.per_call_around("s-1", NOW, calls=3, db=db)
    assert (got["calls_before"], got["calls_after"]) == (3, 3)
    assert got["change"] == -0.75
    early = meter.per_call_around("s-1", NOW, calls=4, db=db)
    assert early["change"] is None and early["before"] == got["before"]
    assert meter.per_call_around("nobody", NOW, db=db) == {"before": None, "after": None, "calls_before": 0, "calls_after": 0, "change": None}


def test_per_call_around_says_nothing_across_a_model_change(tmp_path):
    db = tmp_path / "m.sqlite3"
    conn = meter.connect(db)
    for i, (mid, at, model) in enumerate([("b0", NOW - 2, "claude-opus-9"), ("b1", NOW - 1, "claude-opus-9"),
                                          ("a0", NOW + 1, "claude-haiku-9"), ("a1", NOW + 2, "claude-haiku-9")]):
        conn.execute("INSERT INTO responses(message_id, session_id, at, model, input, output, cache_read, cache_write, subagent) "
                     "VALUES (?, 's-1', ?, ?, 0, 0, 1000, 0, 0)", (mid, at, model))
    conn.commit()
    conn.close()
    got = meter.per_call_around("s-1", NOW, calls=2, db=db)
    assert got["change"] is None and got["before"] > got["after"] and got["calls_after"] == 2


def test_a_chats_day_is_its_own_curve_and_each_side_of_its_spend(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_bytes(b"".join([
        _said("before", "2027-01-15T07:00:00Z", 1, 900000, 100, 500),
        _said("one", "2027-01-15T09:00:00Z", 1, 1000, 2000, 100),
        _said("two", "2027-01-15T09:01:00Z", 1, 3000, 500, 100),
    ]))
    db = tmp_path / "m.sqlite3"
    conn = meter.connect(db)
    meter.read_file(conn, f)
    at = conn.execute("SELECT at FROM responses WHERE message_id = 'two'").fetchone()[0]
    conn.execute("INSERT INTO responses(message_id, session_id, at, model, input, output, cache_read, cache_write, subagent) "
                 "VALUES ('helper', 's-1', ?, 'claude-opus-9', 1, 10, 500000, 0, 1)", (at + 1,))
    conn.execute("INSERT INTO compactions(session_id, at) VALUES ('s-1', ?)", (at + 2,))
    conn.execute("INSERT INTO compactions(session_id, at) VALUES ('s-1', ?)", (at - 86400,))
    conn.commit()
    conn.close()

    day = meter.chat_day("s-1", db=db, since=at - 3600, until=at + 3600)
    assert [p for _, p in day["curve"]] == [3001, 3501], "only the chat's own calls in the window, the subagent's left out"
    assert day["compactions"] == [at + 2], "a compaction outside the window is left out"
    assert (day["chat"]["responses"], day["subagents"]["responses"]) == (2, 1)
    assert day["subagents"]["weighted"] > 0 and day["chat"]["weighted"] > 0, "each side has its own spend"
    assert day["unknown_since"] == at + 2 and day["growth"] is None, "a compaction after the last response: nothing is known since"
    assert day["curve"][-1] == (at, 3501), "the curve keeps what was read, and unknown_since says it is from before"

    conn = meter.connect(db)
    conn.execute("INSERT INTO responses(message_id, session_id, at, model, input, output, cache_read, cache_write, subagent) "
                 "VALUES ('helper-2', 's-1', ?, 'claude-opus-9', 1, 10, 500000, 0, 1)", (at + 3,))
    conn.commit()
    conn.close()
    day = meter.chat_day("s-1", db=db, since=at - 3600, until=at + 3600)
    assert day["unknown_since"] == at + 2, "a subagent's response says nothing of the chat's own prompt"

    conn = meter.connect(db)
    conn.execute("INSERT INTO responses(message_id, session_id, at, model, input, output, cache_read, cache_write, subagent) "
                 "VALUES ('three', 's-1', ?, 'claude-opus-9', 1, 10, 900, 0, 0)", (at + 4,))
    conn.commit()
    conn.close()
    day = meter.chat_day("s-1", db=db, since=at - 3600, until=at + 3600)
    assert day["unknown_since"] is None and day["curve"][-1] == (at + 4, 901), "its next response is its size again"
    assert day["growth"]["floor"] == 901 and day["growth"]["prompt"] == 901, "the same reading as meter.growth, from the new start"

    empty = meter.chat_day("nobody", db=db, since=at - 3600, until=at + 3600)
    assert empty["curve"] == [] and empty["compactions"] == [] and empty["chat"]["responses"] == 0 and empty["growth"] is None
    assert empty["unknown_since"] is None, "no compaction, nothing to be unknown since"


def _spent(conn, rows):
    """(id, session, at, model, input, output, cache_read, cache_write, subagent)"""
    conn.executemany("INSERT INTO responses(message_id, session_id, at, model, input, output, cache_read, cache_write, subagent) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def test_a_windows_spend_splits_by_cause_and_adds_up(tmp_path):
    db = tmp_path / "m.sqlite3"
    conn = meter.connect(db)
    m = "claude-opus-9"
    _spent(conn, [
        ("old", "s-1", 50.0, m, 1, 10, 900000, 900000, 0),
        ("first", "s-1", 1000.0, m, 1, 10, 0, 100000, 0),
        ("warm", "s-1", 1010.0, m, 1, 20, 100000, 1000, 0),
        ("idle", "s-1", 9000.0, m, 1, 30, 0, 101000, 0),
        ("helper", "s-1", 9001.0, m, 1, 40, 0, 5000, 1),
        ("warm-2", "s-1", 9010.0, m, 1, 50, 101000, 2000, 0),
        ("small", "s-1", 9020.0, m, 1, 60, 1000, 40000, 0),
        ("warm-3", "s-1", 9030.0, m, 1, 70, 41000, 2000, 0),
        ("marked", "s-1", 9040.0, m, 1, 80, 1000, 42000, 0),
        ("other", "s-2", 1000.0, "claude-fable-9", 1, 1, 0, 10, 0),
    ])
    conn.execute("INSERT INTO compactions(session_id, at) VALUES ('s-1', 9035.0)")
    conn.commit()
    conn.close()

    got = meter.by_cause(db=db, since=500.0, until=9500.0)
    one = next(c for c in got["chats"] if c["session_id"] == "s-1")
    written = lambda *tokens: sum(meter.weighted(m, 0, 0, 0, t) for t in tokens)  # noqa: E731
    assert one["causes"]["fresh"] == written(100000, 5000, 40000, 42000), "a first response, a subagent's, a fall in the prompt and a compaction's own row"
    assert one["causes"]["expired"] == written(101000), "the same size written again, with nothing read: the prefix ran out"
    assert one["causes"]["written"] == written(1000, 2000, 2000), "what each ordinary call added"
    assert one["causes"]["output"] == meter.weighted(m, 0, 10 + 20 + 30 + 40 + 50 + 60 + 70 + 80, 0, 0)
    assert one["causes"]["reread"] == sum(meter.weighted(m, 1, 0, r, 0) for r in (0, 100000, 0, 0, 101000, 1000, 41000, 1000))
    assert one["responses"] == 8, "the response before the window is left out"
    assert sum(one["causes"].values()) == one["weighted"]
    assert got["weighted"] == sum(c["weighted"] for c in got["chats"]) == sum(got["causes"].values()), "it all adds up"
    assert got["responses"] == 9 and got["chats"][0]["session_id"] == "s-1", "the dearest chat first"


def test_an_empty_window_splits_into_nothing(tmp_path):
    got = meter.by_cause(db=tmp_path / "m.sqlite3", since=1.0, until=2.0)
    assert got["weighted"] == 0 and got["responses"] == 0 and got["chats"] == []
    assert set(got["causes"]) == set(meter.CAUSES), "every cause is there, at nothing"


CONTEXT = """## Context Usage

**Model:** claude-opus-9
**Tokens:** 41.4k / 1m (4%)

### Estimated usage by category

| Category | Tokens | Percentage |
|----------|--------|------------|
| System prompt | 3.5k | 0.3% |
| MCP tools (deferred) | 45.3k | 4.5% |
| Memory files | 30k | 3% |
| Skills | 1.2k | 0.1% |
| Free space | 876.9k | 87.7% |

### MCP Tools

| Tool | Server | Tokens |
|------|--------|--------|
| mcp__docs__read | docs | 300 |

### Memory Files

| Type | Path | Tokens |
|------|------|--------|
| User | /home/user/.claude/CLAUDE.md | 20k |
| Project | /work/app/CLAUDE.md | 10k |

### Skills

| Skill | Source | Tokens |
|-------|--------|--------|
| big | User | ~1.1k |
| small | User | ~90 |
| tiny | Built-in | < 20 |
"""


def _answers(text, seen=None):
    def run(argv, **kw):
        if seen is not None:
            seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"type": "result", "result": text}), stderr="")
    return run


def test_context_tables_read_as_parts():
    parts = meter.standing_parts(CONTEXT)
    assert ("category", "Skills", 1200) in parts and ("category", "Memory files", 30000) in parts
    assert ("file", "/home/user/.claude/CLAUDE.md", 20000) in parts and ("file", "/work/app/CLAUDE.md", 10000) in parts
    assert ("skill", "big", 1100) in parts and ("skill", "tiny", 20) in parts
    assert not [p for p in parts if p[1] in ("Tokens", "mcp__docs__read", "Path", "Skill")]


def test_a_standing_read_runs_no_hooks_and_saves_no_session(tmp_path):
    db, seen = tmp_path / "m.sqlite3", []
    assert meter.read_standing(tmp_path, db=db, now=100.0, run=_answers(CONTEXT, seen), env={"PATH": "/x"})
    argv, kw = seen[0]
    assert argv[:3] == ["claude", "-p", "/context"] and "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
    assert kw["cwd"] == str(tmp_path) and kw["env"] == {"PATH": "/x"} and kw["stdin"] == subprocess.DEVNULL
    got = meter.standing(db=db)[str(tmp_path)]
    assert got["at"] == 100.0 and ("file", "/work/app/CLAUDE.md", 10000) in got["parts"]


def test_a_standing_read_that_says_nothing_keeps_nothing(tmp_path):
    db = tmp_path / "m.sqlite3"

    def refused(argv, **kw):
        raise OSError("no such program")

    def late(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    def broken(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="not json", stderr="")

    assert meter.read_standing(tmp_path / "gone", db=db, run=_answers(CONTEXT)) is None
    for run in (refused, late, broken, _answers("no tables here")):
        assert meter.read_standing(tmp_path, db=db, run=run) is None
    assert meter.standing(db=db) == {}


def test_the_newest_standing_reading_of_a_folder_wins_and_old_ones_go(tmp_path):
    db = tmp_path / "m.sqlite3"
    meter.read_standing(tmp_path, db=db, now=100.0, run=_answers(CONTEXT))
    meter.read_standing(tmp_path, db=db, now=200.0, run=_answers(CONTEXT.replace("| 10k |", "| 7k |")))
    assert ("file", "/work/app/CLAUDE.md", 7000) in meter.standing(db=db)[str(tmp_path)]["parts"]
    assert ("file", "/work/app/CLAUDE.md", 10000) not in meter.standing(db=db)[str(tmp_path)]["parts"]
    assert meter.prune(db=db, keep_days=1, now=86400 + 150.0)["standing"] > 0
    assert meter.standing(db=db)[str(tmp_path)]["at"] == 200.0


def test_carrying_prices_each_part_by_the_calls_of_its_folders(tmp_path):
    db = tmp_path / "m.sqlite3"
    app, other, gone = tmp_path / "app", tmp_path / "other", tmp_path / "gone"
    app.mkdir()
    other.mkdir()
    meter.read_standing(app, db=db, now=500.0, run=_answers(CONTEXT))
    meter.read_standing(other, db=db, now=500.0, run=_answers(CONTEXT.replace("| Project | /work/app/CLAUDE.md | 10k |\n", "")))
    conn = meter.connect(db)
    opus, fable = "claude-opus-9", "claude-fable-9"
    rows = [
        # (id, session, at, model, input, output, read, write, subagent, cwd): the first call of a conversation pays the whole prefix
        ("a1", "s-a", 1000.0, opus, 1, 10, 0, 100000, 0, str(app)),
        ("a2", "s-a", 1010.0, opus, 1, 10, 100000, 500, 0, str(app)),
        ("a3", "s-a", 1020.0, opus, 1, 10, 100500, 500, 0, str(app)),
        ("h1", "s-a", 1030.0, opus, 1, 10, 0, 9000, 1, str(app)),
        ("b1", "s-b", 1000.0, fable, 1, 10, 0, 80000, 0, str(other)),
        ("c1", "s-c", 1000.0, opus, 1, 10, 900, 100, 0, str(gone)),
        ("o1", "s-old", -2000000.0, opus, 1, 10, 900, 100, 0, str(app)),
    ]
    conn.executemany("INSERT INTO responses(message_id, session_id, at, model, input, output, cache_read, cache_write, subagent, cwd) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    assert meter.busiest_folders(db=db, days=1, now=2000.0, limit=2)[:1] == [str(app)]
    got = meter.carrying(db=db, since=500.0, until=2000.0, now=2000.0)
    assert (got["responses"], got["responses_read"]) == (5, 4)
    by = {(p["kind"], p["name"]): p for p in got["parts"]}
    # app: one call wrote it and two read it, on Opus. other: one call wrote it, on Fable
    app_w, other_w = 12.5 * 5 + 2 * 5, 12.5 * 10
    app_e, other_e = 1.25 + 2 * 0.1, 1.25
    home = by[("file", "/home/user/.claude/CLAUDE.md")]
    assert home["folders"] == {str(app): 20000, str(other): 20000}
    assert home["weighted"] == 20000 * (app_w + other_w) and home["equivalents"] == 20000 * (app_e + other_e)
    only = by[("file", "/work/app/CLAUDE.md")]
    assert only["folders"] == {str(app): 10000} and only["weighted"] == 10000 * app_w
    assert by[("skills", "Skills")]["tokens"] == 1200
    # What a first call paid beyond the named parts: 100,001 less 31,200 in app and 80,001 less 21,200 in other. s-c's
    # folder has no reading, so it is held against app, the busiest, which leaves 0. s-old is too old for either figure
    assert (got["first_calls"], got["first_call"], got["rest"]) == (3, 80001, 58801)
    assert by[("rest", "rest")]["folders"] == {str(app): 58801, str(other): 58801}
    assert by[("rest", "rest")]["weighted"] == 58801 * (app_w + other_w)
    assert got["carried"]["weighted"] == sum(p["weighted"] for p in got["parts"])
    assert got["carried"]["equivalents"] == sum(p["equivalents"] for p in got["parts"])
    assert got["parts"][0]["weighted"] == max(p["weighted"] for p in got["parts"])
    assert got["folders"][str(app)] == {"at": 500.0, "responses": 3}
    assert got["skills"][0] == ("big", 1100)


def test_carrying_with_nothing_read_is_unknown_and_not_zero(tmp_path):
    got = meter.carrying(db=tmp_path / "m.sqlite3", since=1.0, until=2.0)
    assert got["parts"] == [] and got["rest"] is None and got["first_call"] is None and got["skills"] == []
    assert got["responses_read"] == 0
