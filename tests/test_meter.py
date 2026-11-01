"""meter: the status line kept at most once a minute, a response counted once however many lines repeat
it, a transcript read from its position, telemetry through the collector, and the report. All data invented."""
import gzip
import io
import json
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


def test_prune_keeps_the_responses(tmp_path):
    db = tmp_path / "m.sqlite3"
    meter.take(PAYLOAD, db=db, now=NOW - 60 * 86400)
    meter.take(PAYLOAD, db=db, now=NOW)
    conn = meter.connect(db)
    conn.executemany(meter._SAVE, [{"message_id": "m1", "session_id": "s-1", "at": NOW - 60 * 86400, "model": "x", "input": 1, "output": 1, "cache_read": 1, "cache_write": 1, "subagent": 0, "cwd": None}])
    conn.commit()
    conn.close()
    assert meter.prune(db=db, now=NOW) == {"statusline": 1, "telemetry": 0}
    assert len(rows(db, "SELECT 1 FROM responses")) == 1
