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


def test_a_turn_that_ended_in_an_error_does_not_read_as_working_for_ever():
    # an API error ends the turn without a Stop hook, so the transcript is newer than the last
    # recorded end of turn (portal-local-gguf, 2026-10-11)
    ok, why = cu.idle(100, MCP_ONLY, ago(900), ago(3600), NOW, prompt_on_screen=True)
    assert ok and "error" in why
    assert not cu.idle(100, MCP_ONLY, ago(900), ago(3600), NOW)[0]                  # screen unknown
    assert not cu.idle(100, MCP_ONLY, ago(900), ago(3600), NOW, prompt_on_screen=False)[0]
    assert not cu.idle(100, MCP_ONLY, ago(120), ago(3600), NOW, prompt_on_screen=True)[0]  # recent
    assert not cu.idle(100, WITH_CMD, ago(900), ago(3600), NOW, prompt_on_screen=True)[0]  # a command


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


def test_a_compaction_is_not_activity(tmp_path):
    t = tmp_path / "compacted.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "timestamp": "2026-10-09T17:36:24Z"},
        {"type": "system", "subtype": "stop_hook_summary", "timestamp": "2026-10-09T17:36:25Z"},
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-09T18:32:00Z"},
        {"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
         "timestamp": "2026-10-09T18:32:00Z"}]) + "\n")
    assert cu.last_activity(t) == dt.datetime(2026, 10, 9, 17, 36, 24, tzinfo=dt.timezone.utc)


def test_a_local_command_is_not_activity(tmp_path):
    t = tmp_path / "refreshed.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "timestamp": "2026-10-09T16:02:48Z"},
        {"type": "user", "timestamp": "2026-10-09T16:46:47Z", "message": {"content":
            "<command-name>/reload-plugins</command-name>\n<command-message>reload-plugins</command-message>"}},
        {"type": "user", "isMeta": True, "timestamp": "2026-10-09T16:46:47Z",
         "message": {"content": "<local-command-caveat>The command below was run directly</local-command-caveat>"}},
        {"type": "user", "timestamp": "2026-10-09T16:46:48Z", "message": {"content":
            "<local-command-stdout>Reloaded 4 plugins</local-command-stdout>"}}]) + "\n")
    assert cu.last_activity(t) == dt.datetime(2026, 10, 9, 16, 2, 48, tzinfo=dt.timezone.utc)
    with open(t, "a") as f:                       # a message he types afterwards still counts
        f.write(json.dumps({"type": "user", "timestamp": "2026-10-09T17:00:00Z",
                            "message": {"content": "please check the deploy"}}) + "\n")
    assert cu.last_activity(t).hour == 17


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
    # a chat started with its own settings file (a local model, say) stays on it after a restart
    assert (cu.resume_line("claude --settings /s.json -n x", "U")
            == "claude --settings /s.json -n x --resume U")
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


def test_remote_control_of_reads_the_newest_bridge_status(tmp_path):
    t = tmp_path / "chat.jsonl"
    active = {"type": "system", "subtype": "bridge_status",
              "content": "/remote-control is active · Continue here, on your phone"}
    blocked = {"type": "system", "subtype": "informational",
               "content": "Remote Control not started here · another Claude Code has it"}
    t.write_text(json.dumps(active) + "\n" + json.dumps(blocked) + "\n")
    # the newest bridge_status decides; a note that it could not start here is not him turning it off
    assert cu.remote_control_of(t) is True
    t.write_text(json.dumps(active) + "\n" + json.dumps(
        {"type": "system", "subtype": "bridge_status", "content": "/remote-control is off"}) + "\n")
    assert cu.remote_control_of(t) is False
    t.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    assert cu.remote_control_of(t) is False
    assert cu.remote_control_of(None) is False
    assert cu.remote_control_of(tmp_path / "missing.jsonl") is False


