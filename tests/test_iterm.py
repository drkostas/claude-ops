"""Telling chats apart in a shared iTerm2 window by tab color."""
import json
import subprocess

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
    def __init__(self, apps=(100,), servers=(877,), held=21, own=0, dies=True, opens=True, answers_after=1,
                 server_is_child=False):
        self.apps, self.servers, self.held, self.own = list(apps), list(servers), held, own
        self.server_is_child = server_is_child
        self.dies, self.opens, self.answers_after = dies, opens, answers_after
        self.killed, self.opened, self.asked = [], 0, 0

    def app(self):
        return self.apps

    def server(self):
        return self.servers

    def children(self, pid, besides=()):
        if pid in self.servers:
            return self.held
        child_servers = [s for s in self.servers if s not in besides] if self.server_is_child else []
        return self.own + len(child_servers)

    def kill(self, pid):
        self.killed.append(pid)

    def alive(self, pid):
        return not self.dies

    #: (pid, state, seconds since start, command line) rows, as ps gives them
    rows = ()
    t = 0.0
    resumed = None

    def table(self):
        return [(pid, "S" if pid in (self.resumed or []) else stat, age, cmd) for pid, stat, age, cmd in self.rows]

    def now(self):
        return self.t

    def resume(self, pid):
        self.resumed = (self.resumed or []) + [pid]

    def open(self):
        self.opened += 1
        return self.opens

    def answers(self):
        self.asked += 1
        if any(pid in self.apps and stat.startswith("T") for pid, stat, _a, _c in self.table()):
            return False
        return self.asked >= self.answers_after

    def sleep(self, s):
        self.t += s


def test_restart_stops_only_the_app_and_waits_for_it_to_answer():
    p = FakeProcs(answers_after=3)
    outcome, detail = iterm.restart_app(procs=p)
    assert outcome == "success" and p.killed == [100] and p.opened == 1 and p.asked == 3
    assert detail["terminals_before"] == 21 and detail["terminals_after"] == 21


def test_restart_refuses_when_the_terminals_would_end():
    for p in (FakeProcs(servers=()), FakeProcs(held=0), FakeProcs(own=2), FakeProcs(apps=(1, 2))):
        outcome, detail = iterm.restart_app(procs=p)
        assert outcome == "failure" and p.killed == [] and "nothing was stopped" in detail["message"]


def test_restart_does_not_count_the_session_server_as_the_apps_own():
    # the app started its session server, so the server is the app's child; stopping the app does
    # not end it, so it must not read as a process the restart would end
    p = FakeProcs(server_is_child=True)
    outcome, detail = iterm.restart_app(procs=p)
    assert outcome == "success" and p.killed == [100], detail
    p = FakeProcs(server_is_child=True, own=1)
    outcome, detail = iterm.restart_app(procs=p)
    assert outcome == "failure" and p.killed == [] and "holds 1 process" in detail["message"]


def test_children_leaves_out_the_pids_it_is_told_to():
    ps = "  1     0\n 100     1\n 877   100\n 901   100\n 950   877\n"
    run = lambda *a, **k: subprocess.CompletedProcess(a, 0, ps, "")
    assert iterm._children(100, None, run=run) == 2
    assert iterm._children(100, None, besides=(877,), run=run) == 1
    assert iterm._children(877, None, run=run) == 1


def test_restart_that_never_answers_is_a_failure():
    p = FakeProcs(answers_after=10**6)
    outcome, detail = iterm.restart_app(procs=p, answer_wait=9)
    assert outcome == "failure" and "did not answer" in detail["message"]
    p = FakeProcs(dies=False)
    assert iterm.restart_app(procs=p)[0] == "failure" and p.opened == 0


def test_start_opens_an_iterm2_that_is_not_running_and_waits_for_it():
    p = FakeProcs(apps=(), answers_after=2)
    outcome, detail = iterm.start_app(procs=p)
    assert outcome == "success" and p.opened == 1 and p.asked == 2 and p.killed == []
    p = FakeProcs(apps=(), answers_after=10**6)
    outcome, detail = iterm.start_app(procs=p, answer_wait=9)
    assert outcome == "failure" and "did not answer" in detail["message"]
    p = FakeProcs(apps=(), opens=False)
    assert iterm.start_app(procs=p)[0] == "failure"


