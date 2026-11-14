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


def test_two_writers_at_once_both_finish(chat_tree, tmp_path):
    """Two processes building the index at the same moment each write through a temp file of their
    own: with one shared temp name, one renamed the other's file away and failed."""
    import threading
    cache = tmp_path / "cache" / "sessions.json"
    errors: list[BaseException] = []

    def run():
        try:
            for _ in range(20):
                sessions.build_index(chat_tree, cache, force=True)
        except BaseException as e:   # noqa: BLE001 - the test reports whatever a writer raised
            errors.append(e)
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(json.loads(cache.read_text())) == 2
    assert not list(cache.parent.glob("*.tmp"))


def test_the_index_follows_a_conversation_that_moved(tmp_path):
    """Every record names the folder it ran in. A conversation moved to another folder (the person
    resumed it elsewhere) shows the newest folder, on a full read and on an incremental one."""
    from claude_ops import sessions
    root = tmp_path / "projects"
    d = root / "-tmp-first"
    d.mkdir(parents=True)
    path = d / "0d0e0f10-1111-4222-8333-444455556666.jsonl"
    recs = [{"type": "user", "cwd": "/tmp/first", "message": {"role": "user", "content": "hello"}, "sessionId": path.stem},
            {"type": "assistant", "cwd": "/tmp/first", "message": {"role": "assistant", "content": "hi"}}]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    first = sessions.read_meta(path)
    assert first["cwd"] == "/tmp/first"
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "cwd": "/tmp/second", "message": {"role": "user", "content": "moved"}}) + "\n")
    again = sessions.read_meta(path, first)
    assert again["cwd"] == "/tmp/second", again
    fresh = sessions.read_meta(path)
    assert fresh["cwd"] == "/tmp/second"


def test_cache_follows_the_transcript_folder(tmp_path, monkeypatch):
    """A folder other than Claude Code's own gets an index file of its own, so reading it never replaces
    the index of the real folder."""
    monkeypatch.delenv("CLAUDE_OPS_CACHE", raising=False)
    fixtures = tmp_path / "projects"
    real = sessions.Path.home() / ".claude" / "projects"
    assert sessions.default_cache(real) == sessions.Path.home() / ".cache" / "claude-ops" / "sessions.json"
    assert sessions.default_cache(fixtures) == tmp_path / "claude-ops-sessions.json"
    monkeypatch.setenv("CLAUDE_OPS_CACHE", str(tmp_path / "named.json"))
    assert sessions.default_cache(fixtures) == tmp_path / "named.json"


def test_build_index_of_another_folder_leaves_the_default_cache_alone(chat_tree, tmp_path, monkeypatch):
    """Asking for the index of a folder without naming a cache file keeps it beside that folder."""
    monkeypatch.delenv("CLAUDE_OPS_CACHE", raising=False)
    sessions.build_index(chat_tree)
    assert sessions.default_cache(chat_tree).exists()
    assert sessions.default_cache(chat_tree) != sessions.CACHE


