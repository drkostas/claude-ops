"""Telling chats apart in a shared iTerm2 window by tab color."""
from claude_ops import iterm

RED, YELLOW, PURPLE, ORANGE = iterm.PALETTE[:4]


def test_a_window_with_one_chat_is_left_alone():
    assert iterm.assign([[("claude-a", None)]]) == {}


def test_colors_the_user_set_are_kept_when_they_differ():
    assert iterm.assign([[("claude-a", RED), ("claude-b", YELLOW)]]) == {}


def test_uncolored_tabs_take_the_first_free_colors():
    assert iterm.assign([[("claude-a", None), ("claude-b", None)]]) == {"claude-a": RED, "claude-b": YELLOW}


def test_a_repeated_color_is_replaced_in_the_later_tab():
    assert iterm.assign([[("claude-a", RED), ("claude-b", RED)]]) == {"claude-b": YELLOW}


def test_a_chat_takes_back_its_old_color_when_it_is_free():
    assert iterm.assign([[("claude-a", RED), ("claude-b", None)]], {"claude-b": PURPLE}) == {"claude-b": PURPLE}
    assert iterm.assign([[("claude-a", RED), ("claude-b", None)]], {"claude-b": RED}) == {"claude-b": YELLOW}


def test_float_colors_compare_rounded_and_windows_are_separate():
    assert iterm.assign([[("claude-a", (234.4, 116.1, 103.6)), ("claude-b", RED)]]) == {"claude-b": YELLOW}
    two = iterm.assign([[("claude-a", None), ("claude-b", None)], [("claude-c", None), ("claude-d", None)]])
    assert two == {"claude-a": RED, "claude-b": YELLOW, "claude-c": RED, "claude-d": YELLOW}


def test_more_chats_than_colors_leaves_the_rest():
    tabs = [(f"claude-{i}", None) for i in range(3)]
    assert iterm.assign([tabs], palette=(RED, YELLOW)) == {"claude-0": RED, "claude-1": YELLOW}


def test_tmux_clients_are_read_by_terminal(monkeypatch):
    class R:
        stdout = "/dev/ttys001 claude-a\n/dev/ttys002 other\n"
    monkeypatch.setattr(iterm.subprocess, "run", lambda *a, **k: R())
    assert iterm.tmux_by_tty() == {"/dev/ttys001": "claude-a", "/dev/ttys002": "other"}


# ----------------------------------------------------------------------------- windows

def test_windows_are_parsed_with_their_frames():
    raw = "5\t2\t58,33,1728,1117\tbuild\nbad line\n6\t0\t0,0,10,10\t[tmux]\n"
    got = iterm.parse_windows(raw)
    assert got[0] == {"id": 5, "tabs": 2, "frame": {"x": 58, "y": 33, "w": 1670, "h": 1084}, "title": "build"}
    assert [w["id"] for w in got] == [5, 6] and iterm.parse_windows(None) is None


def test_dead_windows_are_the_unreadable_ones_and_unknown_is_none(monkeypatch):
    assert iterm.dead_windows("12\n34\n") == {12, 34}
    monkeypatch.setattr(iterm, "_osa", lambda *a, **k: None)
    assert iterm.dead_windows() is None


def test_a_session_or_terminal_names_one_window_or_none():
    raw = "5\t/dev/ttys001\n5\t/dev/ttys002\n6\t/dev/ttys003\nx\t/dev/ttys009\n"
    assert iterm.parse_ttys(raw) == [(5, "/dev/ttys001"), (5, "/dev/ttys002"), (6, "/dev/ttys003")]
    assert iterm.window_of_ttys({"/dev/ttys002"}, raw) == 5
    assert iterm.window_of_ttys({"/dev/ttys001", "/dev/ttys003"}, raw) is None   # two windows: no guess
    assert iterm.window_of_ttys(set(), raw) is None


def test_every_window_of_some_terminals_and_unreadable_is_none(monkeypatch):
    raw = "5\t/dev/ttys001\n5\t/dev/ttys002\n6\t/dev/ttys003\n"
    assert iterm.windows_of_ttys({"/dev/ttys001", "/dev/ttys003"}, raw) == {5, 6}   # both, not none
    assert iterm.windows_of_ttys({"/dev/ttys002"}, raw) == {5}
    assert iterm.windows_of_ttys({"/dev/ttys009"}, raw) == set()   # read, and shown nowhere
    assert iterm.windows_of_ttys({"/dev/ttys009"}, "") == set()    # no windows at all
    assert iterm.windows_of_ttys(set(), raw) == set()
    monkeypatch.setattr(iterm, "_osa", lambda *a, **k: None)
    assert iterm.windows_of_ttys({"/dev/ttys001"}) is None         # iTerm2 did not answer


