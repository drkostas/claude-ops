"""An env given to a spawning function reaches every process it starts, and None inherits."""
import subprocess

from claude_ops import chat, inject, sessions

ENV = {"PATH": "/usr/bin:/bin"}


def _capture(monkeypatch, stdout=""):
    seen = []

    def fake(cmd, **k):
        seen.append(k.get("env"))
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake)
    return seen


def test_tmux_listing_and_send_carry_the_env(monkeypatch):
    seen = _capture(monkeypatch, "claude-x\tclaude\t0\n")
    inject.tmux_sessions(env=ENV)
    inject.send_tmux("claude-x", "hi", True, env=ENV)
    assert seen and all(e is ENV for e in seen)


def test_inject_passes_the_env_to_the_default_sender(monkeypatch):
    seen = _capture(monkeypatch, "claude-x\tclaude\t0\n")
    r = inject.inject("claude-x", "hi", transport="tmux", env=ENV, log=__import__("pathlib").Path("/dev/null"))
    assert r["result"] == "sent"
    assert seen and all(e is ENV for e in seen)


def test_chat_end_and_live_folders_carry_the_env(monkeypatch):
    seen = _capture(monkeypatch, "claude-x\tclaude\t0\n")
    chat.end("x", env=ENV)
    sessions.live_folders(env=ENV)
    assert len(seen) >= 4 and all(e is ENV for e in seen)


def test_no_env_inherits(monkeypatch):
    seen = _capture(monkeypatch)
    inject.tmux_sessions()
    assert seen == [None]
