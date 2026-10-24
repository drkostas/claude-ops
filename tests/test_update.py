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


RULE = "\x1b[38;5;244m" + "─" * 40
FOOTER = "\x1b[39m  \x1b[38;5;211m⏵⏵ bypass permissions on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m"


def test_a_suggested_prompt_is_not_typed_text():
    # Claude Code draws a suggested next prompt dim after the ❯; real typing is plain text
    suggested = "\n".join([RULE, "\x1b[39m❯\xa0\x1b[2mrun the tests again\x1b[0m", RULE, FOOTER])
    assert cu.empty_prompt(cu.plain_screen(suggested)) == (True, "its input line is empty")
    typed = "\n".join([RULE, "\x1b[38;5;246m\x1b[48;5;237m❯ \x1b[38;5;231mhalf a sentence\x1b[39m", RULE, FOOTER])
    assert cu.empty_prompt(cu.plain_screen(typed)) == (False, cu.TYPED)
    both = "\n".join([RULE, "\x1b[39m❯ half\x1b[2m a suggestion\x1b[22m", RULE])
    assert cu.plain_screen(both).splitlines()[1] == "❯ half"


def test_plain_screen_keeps_everything_off_the_input_line():
    # a dim hint outside the input line still names a dialog
    dialog = "Allow this edit?\n\x1b[2mEnter to confirm · Esc to cancel\x1b[0m"
    assert cu.plain_screen(dialog) == "Allow this edit?\nEnter to confirm · Esc to cancel"
    assert cu.empty_prompt(cu.plain_screen(dialog)) == (False, cu.ASKING)
    # a 2 inside a 24-bit colour is a colour, not dim, and dim carries over a line break
    assert cu.plain_screen("\x1b[38;2;2;2;2m❯ typed") == "❯ typed"
    assert cu.plain_screen("\x1b[2mhint\n❯\xa0ghost\x1b[0m") == "hint\n❯"
    assert cu.plain_screen("\x1b]8;;https://example.org\x07link\x1b]8;;\x07") == "link"


def test_screen_of_a_tmux_pane_reads_it_styled(monkeypatch):
    seen = []

    def fake(argv, **k):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "\x1b[39m❯\xa0\x1b[2mrun the tests again\x1b[0m\n", "")

    monkeypatch.setattr(subprocess, "run", fake)
    assert cu.screen_of("tmux", "chat-a") == "❯\xa0\n"
    assert "-e" in seen[0]


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
    # the newest bridge_status decides; a note that it could not start here is not the user turning it off
    assert cu.remote_control_of(t) is True
    t.write_text(json.dumps(active) + "\n" + json.dumps(
        {"type": "system", "subtype": "bridge_status", "content": "/remote-control is off"}) + "\n")
    assert cu.remote_control_of(t) is False
    t.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    assert cu.remote_control_of(t) is False
    assert cu.remote_control_of(None) is False
    assert cu.remote_control_of(tmp_path / "missing.jsonl") is False


def test_resume_line_keeps_remote_control_switched_on_inside_the_chat():
    started = "claude --allow-dangerously-skip-permissions -n ops-core"
    assert (cu.resume_line(started, "U", remote_control=True)
            == "claude --allow-dangerously-skip-permissions -n ops-core --remote-control ops-core --resume U")
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
        {"type": "custom-title", "customTitle": "stats-chat"})) + "\n")
    assert cu.title_of(t) == "stats-chat"
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


def test_a_running_workflow_keeps_a_chat_busy(tmp_path):
    import os
    projects, proj = _chat_dir(tmp_path)
    run = proj / "C1" / "subagents" / "workflows" / "wf_0001"
    run.mkdir(parents=True)
    agent = run / "agent-b2.jsonl"
    agent.write_text("{}\n")
    assert cu.background_agent("C1", projects=projects) == "workflows/wf_0001/agent-b2", \
        "a workflow's agent writing one folder deeper is working"
    journal = run / "journal.jsonl"
    journal.write_text('{"type":"started"}\n')
    old = agent.stat().st_mtime - 600
    os.utime(agent, (old, old))
    assert cu.background_agent("C1", projects=projects) == "workflow wf_0001", \
        "between phases no agent writes, and the run still counts while its journal is recent"
    state = proj / "C1" / "workflows" / "wf_0001.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"status": "completed"}')
    assert cu.background_agent("C1", projects=projects) is None, "a run with a final state is over"
    state.unlink()
    stale = journal.stat().st_mtime - 7200
    os.utime(journal, (stale, stale))
    assert cu.background_agent("C1", projects=projects) is None, \
        "a run whose process died without a final state does not hold the chat for ever"


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
    assert cu.tmux_name("ops-core", "abc") == "claude-ops-core"
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


