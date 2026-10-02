import os
import subprocess

from claude_ops.watch import State, lock, run_source


def sender(result="sent"):
    calls = []

    def send(target, text, transport="iterm"):
        calls.append(text)
        return {"result": result}
    return send, calls


def test_first_run_is_a_silent_baseline(tmp_path):
    s = State(tmp_path / "s.json")
    send, calls = sender()
    r = run_source(s, "src", lambda: ["1", "2"], lambda n: "x", "chat", send=send)
    assert r["result"] == "baseline" and calls == []
    assert run_source(s, "src", lambda: ["1", "2"], lambda n: "x", "chat", send=send)["result"] == "nothing-new"


def test_new_items_wake_the_chat_once(tmp_path):
    s = State(tmp_path / "s.json")
    send, calls = sender()
    run_source(s, "src", lambda: ["1"], str, "chat", send=send)
    r = run_source(s, "src", lambda: ["1", "2"], lambda n: f"new {n}", "chat", send=send)
    assert r["result"] == "sent" and calls == ["new ['2']"]
    assert State(tmp_path / "s.json").new_items("src", ["1", "2"]) == []  # saved


def test_items_are_kept_when_the_chat_is_closed(tmp_path):
    s = State(tmp_path / "s.json")
    run_source(s, "src", lambda: [], str, "chat", send=sender()[0])
    closed, _ = sender("no-session")
    assert run_source(s, "src", lambda: ["9"], str, "chat", send=closed)["result"] == "no-session"
    send, calls = sender()
    run_source(s, "src", lambda: ["9"], str, "chat", send=send)
    assert calls == ["['9']"]  # offered again


def test_a_failing_source_alerts_once(tmp_path):
    s = State(tmp_path / "s.json")
    send, calls = sender()

    def broken():
        raise RuntimeError("token expired")
    for _ in range(7):
        run_source(s, "src", broken, str, "chat", send=send, alert_after=3)
    assert len(calls) == 1 and "token expired" in calls[0]
    run_source(s, "src", lambda: [], str, "chat", send=send)  # a success resets it
    for _ in range(3):
        run_source(s, "src", broken, str, "chat", send=send, alert_after=3)
    assert len(calls) == 2


def test_lock_is_exclusive_and_a_dead_owner_is_replaced(tmp_path):
    path = tmp_path / "lock"
    with lock(path) as a:
        assert a
        with lock(path) as b:
            assert not b
    assert not path.exists()
    dead = subprocess.Popen(["true"])
    dead.wait()
    path.mkdir()
    (path / "pid").write_text(str(dead.pid))
    with lock(path) as c:
        assert c and (path / "pid").read_text() == str(os.getpid())