def test_resume_line_keeps_remote_control_switched_on_inside_the_chat():
    started = "claude --allow-dangerously-skip-permissions -n drlab-core"
    assert (cu.resume_line(started, "U", remote_control=True)
            == "claude --allow-dangerously-skip-permissions -n drlab-core --remote-control drlab-core --resume U")
    assert (cu.resume_line("claude --allow-dangerously-skip-permissions", "U", "bypassPermissions",
                           remote_control=True)
            == "claude --allow-dangerously-skip-permissions --remote-control --permission-mode bypassPermissions --resume U")
    assert (cu.resume_line("claude", "U", remote_control="core")
            == "claude --remote-control core --resume U")
    # started with the flag: kept once, never doubled
    assert (cu.resume_line("claude -n x --remote-control x", "U", remote_control=True)
            == "claude -n x --remote-control x --resume U")
    assert cu.resume_line("claude -n x", "U") == "claude -n x --resume U"


def _chat_dir(tmp_path, chat_id="C1"):
    proj = tmp_path / "projects" / "-work-x"
    proj.mkdir(parents=True)
    (proj / f"{chat_id}.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-10-09T11:00:00Z"}) + "\n")
    return tmp_path / "projects", proj


def test_title_of_is_the_newest_custom_title(tmp_path):
    t = tmp_path / "c.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in (
        {"type": "custom-title", "customTitle": "old"}, {"type": "user"},
        {"type": "custom-title", "customTitle": "drlab-statistics"})) + "\n")
    assert cu.title_of(t) == "drlab-statistics"
    assert cu.title_of(None) is None and cu.title_of(tmp_path / "missing.jsonl") is None


def test_a_background_agent_keeps_a_chat_busy(tmp_path):
    projects, proj = _chat_dir(tmp_path)
    assert cu.background_agent("C1", projects=projects) is None
    sub = proj / "C1" / "subagents"
    sub.mkdir(parents=True)
    f = sub / "agent-a1.jsonl"
    f.write_text("{}\n")
    assert cu.background_agent("C1", projects=projects) == "agent-a1"
    old = f.stat().st_mtime - 3600
    import os
    os.utime(f, (old, old))
    assert cu.background_agent("C1", projects=projects) is None


def test_ready_names_a_question_waiting_for_the_person(tmp_path, monkeypatch):
    projects, _ = _chat_dir(tmp_path)
    asking = "❯ 1. Digest them (Recommended)\n  2. Push them too\nEnter to select · Esc to cancel\n"
    ok, why = cu.ready(100, MCP_ONLY, "C1", asking, ago(3600), NOW, projects=projects)
    assert not ok and "question" in why
    monkeypatch.setattr(cu, "last_activity", lambda p: ago(120))
    ok, why = cu.ready(100, MCP_ONLY, "C1", "ok\n❯ \n", ago(110), NOW, projects=projects)
    assert ok, why
    assert not cu.ready(100, WITH_CMD, "C1", "ok\n❯ \n", ago(110), NOW, projects=projects)[0]
    agent = projects / "-work-x" / "C1" / "subagents"
    agent.mkdir(parents=True)
    (agent / "agent-b2.jsonl").write_text("{}\n")
    monkeypatch.setattr(cu.time, "time", lambda: NOW.timestamp())
    import os
    os.utime(agent / "agent-b2.jsonl", (NOW.timestamp() - 10, NOW.timestamp() - 10))
    ok, why = cu.ready(100, MCP_ONLY, "C1", "ok\n❯ \n", ago(110), NOW, projects=projects)
    assert not ok and "agent-b2" in why


def test_tmux_name_keeps_only_safe_characters():
    assert cu.tmux_name("drlab-core", "abc") == "claude-drlab-core"
    assert cu.tmux_name("my.chat:1", "abc") == "claude-my-chat-1"
    assert cu.tmux_name(None, "2296e6b7-452a") == "claude-2296e6b7"


