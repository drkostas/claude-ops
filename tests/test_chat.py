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
    monkeypatch.setattr(chat, "stable_pane", lambda name: TRUST)  # the Down key had no effect
    monkeypatch.setattr(chat.subprocess, "run", lambda cmd, **k: sent.append(cmd[-1]))
    assert chat.answer_trust("claude-x") is False
    assert sent == ["Down"]
