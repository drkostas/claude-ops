import json

from claude_ops import inject

ITERM = {"S1": {"name": "alpha-login (node)"}, "S2": {"name": "zsh"}, "S3": {"name": "beta (claude)"}}
EMPTY = lambda transport, session: "───\n❯ \n───\n  ⏵⏵ accept edits on"  # noqa: E731


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
    r = inject.inject("alpha", "hello", log=log, sessions=lambda: ITERM, screen=EMPTY,
                      send=lambda s, t, e: sent.append((s, t, e)) or (True, ""))
    assert r["result"] == "sent" and sent == [("S1", "hello", True)]
    a, b = lines(log)
    assert a["event"] == "attempt" and b["event"] == "delivered" and a["correlation"] == b["correlation"]
    assert "text" not in a and len(a["sha256"]) == 64


def test_keep_text_stores_the_text(tmp_path):
    log = tmp_path / "inject.jsonl"
    inject.inject("S1", "hi", log=log, keep_text=True, sessions=lambda: ITERM, screen=EMPTY,
                  send=lambda *a: (True, ""))
    assert lines(log)[0]["text"] == "hi"


def test_refuses_a_plain_shell_and_sends_nothing(tmp_path):
    log = tmp_path / "inject.jsonl"
    r = inject.inject("S2", "rm -rf x", log=log, sessions=lambda: ITERM, send=lambda *a: 1 / 0)
    assert r["result"] == "not-claude" and not log.exists()
    r = inject.inject("S2", "ok", log=log, force=True, sessions=lambda: ITERM, send=lambda *a: (True, ""))
    assert r["result"] == "sent"


def test_waits_for_an_empty_input_line(tmp_path):
    log = tmp_path / "inject.jsonl"
    typed = lambda t, s: "───\n❯ half a sentence\n───"  # noqa: E731
    r = inject.inject("S1", "x", log=log, sessions=lambda: ITERM, screen=typed, send=lambda *a: 1 / 0)
    assert r["result"] == "not-ready" and "typed" in r["why"] and not log.exists()
    asking = lambda t, s: "Allow this edit?\nEnter to confirm · Esc to cancel"  # noqa: E731
    assert inject.inject("S1", "x", log=log, sessions=lambda: ITERM, screen=asking, send=lambda *a: 1 / 0)["result"] == "not-ready"
    assert inject.inject("S1", "x", log=log, sessions=lambda: ITERM, screen=lambda t, s: None,
                         send=lambda *a: 1 / 0)["result"] == "not-ready"   # an unread screen is not empty
    r = inject.inject("S1", "x", log=log, force=True, sessions=lambda: ITERM, screen=typed, send=lambda *a: (True, ""))
    assert r["result"] == "sent"


def test_no_session_and_failed(tmp_path):
    log = tmp_path / "inject.jsonl"
    r = inject.inject("nothing", "x", log=log, sessions=lambda: ITERM, send=lambda *a: 1 / 0)
    assert r["result"] == "no-session" and "zsh" in r["open"]
    r = inject.inject("S1", "x", log=log, sessions=lambda: ITERM, screen=EMPTY, send=lambda *a: (False, "timeout"))
    assert r["result"] == "failed" and lines(log)[-1]["event"] == "failed"


def test_tmux_lists_survive_a_process_with_no_locale(monkeypatch):
    """A launchd agent has no locale, and tmux then prints the tab in a -F format as `_`, so a
    session list came back as `claude-x_2.1.296_1` and no session could be found by name.
    Every tmux call passes -u, which keeps the tab whatever the locale."""
    import subprocess
    seen = []

    class R:
        returncode, stdout, stderr = 0, "claude-x\t2.1.296\t1\n", ""

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append(argv) or R())
    from claude_ops import inject
    assert inject.tmux_sessions(env={}) == {"claude-x": {"name": "claude-x", "command": "2.1.296",
                                                         "attached": True}}
    assert seen and seen[0][:2] == [inject.TMUX, "-u"]


def test_applescript_loops_hold_ids_not_positions():
    """A loop over the bare windows, tabs or sessions collection re-reads a position on every use,
    so the session it checked and the session it writes to can differ. Every loop in the package
    takes (get ...) first, which is a fixed list of id references."""
    import re
    from pathlib import Path

    bare = re.compile(r"repeat with \w+ in (?!\(get )(?:every )?(?:windows|tabs|sessions)\b")
    root = Path(inject.__file__).parent
    found = [f"{p.name}:{n}" for p in root.rglob("*.py")
             for n, line in enumerate(p.read_text().splitlines(), 1) if bare.search(line)]
    assert found == []
    assert bare.search("repeat with w in windows") and not bare.search("repeat with w in (get windows)")
    assert "(get sessions of t)" in inject.ITERM_SEND


def test_a_closed_window_iterm2_still_lists_is_skipped():
    # iTerm2 keeps a window it closed in its scripting model for a while, and reading its tabs
    # fails ("Can't make missing value into type specifier"). Without a guard around each window,
    # one such window made every listing fail, so no session was found anywhere.
    from claude_ops import update
    for name, script in (("ITERM_LIST", inject.ITERM_LIST), ("ITERM_SEND", inject.ITERM_SEND),
                         ("ITERM_SCREEN", update.ITERM_SCREEN)):
        body = script.split("repeat with w in (get windows)", 1)[1]
        assert body.lstrip().split("\n")[0].strip().startswith(("try", "set found to false")), name
        assert "end try" in body, name
    # and the send raises an error that happens after the session was found, never "no-session"
    assert "if found then error e" in inject.ITERM_SEND
