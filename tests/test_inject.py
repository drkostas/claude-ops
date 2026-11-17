import json
import subprocess

import pytest

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
        first = next(ln.strip() for ln in body.split("\n")
                     if ln.strip() and not ln.strip().startswith("--") and ln.strip() != "set found to false")
        assert first == "try", name
        assert "end try" in body, name
    # and the send raises an error that happens after the session was found, never "no-session"
    assert "if found then error e" in inject.ITERM_SEND


def test_a_long_message_reaches_tmux(tmp_path):
    """tmux refuses a command argument above about 16 KB, so the text goes to the buffer through
    stdin. A 20 KB message pasted into a session running `cat` arrives whole."""
    import os
    import shutil
    import tempfile
    import time
    if not shutil.which("tmux"):
        pytest.skip("no tmux")
    # a private server. ⛔ Without $TMUX: inside a tmux pane tmux follows $TMUX before TMUX_TMPDIR,
    # so the kill-server below ended the real server and every session in it (twice, the first
    # time this test ran)
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    # and a short folder: a socket path longer than 104 bytes fails on macOS ("File name too long")
    short = tempfile.mkdtemp(prefix="co-", dir="/tmp")
    env["TMUX_TMPDIR"] = short
    out = tmp_path / "out.txt"
    subprocess.run(["tmux", "-u", "new-session", "-d", "-s", "claude-x", f"cat > {out}"], check=True, env=env)
    try:
        text = ("x" * 99 + "\n") * 200   # 20,000 bytes
        ok, why = inject.send_tmux("claude-x", text, False, env=env)
        assert ok, why
        for _ in range(50):
            if out.exists() and out.stat().st_size >= len(text):
                break
            time.sleep(0.1)
        assert out.read_text() == text
    finally:
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
        shutil.rmtree(short, ignore_errors=True)


def test_press_sends_one_escape_to_a_tmux_session(tmp_path):
    """The program in the pane reads one escape byte and nothing else: no Enter, no text."""
    import os
    import shutil
    import tempfile
    import time
    if not shutil.which("tmux"):
        pytest.skip("no tmux")
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}   # a private server, as above
    short = tempfile.mkdtemp(prefix="co-", dir="/tmp")
    env["TMUX_TMPDIR"] = short
    out = tmp_path / "key.bin"
    subprocess.run(["tmux", "-u", "new-session", "-d", "-s", "claude-k",
                    f"stty raw -echo; head -c 3 > {out}"], check=True, env=env)
    try:
        # the key must arrive after stty, or the terminal keeps it waiting for a whole line
        for _ in range(50):
            now = subprocess.run(["tmux", "display-message", "-p", "-t", "claude-k", "#{pane_current_command}"],
                                 capture_output=True, text=True, env=env).stdout.strip()
            if now == "head":
                break
            time.sleep(0.1)
        ok, why = inject.press("claude-k", "Escape", env=env)
        assert ok, why
        ok, why = inject.press("claude-k", "Enter", env=env)
        assert ok, why
        subprocess.run(["tmux", "send-keys", "-t", "claude-k", "-l", "z"], env=env)   # marks the end
        for _ in range(50):
            if out.exists() and out.stat().st_size >= 3:
                break
            time.sleep(0.1)
        assert out.read_bytes() == b"\x1b\rz"
    finally:
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
        shutil.rmtree(short, ignore_errors=True)


def test_press_paste_end_sends_the_end_marker_alone(tmp_path):
    """PasteEnd types the bracketed-paste end marker and nothing else: no Enter, no text."""
    import os
    import shutil
    import tempfile
    import time
    if not shutil.which("tmux"):
        pytest.skip("no tmux")
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}   # a private server, as above
    short = tempfile.mkdtemp(prefix="co-", dir="/tmp")
    env["TMUX_TMPDIR"] = short
    out = tmp_path / "key.bin"
    subprocess.run(["tmux", "-u", "new-session", "-d", "-s", "claude-k",
                    f"stty raw -echo; head -c 7 > {out}"], check=True, env=env)
    try:
        for _ in range(50):
            now = subprocess.run(["tmux", "display-message", "-p", "-t", "claude-k", "#{pane_current_command}"],
                                 capture_output=True, text=True, env=env).stdout.strip()
            if now == "head":
                break
            time.sleep(0.1)
        ok, why = inject.press("claude-k", "PasteEnd", env=env)
        assert ok, why
        subprocess.run(["tmux", "send-keys", "-t", "claude-k", "-l", "z"], env=env)   # marks the end
        for _ in range(50):
            if out.exists() and out.stat().st_size >= 7:
                break
            time.sleep(0.1)
        assert out.read_bytes() == b"\x1b[201~z"
    finally:
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
        shutil.rmtree(short, ignore_errors=True)


def test_press_sends_only_the_keys_it_names():
    with pytest.raises(ValueError):
        inject.press("claude-k", "C-c")
    with pytest.raises(ValueError):
        inject.press("S1", "Escape", transport="iterm")


def test_no_test_reaches_the_real_tmux_server():
    """conftest's guard: $TMUX is unset and TMUX_TMPDIR is private while the suite runs, so a
    `tmux kill-server` in a test can only end a server the test made."""
    import os
    import tempfile
    assert "TMUX" not in os.environ
    assert os.environ.get("TMUX_TMPDIR", "").startswith("/tmp/co-")


class _Ran:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout = returncode, stdout


def test_input_age_is_the_newest_key_of_any_client_of_the_session():
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return _Ran(0, "1000\n1090\n")

    assert inject.input_age("claude-x", now=1100.0, run=run) == 10.0
    assert seen[0][-4:] == ["-t", "claude-x", "-F", "#{client_activity}"]
    assert inject.input_age("claude-x", now=1050.0, run=run) == 0.0  # a clock behind tmux reads as just now


