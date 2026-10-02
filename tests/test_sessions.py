import os
from datetime import datetime

from claude_ops import sessions


def test_index_reads_identity_title_and_last_activity(chat_tree, tmp_path):
    idx = sessions.build_index(chat_tree, tmp_path / "cache.json")
    by = {e["id"]: e for e in idx}
    assert by["aaa"]["title"] == "alpha-login"
    assert by["aaa"]["cwd"] == "/work/alpha" and by["aaa"]["branch"] == "main"
    assert by["aaa"]["first"] == "fix the login page"
    # a first message that is a command wrapper is skipped
    assert by["bbb"]["first"] == "write the backup script"
    assert by["bbb"]["active_at"] == datetime.fromisoformat("2026-02-01T09:30:00+00:00").timestamp()
    # newest activity first
    assert [e["id"] for e in idx] == ["bbb", "aaa"]


def test_last_activity_ignores_a_fresh_file_time(chat_tree, tmp_path):
    p = chat_tree / "-work-alpha" / "aaa.jsonl"
    os.utime(p, (2_000_000_000, 2_000_000_000))  # a copy gave it a new time
    e = sessions.read_meta(p)
    assert e["active_at"] == datetime.fromisoformat("2026-01-01T10:05:00+00:00").timestamp()


def test_index_is_incremental(chat_tree, tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    sessions.build_index(chat_tree, cache)
    calls = []
    real = sessions.read_meta
    monkeypatch.setattr(sessions, "read_meta", lambda p: calls.append(p) or real(p))
    sessions.build_index(chat_tree, cache)
    assert calls == []
    with (chat_tree / "-work-beta" / "bbb.jsonl").open("a") as f:
        f.write('{"type":"user","message":{"content":"more"}}\n')
    sessions.build_index(chat_tree, cache)
    assert [p.stem for p in calls] == ["bbb"]


def test_find_and_resume_command(chat_tree, tmp_path):
    idx = sessions.build_index(chat_tree, tmp_path / "c.json")
    assert [e["id"] for e in sessions.find(idx, "LOGIN")] == ["aaa"]
    assert [e["id"] for e in sessions.find(idx, "/work/beta")] == ["bbb"]
    assert sessions.resume_command({"id": "aaa", "cwd": "/work/alpha"}) == "cd /work/alpha && claude --resume aaa"


def test_maybe_live_needs_both_recent_activity_and_a_process(chat_tree, tmp_path):
    idx = sessions.build_index(chat_tree, tmp_path / "c.json")
    now = datetime.fromisoformat("2026-02-01T09:35:00+00:00").timestamp()
    out = {e["id"]: e["maybe_live"] for e in sessions.annotate(idx, live={"/work/beta", "/work/alpha"}, now=now)}
    assert out == {"bbb": True, "aaa": False}
    out = {e["id"]: e["maybe_live"] for e in sessions.annotate(idx, live=set(), now=now)}
    assert out == {"bbb": False, "aaa": False}


def test_deep_search_finds_text_anywhere(chat_tree):
    hits = sessions.deep_search("ZEBRA", chat_tree)
    assert set(hits) == {"bbb"}
    assert sessions.deep_search("not in any chat", chat_tree) == {}
