import json

from claude_ops import inject

ITERM = {"S1": {"name": "alpha-login (node)"}, "S2": {"name": "zsh"}, "S3": {"name": "beta (claude)"}}


def lines(log):
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_looks_like_claude_fails_closed():
    assert inject.looks_like_claude("iterm", {"name": "my-chat (node)"})
    assert inject.looks_like_claude("iterm", {"name": "x (-claude)"})
    assert not inject.looks_like_claude("iterm", {"name": "zsh"})
    assert not inject.looks_like_claude("iterm", {"name": "build (python3)"})
    assert not inject.looks_like_claude("iterm", {})
    assert inject.looks_like_claude("tmux", {"command": "node"})
    assert not inject.looks_like_claude("tmux", {"command": "zsh"})
    assert inject.looks_like_claude("tmux", {"command": "2.1.286"})  # what tmux really reports
    assert not inject.looks_like_claude("tmux", {"command": "2.1"})


def test_resolve_by_id_or_name_part():
    assert inject.resolve("S2", ITERM, "iterm") == "S2"
    assert inject.resolve("beta", ITERM, "iterm") == "S3"
    assert inject.resolve("gamma", ITERM, "iterm") == ""
    assert inject.resolve("claude-x", {"claude-x": {}}, "tmux") == "claude-x"
    assert inject.resolve("claude", {"claude-x": {}}, "tmux") == ""  # tmux names must match exactly


def test_multiline_text_is_one_bracketed_paste():
    assert inject.bracketed("one line") == "one line"
    assert inject.bracketed("a\nb") == "\x1b[200~a\nb\x1b[201~"


def test_sent_is_logged_before_and_after(tmp_path):
    log = tmp_path / "inject.jsonl"
    sent = []
    r = inject.inject("alpha", "hello", log=log, sessions=lambda: ITERM,
                      send=lambda s, t, e: sent.append((s, t, e)) or (True, ""))
    assert r["result"] == "sent" and sent == [("S1", "hello", True)]
    a, b = lines(log)
    assert a["event"] == "attempt" and b["event"] == "delivered" and a["correlation"] == b["correlation"]
    assert "text" not in a and len(a["sha256"]) == 64


def test_keep_text_stores_the_text(tmp_path):
    log = tmp_path / "inject.jsonl"
    inject.inject("S1", "hi", log=log, keep_text=True, sessions=lambda: ITERM, send=lambda *a: (True, ""))
    assert lines(log)[0]["text"] == "hi"


def test_refuses_a_plain_shell_and_sends_nothing(tmp_path):
    log = tmp_path / "inject.jsonl"
    r = inject.inject("S2", "rm -rf x", log=log, sessions=lambda: ITERM, send=lambda *a: 1 / 0)
    assert r["result"] == "not-claude" and not log.exists()
    r = inject.inject("S2", "ok", log=log, force=True, sessions=lambda: ITERM, send=lambda *a: (True, ""))
    assert r["result"] == "sent"


def test_no_session_and_failed(tmp_path):
    log = tmp_path / "inject.jsonl"
    r = inject.inject("nothing", "x", log=log, sessions=lambda: ITERM, send=lambda *a: 1 / 0)
    assert r["result"] == "no-session" and "zsh" in r["open"]
    r = inject.inject("S1", "x", log=log, sessions=lambda: ITERM, send=lambda *a: (False, "timeout"))
    assert r["result"] == "failed" and lines(log)[-1]["event"] == "failed"


def test_tmux_lists_survive_a_process_with_no_locale(monkeypatch):
    """A launchd agent has no locale, and tmux then prints the tab in a -F format as `_`, so a
    session list came back as `claude-x_2.1.296_1` and no session could be found by name
    (drlab, 2026-10-10). Every tmux call passes -u, which keeps the tab whatever the locale."""
    import subprocess
    seen = []

    class R:
        returncode, stdout, stderr = 0, "claude-x\t2.1.296\t1\n", ""

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append(argv) or R())
    from claude_ops import inject
    assert inject.tmux_sessions(env={}) == {"claude-x": {"name": "claude-x", "command": "2.1.296",
                                                         "attached": True}}
    assert seen and seen[0][:2] == [inject.TMUX, "-u"]