def _renamed(tmp_path, chat_id="NEW"):
    """A transcript started as `soma` and renamed `soma-core` later."""
    t = tmp_path / f"{chat_id}.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in (
        {"type": "custom-title", "customTitle": "soma"}, {"type": "user", "message": {"content": "hi"}},
        {"type": "custom-title", "customTitle": "soma-core"})) + "\n")
    return t


def test_restart_iterm_keeps_a_rename(monkeypatch, tmp_path):
    typed, state = [], {"claude": True}
    t = _renamed(tmp_path)
    monkeypatch.setattr(cu, "transcript_of", lambda chat_id, projects=None: t)
    monkeypatch.setattr(cu, "screen_of", lambda kind, h, env=None: "")
    monkeypatch.setattr(cu, "process_args", lambda pid, env=None: "claude -n soma --remote-control soma --resume OLD")
    monkeypatch.setattr(cu.os, "kill", lambda pid, sig: state.update(claude=False))
    monkeypatch.setattr(cu._inject, "iterm_sessions", lambda env=None: {
        "ABC": {"name": "soma (2.1.294)" if state["claude"] else "soma (-zsh)"}})
    monkeypatch.setattr(cu._inject, "send_iterm", lambda sid, text, enter, env=None: (
        typed.append(text), state.update(claude=True)) and (True, "sent"))
    monkeypatch.setattr(cu.time, "sleep", lambda s: None)
    cu.restart_iterm(100, "ABC", "/work/soma", "NEW", stop_s=5, back_s=5)
    # the old -n would be written back as the chat's title and undo the rename
    assert typed == ["cd /work/soma && claude -n soma-core --remote-control soma-core --resume NEW"]


def test_restart_tmux_keeps_a_rename(monkeypatch, tmp_path):
    typed, state = [], {"claude": True}
    t = _renamed(tmp_path)
    monkeypatch.setattr(cu, "transcript_of", lambda chat_id, projects=None: t)
    monkeypatch.setattr(cu, "screen_of", lambda kind, h, env=None: "")
    monkeypatch.setattr(cu, "process_args", lambda pid, env=None: "claude -n soma --resume OLD")
    monkeypatch.setattr(cu.os, "kill", lambda pid, sig: state.update(claude=False))
    monkeypatch.setattr(cu._inject, "tmux_sessions", lambda env=None: {
        "claude-soma": {"name": "claude-soma", "command": "2.1.296" if state["claude"] else "zsh"}})
    monkeypatch.setattr(cu._inject, "send_tmux", lambda name, text, enter, env=None: (
        typed.append(text), state.update(claude=True)) and (True, ""))
    monkeypatch.setattr(cu.time, "sleep", lambda s: None)
    cu.restart_tmux(200, "claude-soma", "NEW", stop_s=5, back_s=5)
    assert typed == ["claude -n soma-core --resume NEW"]


def test_title_and_remote_control_are_read_from_the_end(tmp_path):
    t = tmp_path / "long.jsonl"
    filler = json.dumps({"type": "assistant", "message": {"content": "x" * 900}}) + "\n"
    with t.open("w") as f:
        f.write(json.dumps({"type": "custom-title", "customTitle": "first"}) + "\n")
        f.write(json.dumps({"type": "system", "subtype": "bridge_status", "content": "/remote-control is active"}) + "\n")
        f.write(filler * 3000)   # about 3 MB, so the records sit several read blocks back
        f.write(json.dumps({"type": "custom-title", "customTitle": "renamed"}) + "\n")
        f.write(filler * 3000)
    assert cu.title_of(t) == "renamed"
    assert cu.remote_control_of(t) is True
    with t.open("a") as f:
        f.write(json.dumps({"type": "system", "subtype": "bridge_status", "content": "/remote-control is off"}) + "\n")
    assert cu.remote_control_of(t) is False


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
    ok, detail = cu.restart_into_tmux(300, "TAB", "/work/project", "C5", title="vision", stop_s=5, back_s=5)
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


