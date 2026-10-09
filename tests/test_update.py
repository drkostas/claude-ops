"""When a chat may be touched, and how it is restarted in place."""
import datetime as dt
import json
import subprocess

from claude_ops import update as cu

NOW = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)
MCP_ONLY = {100: {"ppid": 1, "comm": "claude"}, 101: {"ppid": 100, "comm": "node"},
            102: {"ppid": 100, "comm": "/opt/homebrew/bin/uv"}}
WITH_CMD = {**MCP_ONLY, 103: {"ppid": 100, "comm": "/bin/zsh"}}


def ago(s):
    return NOW - dt.timedelta(seconds=s)


def test_idle():
    assert cu.idle(100, MCP_ONLY, ago(120), ago(110), NOW)[0]
    assert not cu.idle(100, WITH_CMD, ago(120), ago(110), NOW)[0]
    assert not cu.idle(100, MCP_ONLY, ago(60), ago(600), NOW)[0]      # written since its turn ended
    assert not cu.idle(100, MCP_ONLY, ago(10), ago(9), NOW)[0]        # turn ended seconds ago
    assert not cu.idle(100, MCP_ONLY, None, ago(9), NOW)[0]           # no transcript: unknown
    assert not cu.idle(100, MCP_ONLY, ago(300), None, NOW)[0]         # no end of turn: wait longer
    assert cu.idle(100, MCP_ONLY, ago(900), None, NOW)[0]
    assert not cu.idle(100, MCP_ONLY, ago(900), None, NOW, prompt_on_screen=False)[0]
    assert cu.idle(100, MCP_ONLY, ago(70), None, NOW, quiet_unhooked_s=60)[0]


def test_may_be_typing():
    assert cu.may_be_typing(30, "iTerm2")
    assert not cu.may_be_typing(30, "Google Chrome")
    assert not cu.may_be_typing(600, "iTerm2")
    assert cu.may_be_typing(None, "Google Chrome")
    assert cu.may_be_typing(30, "Terminal", terminal="Terminal")


def test_last_activity_is_the_last_message(tmp_path):
    t = tmp_path / "x.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "timestamp": "2026-10-09T09:00:00Z"},
        {"type": "assistant", "timestamp": "2026-10-09T09:04:10Z"},
        {"type": "system", "subtype": "stop_hook_summary", "timestamp": "2026-10-09T09:04:16Z"},
        {"type": "artifact-autoreact-ledger"}]) + "\n")
    assert cu.last_activity(t) == dt.datetime(2026, 10, 9, 9, 4, 10, tzinfo=dt.timezone.utc)
    with open(t, "a") as f:
        f.write(json.dumps({"type": "queue-operation", "timestamp": "2026-10-09T09:10:00Z"}) + "\n")
    assert cu.last_activity(t).minute == 10
    assert cu.last_activity(None) is None


def test_last_activity_reads_past_a_long_run_of_bookkeeping(tmp_path):
    t = tmp_path / "deep.jsonl"
    with open(t, "w") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-09T09:00:00Z"}) + "\n")
        for _ in range(400):
            f.write(json.dumps({"type": "artifact-autoreact-ledger", "pad": "x" * 2000}) + "\n")
    assert cu.last_activity(t, first=4096) == dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone.utc)


def test_resume_line_and_permission_mode():
    assert (cu.resume_line("claude --allow-dangerously-skip-permissions --resume old", "U")
            == "claude --allow-dangerously-skip-permissions --resume U")
    assert cu.resume_line("claude -n x --remote-control x -c", "U") == "claude -n x --remote-control x --resume U"
    started_with_a_prompt = ("claude --allow-dangerously-skip-permissions --name vision "
                             "You are the vision chat. Read ~/x.md --permission-mode bypassPermissions "
                             "--resume OLD")
    assert (cu.resume_line(started_with_a_prompt, "U", "bypassPermissions")
            == "claude --allow-dangerously-skip-permissions --name vision "
               "--permission-mode bypassPermissions --resume U")
    assert cu.resume_line("claude --some-new-flag value", "U") == "claude --resume U"
    assert cu.resume_line("/opt/homebrew/bin/node something", "U") == "claude --resume U"
    assert cu.permission_mode("x\n  ⏵⏵ bypass permissions on (shift+tab to cycle)") == "bypassPermissions"
    assert cu.permission_mode("❯ \n────") is None


def test_empty_prompt():
    assert cu.empty_prompt("hello\n❯ \n───\n  ⏵⏵ accept edits on")[0]
    assert not cu.empty_prompt("❯ half a sentence\n───")[0]
    assert not cu.empty_prompt("You have 1 unsent feedback draft\nEnter to review & send · Esc to discard")[0]
    assert not cu.empty_prompt(None)[0]


def test_transcript_of_finds_the_newest(tmp_path):
    (tmp_path / "-a").mkdir()
    (tmp_path / "-a" / "ID.jsonl").write_text("x")
    assert cu.transcript_of("ID", tmp_path) == tmp_path / "-a" / "ID.jsonl"
    assert cu.transcript_of("NONE", tmp_path) is None


def test_the_mac_readings_carry_the_env(monkeypatch):
    seen = []

    def fake(argv, **k):
        seen.append(k.get("env"))
        out = {"/usr/sbin/ioreg": '"HIDIdleTime" = 5000000000\n', "/usr/bin/osascript": "❯ \n"}.get(
            argv[0], '"LSDisplayName"="iTerm2"\n')
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake)
    env = {"PATH": "/usr/bin:/bin"}
    assert cu.seconds_since_input(env=env) == 5.0
    assert cu.front_app(env=env) == "iTerm2"
    assert cu.screen_of("iterm", "ABC", env=env) == "❯ \n"
    assert seen and all(e is env for e in seen)


def test_restart_iterm_terminates_waits_and_types_the_resume_line(monkeypatch):
    killed, typed = [], []
    state = {"claude": True}
    monkeypatch.setattr(cu, "screen_of", lambda kind, h, env=None: "⏵⏵ plan mode on")
    monkeypatch.setattr(cu, "process_args", lambda pid, env=None: "claude -n soma --resume OLD")
    monkeypatch.setattr(cu.os, "kill", lambda pid, sig: (killed.append(pid), state.update(claude=False)))
    monkeypatch.setattr(cu._inject, "iterm_sessions", lambda env=None: {
        "ABC": {"name": "soma (2.1.294)" if state["claude"] else "soma (-zsh)"}})

    def send(sid, text, enter, env=None):
        typed.append(text)
        state["claude"] = True
        return True, "sent"

    monkeypatch.setattr(cu._inject, "send_iterm", send)
    monkeypatch.setattr(cu.time, "sleep", lambda s: None)
    ok, how = cu.restart_iterm(100, "ABC", "/work/soma", "NEW", stop_s=5, back_s=5)
    assert ok and killed == [100]
    assert typed == ["cd /work/soma && claude -n soma --permission-mode plan --resume NEW"]
