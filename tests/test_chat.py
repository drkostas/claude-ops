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


# the trust question as Claude Code draws it, and a chat at its prompt whose last answer quotes it
TRUST_SCREEN = """
 /home/user/projects/app

 Quick safety check: Is this a project you created or one you trust? (Like your
 own code, a well-known open source project, or work from your team).

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""

AT_PROMPT_QUOTING_IT = """
⏺ The resume left the chat on the question "Yes, I trust this folder", so I answered it.

✻ Worked for 1m 16s

───────────────────────────────────────────────────────────────────── build ─
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""


def test_a_start_up_question_counts_only_at_the_bottom():
    what, _fix = chat.diagnose(TRUST_SCREEN, "claude-x")
    assert "trust this folder" in what
    assert not chat.input_box(TRUST_SCREEN)
    # the same words in the conversation above the input box are not a question waiting
    assert chat.input_box(AT_PROMPT_QUOTING_IT)
    assert chat.diagnose(AT_PROMPT_QUOTING_IT, "claude-x") is None
    # and words far above the bottom are not either
    assert chat.diagnose("Please Select login method\n" + "line\n" * 20, "claude-x") is None


class FakeTmux:
    """tmux and osascript, run in turn; the server exists once iTerm2 has run its command."""
    def __init__(self, server=False, iterm_runs=True):
        self.server, self.iterm_runs, self.calls = server, iterm_runs, []

    def __call__(self, cmd, **_k):
        import subprocess as sp
        self.calls.append(list(cmd))
        if cmd[0].endswith("osascript"):
            self.server = self.iterm_runs
            return sp.CompletedProcess(cmd, 0 if self.iterm_runs else 1, "", "")
        if "ls" in cmd or "has-session" in cmd:
            return sp.CompletedProcess(cmd, 0 if self.server else 1, "", "")
        return sp.CompletedProcess(cmd, 0, "", "")


def test_a_new_tmux_server_is_started_through_iterm2():
    run = FakeTmux()
    assert not chat.server_running(run=run)
    ok, how = chat.start_server_through_iterm("claude-x", Path("/tmp/work"), run=run, sleep=lambda s: None)
    osa = [c for c in run.calls if c[0].endswith("osascript")]
    assert ok and "iTerm2" in how and len(osa) == 1
    # the command reaches iTerm2 as an argument, never inside the script, so no quoting can break it
    assert "new-session" in osa[0][-1] and "claude-x" in osa[0][-1] and "/tmp/work" in osa[0][-1]
    assert "claude-x" not in " ".join(osa[0][:-1])


def test_iterm2_that_does_not_run_it_is_a_failure_and_a_running_server_is_never_touched():
    run = FakeTmux(iterm_runs=False)
    ok, how = chat.start_server_through_iterm("claude-x", Path("/tmp/work"), run=run, wait=3,
                                              sleep=lambda s: None)
    assert not ok and how
    run = FakeTmux(server=True)
    ok, how = chat.start_server_through_iterm("claude-x", Path("/tmp/work"), run=run, sleep=lambda s: None)
    assert not ok and "runs" in how and not [c for c in run.calls if c[0].endswith("osascript")]


def test_at_prompt_is_not_fooled_by_a_conversation_about_the_question(monkeypatch):
    monkeypatch.setattr(chat, "pane_text", lambda *_a, **_k: AT_PROMPT_QUOTING_IT)
    assert chat.at_prompt("claude-x", wait=0.1)
    monkeypatch.setattr(chat, "pane_text", lambda *_a, **_k: TRUST_SCREEN)
    assert not chat.at_prompt("claude-x", wait=0.1)


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


def test_claude_pid_walks_up_to_the_claude_process():
    from claude_ops import chat as ch
    tree = {500: ("400", "/bin/zsh"), 400: ("300", "/usr/local/bin/claude"), 300: ("1", "tmux")}
    assert ch.claude_pid(500, parent=lambda p: tree.get(p)) == 400
    assert ch.claude_pid(300, parent=lambda p: tree.get(p)) is None        # reaches the top
    assert ch.claude_pid(999, parent=lambda p: tree.get(p)) is None        # cannot read
    loop = {10: ("11", "sh"), 11: ("10", "sh")}
    assert ch.claude_pid(10, parent=lambda p: loop.get(p), depth=4) is None