def test_front_app_gives_the_name_or_the_bundle_id(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        out = {"front": "ASN:0x0-0x1234:\n",
               "name": '"LSDisplayName"="iTerm2"\n',
               "bundleid": '"CFBundleIdentifier"="com.googlecode.iterm2"\n'}
        key = "front" if argv[1] == "front" else argv[3]
        return subprocess.CompletedProcess(argv, 0, out[key], "")

    monkeypatch.setattr(cu.subprocess, "run", run)
    assert cu.front_app() == "iTerm2"
    assert cu.front_app(bundle_id=True) == "com.googlecode.iterm2"


def _assistant(model, effort, *, requested=None, sidechain=False):
    r = {"type": "assistant", "isSidechain": sidechain, "message": {"model": model}, "effort": effort}
    if requested:
        r["requestedModel"] = requested
    return json.dumps(r)


def test_model_of_reads_the_newest_turn_of_the_chat_itself(tmp_path):
    t = tmp_path / "c.jsonl"
    t.write_text("\n".join([
        _assistant("claude-opus-5-5", "medium"),
        json.dumps({"type": "user", "message": {"content": "/model"}}),
        _assistant("claude-sonnet-5", "high", requested="claude-sonnet-5[1m]"),
        _assistant("claude-haiku-5-5", "low", sidechain=True),      # a subagent's turn
        _assistant("<synthetic>", None),
    ]) + "\n")
    assert cu.model_of(t) == ("claude-sonnet-5[1m]", "high")
    assert cu.model_of(None) == (None, None)
    assert cu.model_of(tmp_path / "missing.jsonl") == (None, None)
    (tmp_path / "old.jsonl").write_text(_assistant("claude-opus-4-8", None) + "\n")
    assert cu.model_of(tmp_path / "old.jsonl") == ("claude-opus-4-8", None)


def test_model_of_reads_backwards_across_blocks(tmp_path):
    t = tmp_path / "long.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 300}})
    t.write_text(_assistant("claude-opus-5-5", "xhigh") + "\n" + "\n".join([filler] * 50) + "\n")
    lines = list(cu._lines_from_end(t, block=97))
    assert len(lines) == 51 and json.loads(lines[-1])["effort"] == "xhigh"
    assert cu.model_of(t) == ("claude-opus-5-5", "xhigh")


def test_resume_line_keeps_the_model_and_effort_chosen_inside_the_chat():
    started = "claude --allow-dangerously-skip-permissions -n ops --model opus --effort medium"
    assert (cu.resume_line(started, "U", model="claude-sonnet-5", effort="high")
            == "claude --allow-dangerously-skip-permissions -n ops --model claude-sonnet-5 "
               "--effort high --resume U")
    assert (cu.resume_line("claude -n ops", "U", model="claude-opus-5-5[1m]", effort="xhigh")
            == "claude -n ops --model 'claude-opus-5-5[1m]' --effort xhigh --resume U")
    assert cu.resume_line("claude -n ops --model opus", "U") == "claude -n ops --model opus --resume U"
    # a settings file names its own model, so a chat started with one keeps it
    assert (cu.resume_line("claude --settings /s.json -n ops", "U", model="local-model", effort="high")
            == "claude --settings /s.json -n ops --resume U")


def test_a_restart_keeps_the_name_the_chat_has_now():
    """A chat renamed after it started still has its old -n in its command line."""
    from claude_ops import update
    args = "claude -n old-name --remote-control old-name --model opus"
    line = update.resume_line(args, "abc", name="new-name")
    assert "-n new-name" in line and "--remote-control new-name" in line and "old-name" not in line
    assert update.resume_line("claude --model opus", "abc", name="fresh").count("-n fresh") == 1
    assert "-n old-name" in update.resume_line(args, "abc"), "no name given: the start flags stand"


def test_unfinished_background_lists_commands_with_no_notification(tmp_path):
    import json
    from claude_ops import update
    rows = [
        {"type": "assistant", "timestamp": "t1", "message": {"content": [{"type": "tool_use", "id": "toolu_A",
         "input": {"command": "watch-a.sh", "description": "watch A", "run_in_background": True}}]}},
        {"type": "assistant", "timestamp": "t2", "message": {"content": [{"type": "tool_use", "id": "toolu_B",
         "input": {"command": "build.sh", "description": "build", "run_in_background": True}}]}},
        {"type": "assistant", "timestamp": "t3", "message": {"content": [{"type": "tool_use", "id": "toolu_C",
         "input": {"command": "ls", "description": "list"}}]}},
        {"type": "user", "message": {"content": "<task-notification>\n<task-id>b1</task-id>\n"
         "<tool-use-id>toolu_B</tool-use-id>\n<status>completed</status>\n</task-notification>"}},
    ]
    f = tmp_path / "chat.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    got = update.unfinished_background(f)
    assert [t["tool_use_id"] for t in got] == ["toolu_A"]
    assert got[0]["command"] == "watch-a.sh" and got[0]["description"] == "watch A" and got[0]["at"] == "t1"
    assert update.unfinished_background(None) == [] and update.unfinished_background(tmp_path / "x") == []
    assert update.unfinished_background(f, since="t2") == [], "a command started before `since` is not listed"


def test_last_start_is_the_newest_session_start_record(tmp_path):
    import json
    from claude_ops import update
    rows = [{"type": "attachment", "timestamp": "t1", "attachment": {"type": "hook_success", "hookName": "SessionStart:startup", "hookEvent": "SessionStart"}},
            {"type": "user", "timestamp": "t2", "message": {"content": "SessionStart is just a word here"}},
            {"type": "attachment", "timestamp": "t3", "attachment": {"type": "hook_success", "hookName": "SessionStart:resume", "hookEvent": "SessionStart"}}]
    f = tmp_path / "c.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert update.last_start(f) == "t3" and update.last_start(None) is None


def test_files_seen_is_what_the_chat_wrote_or_read_whole(tmp_path):
    a, b, c, d, e, g = (tmp_path / n for n in ("a.md", "b.md", "c.md", "d.md", "e.md", "g.md"))

    def res(at, result):
        return {"type": "user", "timestamp": at, "toolUseResult": result,
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}}
    rows = [
        res("2026-10-01T10:00:00.000Z", {"filePath": str(a), "oldString": "x", "newString": "y",
                                         "structuredPatch": [], "userModified": False}),
        res("2026-10-01T10:01:00.000Z", {"type": "create", "filePath": str(b), "content": "x",
                                         "structuredPatch": []}),
        res("2026-10-01T10:02:00.000Z", {"type": "text", "file": {"filePath": str(c), "content": "x",
                                         "numLines": 3, "startLine": 1, "totalLines": 3}}),
        res("2026-10-01T10:03:00.000Z", {"type": "text", "file": {"filePath": str(d), "content": "x",
                                         "numLines": 2, "startLine": 1, "totalLines": 9}}),
        res("2026-10-01T10:04:00.000Z", {"filePath": str(e), "oldString": "x", "newString": "y",
                                         "structuredPatch": [], "userModified": True}),
        res("2026-10-01T10:05:00.000Z", f"Error: String to replace not found in {g}, filePath"),
        res("2026-10-01T10:06:00.000Z", {"filePath": str(a), "edits": [], "structuredPatch": []}),
    ]
    f = tmp_path / "chat.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    seen = cu.files_seen(f)
    assert seen == {a.resolve(): "2026-10-01T10:06:00.000Z", b.resolve(): "2026-10-01T10:01:00.000Z",
                    c.resolve(): "2026-10-01T10:02:00.000Z"}, seen
    assert cu.files_seen(f, since="2026-10-01T10:01:30.000Z") == {
        a.resolve(): "2026-10-01T10:06:00.000Z", c.resolve(): "2026-10-01T10:02:00.000Z"}
    assert cu.files_seen(None) == {} and cu.files_seen(tmp_path / "missing.jsonl") == {}


