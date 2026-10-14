"""Starting and finding named chats: their commands, their Remote Control link, one per name."""
import json
import os
import time

from claude_ops import chat, sessions


def test_commands_carry_the_mode_only_when_given():
    assert chat.claude_command("soma", "bypassPermissions") == \
        "claude -n soma --remote-control soma --permission-mode bypassPermissions"
    assert chat.claude_command("soma", None) == "claude -n soma --remote-control soma"
    assert chat.claude_command("soma", None, remote_control=False) == "claude -n soma"
    assert (chat.claude_command("soma", None, remote_control=False, settings="/s.json")
            == "claude --settings /s.json -n soma")
    assert (chat.resume_command("soma", "ID", None, remote_control=False, settings="/s.json")
            == "claude --settings /s.json --resume ID -n soma")
    assert chat.resume_command("soma", "ID", "plan") == \
        "claude --resume ID -n soma --remote-control soma --permission-mode plan"
    assert chat.resume_command("soma", "ID", None) == "claude --resume ID -n soma --remote-control soma"


def test_rc_url_is_the_last_bridge_status(tmp_path):
    t = tmp_path / "c.jsonl"
    rows = [{"type": "system", "subtype": "bridge_status", "url": "https://claude.ai/code/first"},
            {"type": "user", "timestamp": "x"},
            {"type": "system", "subtype": "bridge_status", "url": "https://claude.ai/code/last"}]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert sessions.rc_url(str(t)) == "https://claude.ai/code/last"
    t.write_text(json.dumps({"type": "user"}) + "\n")
    os.utime(t, (time.time() + 5, time.time() + 5))      # a new mtime is read again
    assert sessions.rc_url(str(t)) is None
    assert sessions.rc_url(str(tmp_path / "missing")) is None


def test_rc_url_reads_only_what_was_added(tmp_path, monkeypatch):
    t = tmp_path / "c.jsonl"
    t.write_text(json.dumps({"type": "system", "subtype": "bridge_status", "url": "https://claude.ai/code/one"}) + "\n")
    assert sessions.rc_url(str(t)) == "https://claude.ai/code/one"
    parsed = []
    real = sessions._record
    monkeypatch.setattr(sessions, "_record", lambda raw: parsed.append(raw) or real(raw))
    with t.open("a") as f:
        f.write(json.dumps({"type": "system", "subtype": "bridge_status", "url": "https://claude.ai/code/two"}) + "\n")
    os.utime(t, (time.time() + 5, time.time() + 5))
    assert sessions.rc_url(str(t)) == "https://claude.ai/code/two"
    assert len(parsed) == 1  # the first link was not read again


def test_pick_keeps_the_most_recent_conversation_per_name():
    idx = [{"title": "soma", "path": "/p/a.jsonl", "active_at": 1},
           {"title": "soma", "path": "/p/b.jsonl", "active_at": 5},
           {"title": "soma", "path": "/p/x/subagents/agent-1.jsonl", "active_at": 9},
           {"title": None, "path": "/p/c.jsonl", "active_at": 9}]
    got = sessions.pick(idx)
    assert list(got) == ["soma"] and got["soma"]["path"] == "/p/b.jsonl"


def test_project_for_is_the_closest_folder():
    projects = [{"project": "web", "path": "/w"}, {"project": "web-docs", "path": "/w/docs/"}]
    assert sessions.project_for("/w/docs/a", projects) == "web-docs"
    assert sessions.project_for("/w/src", projects) == "web"
    assert sessions.project_for("/wx", projects) is None
    assert sessions.project_for(None, projects) is None


def test_resume_command_reopens_on_the_model_and_effort_it_last_ran_on():
    assert (chat.resume_command("ops", "ID", None, remote_control=False, model="claude-sonnet-5",
                                effort="high")
            == "claude --resume ID -n ops --model claude-sonnet-5 --effort high")
    assert (chat.resume_command("ops", "ID", None, remote_control=False, settings="/s.json",
                                model="claude-sonnet-5", effort="high")
            == "claude --settings /s.json --resume ID -n ops")
