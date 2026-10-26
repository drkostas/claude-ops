"""Telling chats apart in a shared iTerm2 window by tab color."""
import json

import pytest

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


def test_one_listing_answers_for_many_terminals(monkeypatch):
    asked = []
    monkeypatch.setattr(iterm, "_osa", lambda script, timeout=5, **k: asked.append(timeout)
                        or "5\t/dev/ttys001\n6\t/dev/ttys002\n")
    raw = iterm.tty_listing()
    got = [iterm.window_of_ttys({t}, raw) for t in ("/dev/ttys001", "/dev/ttys002", "/dev/ttys009")]
    assert got == [5, 6, None] and iterm.windows_of_ttys({"/dev/ttys002"}, raw) == {6}
    assert asked == [8]                                             # one question for all of them
    monkeypatch.setattr(iterm, "_osa", lambda *a, **k: None)
    assert iterm.tty_listing() is None                              # iTerm2 did not answer


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


def test_a_terminal_whose_foreground_is_only_its_shell_names_the_shell():
    assert iterm.shell_pid(" 1000 Ss   /usr/bin/login\n 1093 S+   -zsh\n") == 1093
    assert iterm.shell_pid(" 7 S+ /bin/bash\n") == 7
    assert iterm.shell_pid(" 7280 Ss+  /opt/homebrew/bin/tmux\n") is None, "a program runs there"
    assert iterm.shell_pid(" 10 Ss -zsh\n 11 S+ vim\n") is None, "the shell is not in front"
    assert iterm.shell_pid(" 10 S+ -zsh\n 11 S+ less\n") is None, "a pipeline is not an idle shell"
    assert iterm.shell_pid("") is None and iterm.shell_pid(" 1000 Ss /usr/bin/login\n") is None


class Terminals:
    """ps and lsof answers for a few terminals: tty -> (ps text, the shell's folder)."""

    def __init__(self, ttys):
        self.ttys = ttys

    def run(self, cmd, **_k):
        class R:
            stdout, returncode = "", 0
        r = R()
        if cmd[0] == "ps":
            r.stdout = self.ttys.get("/dev/" + cmd[2], ("", None))[0]
        elif cmd[0].endswith("lsof"):
            pid = cmd[cmd.index("-p") + 1]
            for ps, cwd in self.ttys.values():
                if f" {pid} " in f" {ps} " and cwd:
                    r.stdout = f"p{pid}\nfcwd\nn{cwd}\n"
        return r


def test_the_windows_holding_an_idle_shell_in_a_folder_are_found(tmp_path):
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    near = tmp_path / "proj-two"
    near.mkdir()
    raw = "\n".join(f"{w}\t{t}" for w, t in [(1, "/dev/ttys001"), (1, "/dev/ttys002"), (2, "/dev/ttys003"),
                                            (3, "/dev/ttys004"), (4, "/dev/ttys005"), (5, "/dev/ttys006")])
    fake = Terminals({
        "/dev/ttys001": (" 11 Ss+ /opt/homebrew/bin/tmux\n", None),          # a tmux client: not a shell
        "/dev/ttys002": (" 21 Ss /usr/bin/login\n 22 S+ -zsh\n", str(proj)),  # an idle shell in the folder
        "/dev/ttys003": (" 32 S+ -zsh\n", str(proj / "sub")),                 # one in a folder inside it
        "/dev/ttys004": (" 42 S+ -zsh\n", str(other)),                        # another folder
        "/dev/ttys005": (" 52 S+ -zsh\n", str(near)),                         # a name that only starts the same
        "/dev/ttys006": (" 61 Ss -zsh\n 62 S+ vim\n", str(proj)),             # the folder, but busy
    })
    assert iterm.shell_windows(str(proj), raw, run=fake.run) == {1: ["/dev/ttys002"], 2: ["/dev/ttys003"]}
    assert iterm.shell_windows(str(other), raw, run=fake.run) == {3: ["/dev/ttys004"]}
    assert iterm.shell_windows(str(tmp_path / "nowhere"), raw, run=fake.run) == {}


def test_an_unreadable_iterm_is_none_never_no_windows(monkeypatch):
    monkeypatch.setattr(iterm, "tty_listing", lambda env=None: None)
    assert iterm.shell_windows("/tmp") is None


