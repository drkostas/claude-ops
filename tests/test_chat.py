from pathlib import Path

from claude_ops import chat

TRUST = """
 Do you trust the files in this folder?

 ❯ 1. No, exit
   2. Yes, I trust this folder
"""


def test_trust_answer_is_found_by_its_words():
    assert chat.trust_keys(TRUST) == ["Down", "Enter"]
    flipped = TRUST.replace("❯ 1. No, exit", "  1. No, exit").replace("  2. Yes, I trust", "❯ 2. Yes, I trust")
    assert chat.trust_keys(flipped) == ["Enter"]
    assert chat.trust_keys("some other screen") is None


def test_diagnose_names_the_prompt_and_the_fix():
    what, fix = chat.diagnose("Please Select login method", "claude-x")
    assert "not signed in" in what and "tmux attach -t claude-x" in fix
    assert chat.diagnose("> ready", "claude-x") is None


def test_project_dir_matches_claude_code_naming(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "PROJECTS", tmp_path)
    d = chat.project_dir(Path("/"))
    assert d == tmp_path / "-"
    assert chat.project_dir(tmp_path).name == str(tmp_path.resolve()).replace("/", "-")


def test_only_the_last_prompt_on_screen_counts():
    old_then_new = TRUST.replace("❯ 1. No, exit", "  1. No, exit").replace("  2. Yes, I trust", "❯ 2. Yes, I trust") + "\n$ \n" + TRUST
    assert chat.trust_keys(old_then_new) == ["Down", "Enter"]
    assert not chat.cursor_on_yes(TRUST)
    assert chat.cursor_on_yes(TRUST.replace("❯ 1. No, exit", "  1. No, exit").replace("  2. Yes, I trust", "❯ 2. Yes, I trust"))


def test_enter_is_not_pressed_unless_yes_is_selected(monkeypatch):
    sent = []
    monkeypatch.setattr(chat, "stable_pane", lambda name, **k: TRUST)  # the Down key had no effect
    monkeypatch.setattr(chat.subprocess, "run", lambda cmd, **k: sent.append(cmd[-1]))
    assert chat.answer_trust("claude-x") is False
    assert sent == ["Down"]


def test_in_tmux_walks_up_to_a_tmux_server():
    procs = {1: {"ppid": 0, "comm": "launchd"}, 50: {"ppid": 1, "comm": "/opt/homebrew/bin/tmux"},
             51: {"ppid": 50, "comm": "-zsh"}, 52: {"ppid": 51, "comm": "claude"},
             60: {"ppid": 1, "comm": "iTerm2"}, 61: {"ppid": 60, "comm": "-zsh"}, 62: {"ppid": 61, "comm": "claude"}}
    assert chat.in_tmux(52, procs) is True
    assert chat.in_tmux(62, procs) is False
    assert chat.in_tmux(99, procs) is None


def test_attach_line_keeps_the_tab_and_its_name():
    line = chat.attach_line("/work/my dir", "claude-x", "claude --resume U")
    assert line.startswith("cd '/work/my dir' && ")
    assert "-e ITERM_SESSION_ID=" in line and "send-keys -t claude-x -l 'claude --resume U'" in line
    for opt in ("status off", "mouse on", "set-titles on", "'#{pane_title}'", "focus-events on"):
        assert opt in line, opt
    assert line.endswith("attach -t claude-x") and "-CC" not in line
    cc = chat.attach_line("/w", "claude-x", "claude", cc=True)
    assert cc.endswith("-CC attach -t claude-x") and "set-titles" not in cc


def test_server_context_is_none_without_a_server(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(chat.subprocess, "run", lambda *a, **k: R())
    assert chat.server_context() == "none"