def test_input_age_is_unknown_without_an_attached_client_or_an_answer():
    assert inject.input_age("claude-x", now=1.0, run=lambda cmd, **kw: _Ran(0, "")) is None
    assert inject.input_age("claude-x", now=1.0, run=lambda cmd, **kw: _Ran(1, "")) is None
    assert inject.input_age("claude-x", now=1.0, run=lambda cmd, **kw: _Ran(0, "garbage\n")) is None


# ------------------------------------------------------------------ one sender at a time per chat

_HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(float(sys.argv[2]))
"""


def _hold_elsewhere(target, seconds):
    """Another process holding `target`'s input lock for `seconds`; returns once it holds it."""
    import sys
    path = inject._lock_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path), str(seconds)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_input_lock_waits_for_another_process_then_gives_up():
    other = _hold_elsewhere("claude-a", 5)
    try:
        with inject.input_lock("claude-a", wait=0.2) as held:
            assert held is False
        with inject.input_lock("claude-b", wait=0.2) as held:   # another chat is not held
            assert held is True
    finally:
        other.kill()
        other.wait()
    with inject.input_lock("claude-a", wait=1) as held:   # the lock ends with the process that held it
        assert held is True


def test_input_lock_is_taken_again_inside_one_process():
    with inject.input_lock("claude-a", wait=0) as outer:
        with inject.input_lock("claude-a", wait=0) as inner:
            assert outer and inner
        # still held after the inner hold ends: another process cannot take it
        import sys
        r = subprocess.run([sys.executable, "-c", "import fcntl,os,sys;fd=os.open(sys.argv[1],os.O_RDWR);"
                            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)", str(inject._lock_path("claude-a"))],
                           capture_output=True)
        assert r.returncode != 0
    import sys
    r = subprocess.run([sys.executable, "-c", "import fcntl,os,sys;fd=os.open(sys.argv[1],os.O_RDWR);"
                        "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)", str(inject._lock_path("claude-a"))],
                       capture_output=True)
    assert r.returncode == 0


def test_a_paste_and_its_enter_are_never_split_by_another_sender(monkeypatch):
    """A sender queued behind the lock types only after the other's Enter. Without the lock a
    /compact was pasted between a message and its Enter, and arrived as words at its end."""
    import time as _t
    ran = []

    def fake_run(cmd, **kw):
        ran.append((_t.monotonic(), cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(inject.subprocess, "run", fake_run)
    monkeypatch.setattr(inject.time, "sleep", lambda s: None)
    other = _hold_elsewhere("claude-a", 0.6)
    started = _t.monotonic()
    ok, why = inject.send_tmux("claude-a", "/compact", True)
    other.wait()
    assert ok, why
    assert ran and ran[0][0] - started >= 0.4, "the paste waited for the other sender"


def test_a_send_that_cannot_get_the_line_types_nothing(monkeypatch):
    ran = []
    monkeypatch.setattr(inject.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setattr(inject, "LOCK_WAIT", 0.2)
    other = _hold_elsewhere("claude-a", 5)
    try:
        assert inject.send_tmux("claude-a", "hello", True) == (False, inject.HELD)
        assert inject.send_iterm("claude-a", "hello", True) == (False, inject.HELD)
    finally:
        other.kill()
        other.wait()
    assert ran == []


def test_inject_reads_the_screen_and_sends_under_one_hold(tmp_path, monkeypatch):
    """While another sender holds the line, inject neither reads the screen nor sends: it answers
    not-ready, so a watcher tries again."""
    monkeypatch.setattr(inject, "LOCK_WAIT", 0.2)
    read, sent = [], []
    other = _hold_elsewhere("S3", 5)
    try:
        r = inject.inject("S3", "hi", sessions=lambda: ITERM, log=tmp_path / "log.jsonl",
                          screen=lambda t, m: read.append(m) or EMPTY(t, m),
                          send=lambda m, t, e: sent.append(m) or (True, ""))
    finally:
        other.kill()
        other.wait()
    assert r["result"] == "not-ready" and r["why"] == inject.HELD
    assert read == [] and sent == []
    r = inject.inject("S3", "hi", sessions=lambda: ITERM, log=tmp_path / "log.jsonl",
                      screen=lambda t, m: read.append(m) or EMPTY(t, m),
                      send=lambda m, t, e: sent.append(m) or (True, ""))
    assert r["result"] == "sent" and read == ["S3"] and sent == ["S3"]


def test_another_thread_of_the_same_process_waits_for_the_line():
    """A hold nests only in the thread that took it. A second thread sending to the same chat waits
    until the first lets go, then gets the line, and gives up when the wait runs out first."""
    import threading
    import time as _t
    first_in, release = threading.Event(), threading.Event()
    got = {}

    def first():
        with inject.input_lock("claude-a", wait=1) as held:
            got["first"] = held
            first_in.set()
            release.wait(5)

    t1 = threading.Thread(target=first)
    t1.start()
    assert first_in.wait(5)
    with inject.input_lock("claude-a", wait=0.2) as held:
        assert held is False, "a second thread does not pass through the first thread's hold"

    def second():
        start = _t.monotonic()
        with inject.input_lock("claude-a", wait=5) as held:
            got["second"] = (held, _t.monotonic() - start)

    t2 = threading.Thread(target=second)
    t2.start()
    _t.sleep(0.4)
    assert "second" not in got, "the second thread is still waiting while the first holds the line"
    release.set()
    t1.join(5)
    t2.join(5)
    assert got["first"] is True
    held, waited = got["second"]
    assert held is True and waited >= 0.3