def test_restart_tmux_resumes_inside_the_same_pane(monkeypatch):
    killed, typed = [], []
    state = {"claude": True}
    monkeypatch.setattr(cu.os, "kill", lambda pid, sig: (killed.append(pid), state.update(claude=False)))
    monkeypatch.setattr(cu._inject, "tmux_sessions", lambda env=None: {
        "claude-x": {"name": "claude-x", "command": "2.1.296" if state["claude"] else "zsh"}})

    def send(name, text, enter, env=None):
        typed.append((name, text))
        state["claude"] = True
        return True, ""

    monkeypatch.setattr(cu._inject, "send_tmux", send)
    monkeypatch.setattr(cu.time, "sleep", lambda s: None)
    ok, how = cu.restart_tmux(200, "claude-x", "U", command="claude --resume U -n x", stop_s=5, back_s=5)
    assert ok and killed == [200] and typed == [("claude-x", "claude --resume U -n x")]
    # no new session is made: the only tmux call that changes anything is the typing into its pane


def test_restart_tmux_reports_a_claude_that_will_not_stop(monkeypatch):
    monkeypatch.setattr(cu.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(cu._inject, "tmux_sessions", lambda env=None: {"claude-x": {"command": "2.1.296"}})
    monkeypatch.setattr(cu.time, "sleep", lambda s: None)
    ok, how = cu.restart_tmux(200, "claude-x", "U", command="claude", stop_s=0.01, back_s=0.01)
    assert not ok and "did not stop" in how


def test_restart_into_tmux_types_one_line_into_the_same_tab(monkeypatch):
    killed, typed = [], []
    state = {"claude": True, "tmux": False}
    monkeypatch.setattr(cu._chat, "server_context", lambda env=None: "iterm")
    monkeypatch.setattr(cu, "screen_of", lambda kind, h, env=None: "⏵⏵ accept edits on")
    monkeypatch.setattr(cu, "process_args", lambda pid, env=None: "claude --allow-dangerously-skip-permissions")
    monkeypatch.setattr(cu, "transcript_of", lambda cid, projects=None: None)
    monkeypatch.setattr(cu.os, "kill", lambda pid, sig: (killed.append(pid), state.update(claude=False)))
    monkeypatch.setattr(cu._inject, "iterm_sessions", lambda env=None: {
        "TAB": {"name": "vision (node)" if state["claude"] else "vision (-zsh)"}})
    monkeypatch.setattr(cu._inject, "tmux_sessions", lambda env=None: (
        {"claude-vision": {"command": "2.1.296"}} if state["tmux"] else {}))

    def send(sid, text, enter, env=None):
        typed.append((sid, text))
        state["tmux"] = True
        return True, ""

    monkeypatch.setattr(cu._inject, "send_iterm", send)
    monkeypatch.setattr(cu.time, "sleep", lambda s: None)
    ok, detail = cu.restart_into_tmux(300, "TAB", "/work/drlab", "C5", title="vision", stop_s=5, back_s=5)
    assert ok and detail == {"tmux": "claude-vision", "tab": "TAB", "shown": "plain tmux"}
    assert killed == [300] and len(typed) == 1 and typed[0][0] == "TAB"
    line = typed[0][1]
    assert "new-session -d -s claude-vision" in line and "attach -t claude-vision" in line
    assert "set-titles on" in line and "--remote-control vision" in line
    assert "--permission-mode acceptEdits" in line and "--resume C5" in line


def test_restart_into_tmux_refuses_a_server_that_cannot_script_iterm(monkeypatch):
    monkeypatch.setattr(cu._chat, "server_context", lambda env=None: "outside")
    monkeypatch.setattr(cu.os, "kill", lambda pid, sig: (_ for _ in ()).throw(AssertionError("killed")))
    ok, detail = cu.restart_into_tmux(300, "TAB", "/w", "C5", title="vision")
    assert not ok and detail["step"] == "check" and "iTerm2" in detail["why"]