def test_tools_held_is_what_the_conversation_loaded_since_its_last_compaction(tmp_path):
    def loaded(at, *tools):
        return {"type": "attachment", "timestamp": at, "attachment": {"type": "deferred_tools_record", "entries": [
            {"name": n, "description": d, "input_schema": {"type": "object", "properties": {a: {} for a in args}}}
            for n, d, args in tools]}}
    compacted = {"type": "system", "subtype": "compact_boundary", "timestamp": "t2", "content": "Conversation compacted"}
    rows = [
        loaded("t1", ("mcp__srv__ask", "before", ["a"]), ("mcp__srv__gone", "x", [])),
        compacted,
        loaded("t3", ("mcp__srv__ask", "after", ["a", "b"])),
        # a message that only names the record types is not one
        {"type": "user", "timestamp": "t4", "message": {"content": "deferred_tools_record compact_boundary"}},
        loaded("t5", ("mcp__srv__list", "list", []), ("mcp__srv__ask", "newest", ["a", "b", "c"])),
    ]
    f = tmp_path / "chat.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    held = cu.tools_held(f)
    assert held == {"mcp__srv__ask": {"description": "newest", "args": {"a", "b", "c"}},
                    "mcp__srv__list": {"description": "list", "args": set()}}, held
    f.write_text("\n".join(json.dumps(r) for r in rows + [compacted]) + "\n")
    assert cu.tools_held(f) == {}, "a compaction after the last load leaves nothing held"
    assert cu.tools_held(None) == {} and cu.tools_held(tmp_path / "missing.jsonl") == {}