ITERM = "/Applications/iTerm.app/Contents/MacOS/iTerm2"


def test_an_iterm2_left_stopped_at_launch_is_resumed_and_the_result_says_so():
    # macOS can hold a launch back and leave the new app stopped (state T): it never answers until
    # it gets SIGCONT, and then its launch finishes
    p = FakeProcs(apps=(), answers_after=1)
    real_open = p.open

    def open_stopped():
        p.apps, p.rows = [200], [(200, "T", 1.0, ITERM), (300, "T", 4000.0, "/usr/bin/osascript")]
        return real_open()
    p.open = open_stopped
    outcome, detail = iterm.start_app(procs=p)
    assert outcome == "success" and p.resumed == [200] and detail["launch_resumed"] is True, detail
    assert "it was resumed" in detail["message"], "the result says the launch had to be resumed"


def test_only_the_iterm2_this_call_opened_is_resumed():
    # an iTerm2 that was already stopped before the open (it started 600 s ago) is not this call's
    # launch, and a stopped process that is not iTerm2 is never touched
    p = FakeProcs(apps=(), answers_after=10**6)
    real_open = p.open

    def open_old():
        p.apps, p.rows = [200], [(200, "T", 600.0, ITERM), (300, "T", 2.0, "/usr/bin/osascript")]
        return real_open()
    p.open = open_old
    outcome, detail = iterm.start_app(procs=p, answer_wait=9)
    assert outcome == "failure" and p.resumed is None and detail["launch_resumed"] is False, detail
    p = FakeProcs(answers_after=3, )
    p.rows = [(100, "S", 50.0, ITERM)]
    outcome, detail = iterm.restart_app(procs=p)
    assert outcome == "success" and p.resumed is None and detail["resumed"] == [], "a running app gets no signal"


def test_a_restart_refuses_while_the_mac_holds_launches_back():
    # the app launched most recently is stopped: an iTerm2 stopped now could not be opened again
    p = FakeProcs()
    p.rows = [(100, "S", 9000.0, ITERM),
              (400, "T", 30.0, "/Users/x/Applications/Some Tool.app/Contents/MacOS/some-tool --flag"),
              (401, "S", 900.0, "/Applications/Other.app/Contents/MacOS/Other")]
    outcome, detail = iterm.restart_app(procs=p)
    assert outcome == "failure" and p.killed == [] and p.opened == 0 and detail["launches_held"] is True
    assert "Some Tool (pid 400" in detail["message"] and "nothing was stopped" in detail["message"], detail
    p.rows = [(100, "S", 9000.0, ITERM), (400, "T", 30.0, "/Applications/Some Tool.app/Contents/MacOS/some-tool"),
              (402, "S", 1.0, "/opt/homebrew/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/"
                              "Contents/MacOS/Python script.py")]
    outcome, detail = iterm.restart_app(procs=p)
    assert outcome == "failure" and detail.get("launches_held"), "a script run by Python.app is no newer launch"
    p.rows = [(100, "S", 9000.0, ITERM), (400, "T", 3000.0, "/Applications/Old.app/Contents/MacOS/Old"),
              (401, "S", 30.0, "/Applications/Other.app/Contents/MacOS/Other")]
    assert iterm.restart_app(procs=p)[0] == "success", "an old stopped app behind a newer launch that finished"


def test_the_process_table_is_read_from_ps():
    assert iterm.etime_seconds("05:03") == 303 and iterm.etime_seconds("1:00:00") == 3600
    assert iterm.etime_seconds("2-01:00:00") == 2 * 86400 + 3600 and iterm.etime_seconds("x") is None
    rows = iterm.parse_table("  12 Ss   1-02:03:04 /sbin/launchd\n 51177 T    00:16 /Applications/iTerm.app/Contents/MacOS/iTerm2\n")
    assert rows == [(12, "Ss", 93784.0, "/sbin/launchd"), (51177, "T", 16.0, ITERM)]


def test_start_leaves_a_running_iterm2_alone():
    p = FakeProcs()
    outcome, detail = iterm.start_app(procs=p)
    assert outcome == "skipped" and p.opened == 0 and p.killed == [] and "running" in detail["message"]


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