def test_every_idle_shell_is_listed_with_its_folder(tmp_path):
    raw = "1\t/dev/ttys001\n1\t/dev/ttys002\n2\t/dev/ttys003\n"
    fake = Terminals({
        "/dev/ttys001": (" 11 Ss+ /opt/homebrew/bin/tmux\n", None),
        "/dev/ttys002": (" 21 Ss /usr/bin/login\n 22 S+ -zsh\n", "/projects/a"),
        "/dev/ttys003": (" 32 S+ -bash\n", "/projects/b"),
    })
    assert iterm.idle_shells(raw, run=fake.run) == [(1, "/dev/ttys002", "/projects/a"), (2, "/dev/ttys003", "/projects/b")]


def test_a_key_combination_is_named_as_iterm_names_it():
    assert iterm.key_string("cmd+f") == "0x66-0x100000-0x3"
    assert iterm.key_string("ctrl+shift+a") == "0x61-0x60000-0x0"
    for bad in ("", "cmd+", "cmd+f1", "hyper+f", "cmd+cmd+f", "cmd+left"):
        with pytest.raises(ValueError):
            iterm.key_string(bad)


def test_bytes_to_send_are_written_as_iterm_reads_them():
    got = iterm.hex_bindings({"cmd+f": [0x0f, 0x2f]})
    assert got == {"0x66-0x100000-0x3": {"Action": 11, "Text": "0x0f 0x2f", "Version": 1, "Label": ""}}, \
        "spaces between the codes: iTerm2 sends only the first of codes split by newlines"
    with pytest.raises(ValueError):
        iterm.hex_bindings({"cmd+f": [0x100]})
    with pytest.raises(ValueError):
        iterm.hex_bindings({"cmd+f": []})


def test_only_the_chat_tabs_get_the_keys_once_and_keep_their_own(monkeypatch):
    import asyncio
    import sys
    import types

    class Session:
        def __init__(self, tty, keys):
            self.tty, self.keys, self.set = tty, keys, []

        async def async_get_variable(self, name):
            return self.tty

        async def async_get_profile(self):
            return types.SimpleNamespace(key_mappings=self.keys)

        async def async_set_profile_properties(self, lp):
            self.set.append(lp.keys)
            self.keys = lp.keys

    class Local:
        def set_key_mappings(self, value):
            self.keys = value

    own = {"0xf703-0x300000-0x7c": {"Action": 11, "Text": "0x05", "Version": 1}}
    chat, shell, done = Session("/dev/ttys1", dict(own)), Session("/dev/ttys2", {}), Session("/dev/ttys3", {})
    done.keys = dict(iterm.hex_bindings({"cmd+f": [0x0f, 0x2f]}))
    app = types.SimpleNamespace(terminal_windows=[types.SimpleNamespace(tabs=[
        types.SimpleNamespace(sessions=[chat]), types.SimpleNamespace(sessions=[shell]),
        types.SimpleNamespace(sessions=[done])])])
    fake = types.SimpleNamespace(LocalWriteOnlyProfile=Local,
                                 run_until_complete=lambda main, retry=False: asyncio.run(main(None)))

    async def get_app(connection):
        return app

    fake.async_get_app = get_app
    monkeypatch.setitem(sys.modules, "iterm2", fake)
    monkeypatch.setattr(iterm, "tmux_by_tty", lambda **k: {"/dev/ttys1": "claude-a", "/dev/ttys3": "claude-b"})
    assert iterm.bind_chat_keys({"cmd+f": [0x0f, 0x2f]}, apply=False) == ["claude-a"] and not chat.set, \
        "asked without apply, it names the tab that lacks the keys and changes nothing"
    assert iterm.bind_chat_keys({"cmd+f": [0x0f, 0x2f]}) == ["claude-a"]
    assert chat.keys["0x66-0x100000-0x3"]["Text"] == "0x0f 0x2f" and chat.keys["0xf703-0x300000-0x7c"] == own["0xf703-0x300000-0x7c"], \
        "the tab keeps its own keys"
    assert not shell.set and not done.set, "a tab that is not a chat's, or has the keys, is left alone"
    assert iterm.bind_chat_keys({"cmd+f": [0x0f, 0x2f]}) == [], "the second pass changes nothing"


def test_selecting_a_terminals_tab_answers_its_window(monkeypatch):
    asked = []
    monkeypatch.setattr(iterm, "_osa", lambda script, timeout=5, env=None: asked.append(script) or "10275\n")
    assert iterm.select_tty("/dev/ttys041") == 10275 and '"/dev/ttys041"' in asked[0] and "select t" in asked[0]
    assert iterm.select_tty('/dev/ttys041" then quit') is None and len(asked) == 1, "a name that is not a terminal asks nothing"
    monkeypatch.setattr(iterm, "_osa", lambda *a, **k: "")
    assert iterm.select_tty("/dev/ttys099") is None