WRAP = "/bin/zsh -c source /x/snapshot.sh 2>/dev/null || true && setopt NO_EXTENDED_GLOB && eval '{}' < /dev/null && pwd -P >| /tmp/claude-ab12-cwd"


def test_a_wait_loop_that_only_reads_is_a_waiter():
    assert cu.is_waiter(WRAP.format('until grep -q "ship exit" /tmp/x/ship.log; do sleep 60; done; tail -15 /tmp/x/ship.log'))
    assert cu.is_waiter(WRAP.format("until [ -f /tmp/x/READY ]; do sleep 10; done; cat /tmp/x/PORT"))
    assert cu.is_waiter(WRAP.format(
        "until grep -q \"train exit\" /tmp/x/t.log 2>/dev/null; do sleep 60; done; "
        "rtk proxy bash -c '\"'\"'grep -E \"RED|MERGED\" /tmp/x/t.log | head -12'\"'\"'"))
    assert cu.is_waiter(WRAP.format("while pgrep -x make; do sleep 5; done"))


def test_a_wait_that_then_acts_or_writes_is_not_a_waiter():
    assert not cu.is_waiter(WRAP.format("until gh pr view 1 --json state | grep -q MERGED; do sleep 60; done && ./deploy.sh"))
    assert not cu.is_waiter(WRAP.format("until [ -f x ]; do sleep 5; done; cat x > y"))
    assert not cu.is_waiter(WRAP.format("until [ -f x ]; do make; sleep 5; done"))
    assert not cu.is_waiter(WRAP.format("python3 long_job.py"))
    assert not cu.is_waiter(WRAP.format("until [ -f x ]; do sleep 5; done; rtk proxy bash -c 'rm -rf x'"))


def test_a_report_loop_after_the_wait_still_only_reads():
    # the shape a chat writes to report a train's pull requests once its log ends
    assert cu.is_waiter(WRAP.format(
        'until grep -q "train exit" /tmp/x/t.log; do sleep 60; done; tail -15 /tmp/x/t.log; '
        'for n in 11 12; do echo "$n $(gh pr view $n --json state -q .state)"; done'))
    assert cu.is_waiter(WRAP.format(
        "until [ -f x ]; do sleep 5; done; if grep -q ok x; then echo ok; else echo no; fi"))


def test_a_report_that_sets_a_variable_and_tests_it_only_reads():
    assert cu.is_waiter(WRAP.format(
        'until grep -qE "exit|^RED" /tmp/x/t.log 2>/dev/null || { L=$(ls -t /tmp/x/run-*.log 2>/dev/null | head -1); '
        '[ -n "$L" ] && grep -qE "FAIL" "$L"; }; do sleep 30; done; tail -8 /tmp/x/t.log | cut -c1-220; '
        'L=$(ls -t /tmp/x/run-*.log 2>/dev/null | head -1); [ -n "$L" ] && grep -E "FAIL" "$L"'))
    assert not cu.is_waiter(WRAP.format('until [ -f x ]; do sleep 5; done; L=$(rm -rf x)'))
    assert not cu.is_waiter(WRAP.format('until [ -f x ]; do sleep 5; done; { cat x; make; }'))