def _runner(clients: str, ps: str, rc: int = 0):
    import subprocess as sp

    def run(cmd, **_k):
        if cmd[0] == "ps":
            return sp.CompletedProcess(cmd, 0, ps, "")
        if "detach-client" in cmd:
            run.detached.append(cmd[-1])
            return sp.CompletedProcess(cmd, 0, "", "")
        return sp.CompletedProcess(cmd, rc, clients, "")
    run.detached = []
    return run


SERVER = "/Users/someone/Library/Application Support/iTerm2/iTermServer-3.6.10 /tmp/socket"
PS = (f"  1     0 /sbin/launchd\n"
      f" 50     1 {SERVER}\n"
      f"101    50 tmux -u attach -t claude-a\n"
      f"102    50 tmux -u attach -t claude-a\n"
      f"103    50 tmux -u attach -t claude-b\n"
      f" 60     1 /usr/sbin/sshd\n"
      f"104    60 tmux attach -t claude-b\n"
      f"105    50 /bin/zsh -l\n"
      f"106   105 tmux attach -t claude-c\n"
      f"107    50 tmux -u attach -t claude-c\n")
CLIENTS = ("/dev/ttys046\t101\t1000\t0\tclaude-a\n"
           "/dev/ttys048\t102\t1001\t0\tclaude-a\n"
           "/dev/ttys050\t103\t900\t0\tclaude-b\n"
           "/dev/ttys051\t104\t950\t0\tclaude-b\n"
           "/dev/ttys052\t106\t800\t0\tclaude-c\n"
           "/dev/ttys053\t107\t700\t1\tclaude-c\n"
           "/dev/ttys054\t107\t700\t0\tother-d\n")


def test_doubled_clients_are_the_newer_iterm_clients_of_one_session():
    got = iterm.doubled_clients(run=_runner(CLIENTS, PS))
    # claude-a is open in two iTerm2 tabs: the newer one is what to detach
    assert got == {"claude-a": [{"tty": "/dev/ttys048", "pid": 102, "created": 1001}]}
    # claude-b's second client is an ssh login, and claude-c's second is iTerm2's control-mode
    # client, so neither counts, and a session outside the prefix is never read


def test_doubled_clients_say_unknown_when_tmux_does_not_answer():
    assert iterm.doubled_clients(run=_runner("", PS, rc=1)) is None


def test_detach_client_names_the_terminal():
    run = _runner("", "")
    assert iterm.detach_client("/dev/ttys048", run=run) and run.detached == ["/dev/ttys048"]


# ----------------------------------------------------------------------------- a missing tmux

def _no_program(cmd, *a, **k):
    if cmd[0].endswith("tmux"):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])
    return subprocess.CompletedProcess(cmd, 0, PS, "")


def test_a_missing_tmux_is_raised_never_read_as_no_clients(monkeypatch):
    # a background job's PATH often lacks the folder tmux is in, and an empty answer then read as
    # "no chat is attached anywhere"
    monkeypatch.setattr(iterm.subprocess, "run", _no_program)
    with pytest.raises(iterm.TmuxNotFound, match="tmux"):
        iterm.tmux_by_tty()
    with pytest.raises(iterm.TmuxNotFound):
        iterm.tmux_client_ttys("claude-a", tmux="/no/such/tmux")
    with pytest.raises(iterm.TmuxNotFound):
        iterm.doubled_clients(run=_no_program)
    with pytest.raises(iterm.TmuxNotFound):
        iterm.detach_client("/dev/ttys048", run=_no_program)


def test_a_tmux_with_no_server_still_answers_no_clients(monkeypatch):
    def no_server(cmd, *a, **k):
        return subprocess.CompletedProcess(cmd, 1, "", "no server running on /tmp/tmux-1/default\n")
    monkeypatch.setattr(iterm.subprocess, "run", no_server)
    assert iterm.tmux_by_tty() == {}
    assert iterm.tmux_client_ttys("claude-a") == set()


def test_a_tmux_that_hangs_is_still_unknown(monkeypatch):
    def hangs(cmd, *a, **k):
        raise subprocess.TimeoutExpired(cmd, 5)
    monkeypatch.setattr(iterm.subprocess, "run", hangs)
    assert iterm.tmux_by_tty() == {}
    assert iterm.doubled_clients(run=hangs) is None
