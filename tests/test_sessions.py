import json
import os
import subprocess
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
    monkeypatch.setattr(sessions, "read_meta", lambda p, prev=None: calls.append(p) or real(p, prev))
    sessions.build_index(chat_tree, cache)
    assert calls == []
    with (chat_tree / "-work-beta" / "bbb.jsonl").open("a") as f:
        f.write('{"type":"user","message":{"content":"more"}}\n')
    sessions.build_index(chat_tree, cache)
    assert [p.stem for p in calls] == ["bbb"]


def _counting_records(monkeypatch):
    parsed = []
    real = sessions._record
    monkeypatch.setattr(sessions, "_record", lambda raw: parsed.append(raw) or real(raw))
    return parsed


def test_a_grown_transcript_is_read_from_where_the_last_read_stopped(chat_tree, tmp_path, monkeypatch):
    p = chat_tree / "-work-alpha" / "aaa.jsonl"
    with p.open("a") as f:  # past the first records, where only a rename is still looked for
        f.write("".join(json.dumps({"type": "assistant", "message": {"content": "x" * 50}}) + "\n" for _ in range(60)))
    cache = tmp_path / "cache.json"
    sessions.build_index(chat_tree, cache)
    parsed = _counting_records(monkeypatch)
    with p.open("a") as f:
        f.write(json.dumps({"customTitle": "alpha-renamed"}) + "\n")
    by = {e["id"]: e for e in sessions.build_index(chat_tree, cache)}
    assert by["aaa"]["title"] == "alpha-renamed"
    assert by["aaa"]["cwd"] == "/work/alpha" and by["aaa"]["first"] == "fix the login page"
    # only the new record was parsed, not the first rename again
    assert [json.loads(r) for r in parsed] == [{"customTitle": "alpha-renamed"}]
    assert by["aaa"]["read_to"] == p.stat().st_size


def test_a_line_still_being_written_is_read_again_once_whole(chat_tree):
    p = chat_tree / "-work-alpha" / "aaa.jsonl"
    with p.open("a") as f:
        f.write('{"customTitle": "half-wri')
    first = sessions.read_meta(p)
    assert first["title"] == "alpha-login" and first["read_to"] < p.stat().st_size
    with p.open("a") as f:
        f.write('tten"}\n')
    assert sessions.read_meta(p, first)["title"] == "half-written"


def test_a_replaced_transcript_is_read_from_the_start(chat_tree, tmp_path):
    p = chat_tree / "-work-alpha" / "aaa.jsonl"
    cache = tmp_path / "cache.json"
    sessions.build_index(chat_tree, cache)
    # another chat's transcript copied over it, longer than the first
    p.write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": "user", "cwd": "/work/gamma", "message": {"content": "a different chat " + "y" * 400}},
        {"customTitle": "gamma"}]))
    by = {e["id"]: e for e in sessions.build_index(chat_tree, cache)}
    assert by["aaa"]["cwd"] == "/work/gamma" and by["aaa"]["title"] == "gamma"
    assert by["aaa"]["first"].startswith("a different chat")


def test_the_cache_is_written_only_when_something_changed(chat_tree, tmp_path):
    cache = tmp_path / "cache.json"
    sessions.build_index(chat_tree, cache)
    ino = cache.stat().st_ino
    sessions.build_index(chat_tree, cache)
    assert cache.stat().st_ino == ino  # each write replaces the file, so a new inode means a write
    (chat_tree / "-work-beta" / "bbb.jsonl").unlink()
    assert [e["id"] for e in sessions.build_index(chat_tree, cache)] == ["aaa"]
    assert cache.stat().st_ino != ino
    assert [e["id"] for e in json.loads(cache.read_text())] == ["aaa"]


def test_live_folders_asks_lsof_once_for_every_process(monkeypatch):
    calls = []

    def fake(cmd, **k):
        calls.append(cmd)
        out = "11\n12\n" if cmd[0] == "pgrep" else "p11\nfcwd\nn/work/alpha\np12\nfcwd\nn/work/beta\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake)
    assert sessions.live_folders() == {"/work/alpha", "/work/beta"}
    assert [c for c in calls if c[0] == "lsof"] == [["lsof", "-a", "-p", "11,12", "-d", "cwd", "-Fn"]]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 1, "", ""))
    calls.clear()
    assert sessions.live_folders() == set() and [c[0] for c in calls] == ["pgrep"]


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