def test_a_substitution_or_loop_that_acts_is_not_a_waiter():
    assert not cu.is_waiter(WRAP.format('until [ -f x ]; do sleep 5; done; echo "$(rm -rf x)"'))
    assert not cu.is_waiter(WRAP.format("until [ -f x ]; do sleep 5; done; echo `rm x`"))
    assert not cu.is_waiter(WRAP.format("until [ -f x ]; do sleep 5; done; for n in 1 2; do gh pr merge $n; done"))
    assert not cu.is_waiter(WRAP.format('until [ -f x ]; do sleep 5; done; echo "$(cat x'))
    assert not cu.is_waiter(WRAP.format("until [ -f x ]; do sleep 5; done; gh pr close 1"))


def test_idle_looks_through_shells_that_only_wait():
    procs = {**MCP_ONLY, 103: {"ppid": 100, "comm": "/bin/zsh"}, 104: {"ppid": 103, "comm": "sleep"}}
    wait = {103: WRAP.format('until grep -q done /tmp/x.log; do sleep 60; done; tail -5 /tmp/x.log')}
    assert cu.idle(100, procs, ago(120), ago(110), NOW, commands=wait)[0]
    assert not cu.idle(100, procs, ago(120), ago(110), NOW)[0]            # no command lines: counted
    printing = {**procs, 104: {"ppid": 103, "comm": "tail"}}
    assert not cu.idle(100, printing, ago(120), ago(110), NOW, commands=wait)[0]   # past the loop
    job = {103: WRAP.format("python3 long_job.py")}
    assert not cu.idle(100, procs, ago(120), ago(110), NOW, commands=job)[0]


def test_command_lines_reads_one_ps():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "  103 /bin/zsh -c eval 'x'\n  104 sleep 60\n", "")
    assert cu.command_lines([103, 104], run=run) == {103: "/bin/zsh -c eval 'x'", 104: "sleep 60"}
    assert calls == [["/bin/ps", "-o", "pid=,command=", "-p", "103,104"]]
    assert cu.command_lines([], run=run) == {}


def _err(text, error="server_error", **extra):
    return {"type": "assistant", "isApiErrorMessage": True, "error": error, "uuid": "E1",
            "timestamp": "2026-10-16T18:50:28.314Z",
            "message": {"content": [{"type": "text", "text": text}]}, **extra}