def test_session_uuid_and_unsafe_ids_are_refused(monkeypatch):
    assert iterm.session_uuid("w0t1p0:ABC-1") == "ABC-1" and iterm.session_uuid("nope") is None
    monkeypatch.setattr(iterm, "_osa", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert iterm.window_of_session('w0t0p0:x" & quit') is None       # never put into a script
    assert iterm.session_of_tmux_pane("%x") is None


def test_an_empty_window_is_closed_and_one_with_a_session_kept(monkeypatch):
    answers = iter(["0", "", None])
    monkeypatch.setattr(iterm, "_osa", lambda *a, **k: next(answers))
    assert iterm.close_empty_window(9, wait=2) == "closed"
    monkeypatch.setattr(iterm, "_osa", lambda *a, **k: "1")
    assert iterm.close_empty_window(9) == "kept"
    monkeypatch.setattr(iterm, "_osa", lambda *a, **k: None)
    assert iterm.close_empty_window(9) == "not found"


def test_set_bounds_returns_the_frame_read_back(monkeypatch):
    target = {"x": 58, "y": 33, "w": 1670, "h": 1084}
    monkeypatch.setattr(iterm, "_osa", lambda *a, **k: "")
    monkeypatch.setattr(iterm, "windows", lambda env=None: [{"id": 5, "frame": dict(target)}])
    assert iterm.set_bounds(5, target) == target
    monkeypatch.setattr(iterm, "windows", lambda env=None: [])
    assert iterm.set_bounds(5, target) is None


class FakeProcs:
    def __init__(self, apps=(100,), servers=(877,), held=21, own=0, dies=True, opens=True, answers_after=1):
        self.apps, self.servers, self.held, self.own = list(apps), list(servers), held, own
        self.dies, self.opens, self.answers_after = dies, opens, answers_after
        self.killed, self.opened, self.asked = [], 0, 0

    def app(self):
        return self.apps

    def server(self):
        return self.servers

    def children(self, pid):
        return self.held if pid in self.servers else self.own

    def kill(self, pid):
        self.killed.append(pid)

    def alive(self, pid):
        return not self.dies

    def open(self):
        self.opened += 1
        return self.opens

    def answers(self):
        self.asked += 1
        return self.asked >= self.answers_after

    def sleep(self, s):
        pass


def test_restart_stops_only_the_app_and_waits_for_it_to_answer():
    p = FakeProcs(answers_after=3)
    outcome, detail = iterm.restart_app(procs=p)
    assert outcome == "success" and p.killed == [100] and p.opened == 1 and p.asked == 3
    assert detail["terminals_before"] == 21 and detail["terminals_after"] == 21


def test_restart_refuses_when_the_terminals_would_end():
    for p in (FakeProcs(servers=()), FakeProcs(held=0), FakeProcs(own=2), FakeProcs(apps=(1, 2))):
        outcome, detail = iterm.restart_app(procs=p)
        assert outcome == "failure" and p.killed == [] and "nothing was stopped" in detail["message"]


def test_restart_that_never_answers_is_a_failure():
    p = FakeProcs(answers_after=10**6)
    outcome, detail = iterm.restart_app(procs=p, answer_wait=9)
    assert outcome == "failure" and "did not answer" in detail["message"]
    p = FakeProcs(dies=False)
    assert iterm.restart_app(procs=p)[0] == "failure" and p.opened == 0


def test_an_api_that_does_not_answer_reads_none_and_does_not_end_the_program(monkeypatch):
    import sys
    import types

    def refuse(main, retry=False):
        raise SystemExit(1)   # what the iterm2 package does when it cannot connect
    fake = types.SimpleNamespace(run_until_complete=refuse)
    monkeypatch.setitem(sys.modules, "iterm2", fake)
    monkeypatch.setattr(iterm, "tmux_by_tty", lambda **k: {})
    assert iterm.chat_tabs() is None
    assert iterm.set_tab_color("w0t0p0:ABC", (1.0, 0.0, 0.0)) is False