def test_index_max_age_reuses_a_recent_answer_and_default_does_not(chat_tree, tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    sessions._RECENT.clear()
    stats = []
    real = type(chat_tree).glob
    monkeypatch.setattr(type(chat_tree), "glob", lambda self, pat: stats.append(pat) or real(self, pat))
    first = sessions.build_index(chat_tree, cache, max_age=60)
    again = sessions.build_index(chat_tree, cache, max_age=60)
    assert [e["id"] for e in again] == [e["id"] for e in first]
    assert len(stats) == 1  # the second answer did not list the folder
    sessions.build_index(chat_tree, cache)
    sessions.build_index(chat_tree, cache, force=True, max_age=60)
    assert len(stats) == 3  # no max_age and force both ask again


def test_index_max_age_gives_each_caller_its_own_entries(chat_tree, tmp_path):
    cache = tmp_path / "cache.json"
    sessions._RECENT.clear()
    one = sessions.build_index(chat_tree, cache, max_age=60)
    one[0]["maybe_live"] = True
    two = sessions.build_index(chat_tree, cache, max_age=60)
    assert "maybe_live" not in two[0]


def test_index_max_age_runs_out(chat_tree, tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    sessions._RECENT.clear()
    clock = [1000.0]
    monkeypatch.setattr(sessions.time, "monotonic", lambda: clock[0])
    sessions.build_index(chat_tree, cache, max_age=5)
    with (chat_tree / "-work-beta" / "bbb.jsonl").open("a") as f:
        f.write('{"type":"user","message":{"content":"more"}}\n')
    assert sessions.build_index(chat_tree, cache, max_age=5)[0]["id"] == "bbb"
    clock[0] += 6
    calls = []
    real = sessions.read_meta
    monkeypatch.setattr(sessions, "read_meta", lambda p, prev=None: calls.append(p) or real(p, prev))
    sessions.build_index(chat_tree, cache, max_age=5)
    assert [p.stem for p in calls] == ["bbb"]  # after 5 seconds the folder is read again and the change is seen


def test_live_folders_max_age_runs_the_process_list_once(monkeypatch):
    sessions._RECENT.clear()
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, "123\n" if cmd[0] == "pgrep" else "p123\nn/work/alpha\n", "")

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    assert sessions.live_folders(max_age=30) == {"/work/alpha"}
    assert sessions.live_folders(max_age=30) == {"/work/alpha"}
    assert runs == ["pgrep", "lsof"]
    sessions.live_folders()
    assert runs == ["pgrep", "lsof", "pgrep", "lsof"]


def _call(t, command, cwd="/work/a", tool="Bash"):
    return {"type": "assistant", "cwd": cwd, "timestamp": datetime.utcfromtimestamp(t).isoformat() + "Z",
            "message": {"content": [{"type": "tool_use", "name": tool, "input": {"command": command}}]}}


def _write(path, records, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "not json\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_calls_near_finds_the_call_seconds_before_a_request(tmp_path):
    at = 1_800_000_000.0
    pad = [{"type": "user", "timestamp": datetime.utcfromtimestamp(at - 30).isoformat() + "Z",
            "message": {"content": "x" * 200}}] * 20
    _write(tmp_path / "-work-a" / "s1.jsonl",
           [_call(at - 60, "osascript -e 'tell application \"Finder\" to get bounds'"), *pad,
            _call(at - 2, "osascript -e 'tell application \"Finder\" to get bounds of window of desktop'"),
            _call(at - 1, "ls", tool="Read"), _call(at + 5, "osascript Finder late")], mtime=at + 5)
    _write(tmp_path / "-work-a" / "s1" / "subagents" / "agent-x.jsonl",
           [_call(at - 4, "OSASCRIPT finder from a helper", cwd="/work/a/helper")], mtime=at - 4)
    _write(tmp_path / "-work-b" / "s2.jsonl", [_call(at - 3, "osascript Finder")], mtime=at - 3600)
    got = sessions.calls_near(at, ["osascript", "Finder"], tmp_path, chunk=64)
    # newest first; the call a minute before, the one after the window and a call without the words are left out
    assert [(c["session"], c["time"], c["tool"], c["folder"]) for c in got] == [
        ("s1", at - 2, "Bash", "/work/a"), ("s1", at - 4, "Bash", "/work/a/helper")]
    assert "get bounds of window of desktop" in got[0]["input"]["command"]
    # a transcript not changed since the window began is never read
    assert all(c["session"] != "s2" for c in got)


def test_calls_near_reads_backwards_across_chunks(tmp_path):
    p = tmp_path / "-w" / "s.jsonl"
    _write(p, [{"n": i} for i in range(50)])
    assert [r["n"] for r in sessions._records_back(p, chunk=7)] == list(range(49, -1, -1))
    assert sessions.calls_near(1_800_000_000.0, ["x"], tmp_path / "missing") == []