def _write(path, *records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_a_turn_cut_by_a_lost_connection_is_a_transient_stop(tmp_path):
    lost = "API Error: Connection lost mid-response. The response above may be incomplete."
    t = _write(tmp_path / "c.jsonl", {"type": "user"}, _err(lost),
               {"type": "system", "subtype": "turn_duration"}, {"type": "last-prompt"})
    assert cu.stopped_on_api_error(t) == {"uuid": "E1", "at": "2026-10-16T18:50:28.314Z",
                                         "error": "server_error", "text": lost}
    socket = _write(tmp_path / "s.jsonl", _err("API Error: The socket connection was closed "
                                               "unexpectedly.", error="unknown"))
    assert cu.stopped_on_api_error(socket)["error"] == "unknown"
    busy = _write(tmp_path / "b.jsonl", _err("API Error: Server is temporarily limiting requests "
                                             "(not your usage limit)", error="rate_limit"))
    assert cu.stopped_on_api_error(busy) is not None


def test_a_usage_limit_or_a_bad_request_is_not_retried(tmp_path):
    for text, error in (("You've hit your session limit · resets 9pm", "rate_limit"),
                        ("Prompt is too long", "invalid_request"),
                        ("Credit balance is too low", "billing_error"),
                        ("Login expired · Please run /login", "authentication_failed"),
                        ("API Error: Prompt is too long", "invalid_request")):
        t = _write(tmp_path / "c.jsonl", _err(text, error=error))
        assert cu.stopped_on_api_error(t) is None, text


def test_anything_after_the_error_means_the_chat_moved_on(tmp_path):
    lost = _err("API Error: Connection lost mid-response.")
    assert cu.stopped_on_api_error(_write(tmp_path / "u.jsonl", lost, {"type": "user"})) is None
    assert cu.stopped_on_api_error(_write(tmp_path / "q.jsonl", lost, {"type": "queue-operation"})) is None
    assert cu.stopped_on_api_error(_write(tmp_path / "a.jsonl", lost, {"type": "assistant"})) is None
    # a subagent's record is not the conversation's own turn
    side = _write(tmp_path / "side.jsonl", lost, {"type": "assistant", "isSidechain": True})
    assert cu.stopped_on_api_error(side) is not None
    assert cu.stopped_on_api_error(None) is None
    assert cu.stopped_on_api_error(tmp_path / "missing.jsonl") is None


# a background grep a chat started and forgot, as Claude Code's shell wraps it
GREP = ("/bin/zsh -c source /tmp/snapshot.sh 2>/dev/null || true && eval 'rtk proxy bash -c '\"'\"'grep -rl x "
        "~/Projects/*/ | head -20'\"'\"'' < /dev/null && pwd -P >| /tmp/claude-cwd")
PROMPT = "done 7:24 PM\n❯ \n───\n  ⏵⏵ bypass permissions on · 1 shell"


def test_shown_command_is_the_command_the_chat_ran():
    assert cu.shown_command(GREP) == "rtk proxy bash -c 'grep -rl x ~/Projects/*/ | head -20'"
    assert cu.shown_command("python3   long_job.py") == "python3 long_job.py"
    assert cu.shown_command("x" * 200, limit=10) == "x" * 9 + "…"


def test_forgotten_shells_names_an_old_shell_under_an_idle_prompt():
    procs = {**MCP_ONLY, 103: {"ppid": 100, "comm": "/bin/zsh", "started": ago(5 * 3600)},
             104: {"ppid": 103, "comm": "grep", "started": ago(5 * 3600)}}
    got = cu.forgotten_shells(100, procs, PROMPT, NOW, commands={103: GREP})
    assert got == [{"pid": 103, "age_s": 5 * 3600, "command": cu.shown_command(GREP)}]
    # still working on its turn: the shell is the turn's own command, not forgotten
    assert cu.forgotten_shells(100, procs, "⏺ Running…\n✻ Churning", NOW, commands={103: GREP}) == []
    # younger than the limit
    assert cu.forgotten_shells(100, procs, PROMPT, NOW, older_than_s=6 * 3600, commands={103: GREP}) == []
    # a shell that only waits is not forgotten: it ends by itself
    waiting = {**procs, 104: {"ppid": 103, "comm": "sleep", "started": ago(30)}}
    wait = {103: WRAP.format("until [ -f /tmp/x ]; do sleep 30; done")}
    assert cu.forgotten_shells(100, waiting, PROMPT, NOW, commands=wait) == []


def test_idle_names_the_shell_it_waits_for():
    procs = {**MCP_ONLY, 103: {"ppid": 100, "comm": "/bin/zsh", "started": ago(4 * 3600 + 47 * 60)}}
    ok, why = cu.idle(100, procs, ago(120), ago(110), NOW)
    assert not ok and why == "it is running a command (pid 103, for 4h47m)"
    assert cu.idle(100, WITH_CMD, ago(120), ago(110), NOW)[1] == "it is running a command"


# ----------------------------------------------------------------------------- unreachable questions

LOGGED_OUT = "⏺ Login expired · Please run /login\n"
RC_GONE = "⏺ Remote Control disconnected — OAuth token unavailable — run /login to restore Remote Control\n"
RC_BACK = "  /remote-control is active · Continue here, on your phone, or at https://example.org/x\n"
ASKING_SCREEN = "❯ 1. Digest them (Recommended)\n  2. Push them too\nEnter to select · Esc to cancel\n"


def test_signed_out_reads_the_newest_word_on_the_screen():
    assert cu.signed_out("work\n" + LOGGED_OUT + "❯ ") == "⏺ Login expired · Please run /login"
    assert "OAuth token unavailable" in cu.signed_out(RC_GONE + ASKING_SCREEN)
    assert cu.signed_out("⏺ Unknown command: /remote-control\n❯ ") is not None
    # Remote Control came back after the loss: the newest word wins
    assert cu.signed_out(RC_GONE + RC_BACK + "❯ ") is None
    assert cu.signed_out("all fine\n❯ ") is None and cu.signed_out(None) is None


def test_a_question_is_unreachable_only_when_signed_out_and_nobody_is_there():
    screen = RC_GONE + ASKING_SCREEN
    assert cu.question_unreachable(screen, 3600)
    assert not cu.question_unreachable(screen, 60), "somebody used the computer a minute ago"
    assert not cu.question_unreachable(screen, None), "unknown input time: somebody may be there"
    assert not cu.question_unreachable(ASKING_SCREEN, 3600), "a phone still reaches it"


def test_ready_lets_an_unreachable_question_go_and_nothing_else(tmp_path, monkeypatch):
    projects, _ = _chat_dir(tmp_path)
    monkeypatch.setattr(cu, "last_activity", lambda p: ago(900))
    screen = RC_GONE + ASKING_SCREEN
    ok, why = cu.ready(100, MCP_ONLY, "C1", screen, ago(3600), NOW, projects=projects)
    assert not ok and "question" in why, "a question holds a restart by default"
    ok, why = cu.ready(100, MCP_ONLY, "C1", screen, ago(3600), NOW, projects=projects, question_blocks=False)
    assert ok and "nobody can answer" in why, why
    # the rest still holds it: a command running, or a turn that wrote a moment ago
    assert not cu.ready(100, WITH_CMD, "C1", screen, ago(3600), NOW, projects=projects,
                        question_blocks=False)[0]
    monkeypatch.setattr(cu, "last_activity", lambda p: ago(30))
    assert not cu.ready(100, MCP_ONLY, "C1", screen, ago(3600), NOW, projects=projects,
                        question_blocks=False)[0]


# ----------------------------------------------------------------------------- stopping a forgotten shell

STARTED = ago(3 * 86400)
SHELL_TABLE = {**MCP_ONLY, 103: {"ppid": 100, "comm": "/bin/zsh", "started": STARTED},
               104: {"ppid": 103, "comm": "grep", "started": STARTED},
               105: {"ppid": 104, "comm": "head", "started": STARTED}}


def _signals():
    sent = []
    return sent, (lambda pid, sig: sent.append((pid, sig)))


def test_stop_shell_stops_the_shell_and_everything_under_it():
    sent, kill = _signals()
    ok, said = cu.stop_shell(103, SHELL_TABLE, started=STARTED, parent=100, kill=kill,
                             sleep=lambda s: None, refresh=lambda: dict(MCP_ONLY))
    assert ok and "pid 103 and 2 process(es)" in said, said
    assert sent == [(104, cu.signal.SIGTERM), (105, cu.signal.SIGTERM), (103, cu.signal.SIGTERM)], \
        "what runs under the shell goes first, so the shell does not start anything new"
    # what ignored SIGTERM gets SIGKILL, and only when it is still the same process
    sent, kill = _signals()
    still = {**MCP_ONLY, 104: SHELL_TABLE[104], 105: {**SHELL_TABLE[105], "started": NOW}}
    ok, said = cu.stop_shell(103, SHELL_TABLE, started=STARTED, kill=kill, sleep=lambda s: None,
                             refresh=lambda: still)
    assert ok and "1 of them only with SIGKILL" in said, said
    assert sent[-1] == (104, cu.signal.SIGKILL) and (105, cu.signal.SIGKILL) not in sent


def test_stop_shell_never_touches_a_pid_another_program_took():
    sent, kill = _signals()
    ok, said = cu.stop_shell(103, SHELL_TABLE, started=ago(60), kill=kill, sleep=lambda s: None)
    assert not ok and "another process" in said and sent == []
    ok, said = cu.stop_shell(103, SHELL_TABLE, started=STARTED, parent=999, kill=kill, sleep=lambda s: None)
    assert not ok and "no longer under process 999" in said and sent == []
    ok, said = cu.stop_shell(777, SHELL_TABLE, started=STARTED, kill=kill, sleep=lambda s: None)
    assert ok and "already ended" in said and sent == []
