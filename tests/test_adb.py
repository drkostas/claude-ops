"""adb.reach: the fixed port, else the wireless-debugging port found by a scan and tcpip from there."""
from __future__ import annotations

import asyncio
import collections
import errno
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from claude_ops import adb, cli

HOST = "192.0.2.7"


class Device:
    """A fake device and adb client: which ports adbd listens on, what `settings` holds, and every
    call made, so a test checks what reach did as well as what it returned."""

    def __init__(self, fixed=False, tls=None, wifi="1", tcpip_works=True, wifi_off_after_tcpip=False):
        self.fixed, self.tls, self.wifi = fixed, tls, wifi
        self.tcpip_works, self.wifi_off_after_tcpip = tcpip_works, wifi_off_after_tcpip
        self.connected: set[str] = set()
        self.calls: list[list[str]] = []

    def listens(self, port):
        return (port == 5555 and self.fixed) or port == self.tls

    def run(self, argv, *, timeout=15, env=None):
        self.calls.append(argv[1:])
        args, out = argv[1:], ""
        if args[0] == "connect":
            port = int(args[1].rsplit(":", 1)[1])
            if self.listens(port):
                self.connected.add(args[1])
                out = f"connected to {args[1]}"
        elif args[0] == "disconnect":
            self.connected.discard(args[1])
        elif args[0] == "-s":
            serial, rest = args[1], args[2:]
            if serial not in self.connected or not self.listens(int(serial.rsplit(":", 1)[1])):
                return subprocess.CompletedProcess(argv, 1, "", "device offline")
            if rest == ["shell", "echo", "ok"]:
                out = "ok\n"
            elif rest[0] == "tcpip":
                if self.tcpip_works:
                    self.fixed = True
                    if self.wifi_off_after_tcpip:
                        self.wifi, self.tls = "0", None
                self.connected.clear()
                out = f"restarting in TCP mode port: {rest[1]}\n"
            elif rest[:4] == ["shell", "settings", "get", "global"]:
                out = self.wifi + "\n"
            elif rest[:4] == ["shell", "settings", "put", "global"]:
                self.wifi = rest[5]
        return subprocess.CompletedProcess(argv, 0, out, "")

    def scan(self, host, ports):
        return [p for p in ports if self.listens(p)]


def go(d, **kw):
    kw.setdefault("scan", d.scan)
    return adb.reach(HOST, run=d.run, sleep=lambda s: None, **kw)


def test_the_fixed_port_answers_and_nothing_is_scanned():
    d = Device(fixed=True)
    r = go(d, scan=lambda h, p: (_ for _ in ()).throw(AssertionError("scanned")))
    assert (r.serial, r.how) == (f"{HOST}:5555", "fixed")


def test_a_restarted_device_is_re_armed_from_its_wireless_debugging_port():
    d = Device(tls=41759)
    r = go(d)
    assert (r.serial, r.how, r.wireless_port) == (f"{HOST}:5555", "wireless", 41759)
    assert ["-s", f"{HOST}:41759", "tcpip", "5555"] in d.calls
    assert r.wireless_on is True and d.fixed
    assert f"{HOST}:41759" not in d.connected        # the wireless serial is let go


def test_wireless_debugging_that_went_off_is_turned_back_on_only_when_asked():
    r = go(Device(tls=41759, wifi_off_after_tcpip=True))
    assert r.how == "wireless" and r.wireless_on is False
    d = Device(tls=41759, wifi_off_after_tcpip=True)
    r = go(d, keep_wireless=True)
    assert r.wireless_on is True and d.wifi == "1"


def test_an_open_port_that_is_not_adb_is_dropped_and_the_next_tried():
    d = Device(tls=41759)
    r = go(d, scan=lambda h, p: [1, 41759])
    assert r.how == "wireless"
    assert ["disconnect", f"{HOST}:1"] in d.calls


def test_tcpip_that_does_not_take_still_gives_the_wireless_serial():
    r = go(Device(tls=41759, tcpip_works=False))
    assert (r.serial, r.how) == (f"{HOST}:41759", "wireless-only")


def test_nothing_answering_says_so():
    r = go(Device())
    assert r.serial is None and "0 open ports tried" in r.detail


def test_skip_and_the_fixed_port_are_left_out_of_the_scan():
    seen = []
    go(Device(), scan=lambda h, p: seen.extend(p) or [], ports=range(5550, 5560), skip={5552})
    assert 5555 not in seen and 5552 not in seen and 5551 in seen


def test_open_ports_finds_a_listening_socket_and_not_a_closed_one():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    gone = closed.getsockname()[1]
    closed.close()
    try:
        assert adb.open_ports("127.0.0.1", [port, gone], timeout=1) == [port]
    finally:
        s.close()


def test_a_missing_adb_binary_is_an_answer_not_an_exception():
    r = adb.default_run(["/nonexistent/adb", "devices"])
    assert r.returncode == 124 and "FileNotFoundError" in r.stderr


def test_the_cli_lists_it():
    assert "adb-reach" in cli.TOOLS


def test_env_reaches_every_adb_command(monkeypatch):
    seen = []
    monkeypatch.setattr(adb, "default_run",
                        lambda argv, timeout=15, env=None: seen.append(env) or subprocess.CompletedProcess(argv, 1, "", ""))
    adb.reach(HOST, scan=lambda h, p: [], env={"PATH": "/x"}, sleep=lambda s: None)
    assert seen and all(e == {"PATH": "/x"} for e in seen)


def test_the_scan_wait_follows_the_measured_round_trip():
    assert adb.scan_timeout(0.7) == 2.8                 # a slow link waits longer than the usual two seconds
    assert adb.scan_timeout(0.1) == 2.0                 # a near one keeps the floor
    assert adb.scan_timeout(None) == 2.0                # nothing measured: the floor
    assert adb.scan_timeout(10.0) == 15.0               # and never past the ceiling
    assert adb.scan_timeout(1.0, floor=1.0, factor=2.0, ceiling=9.0) == 2.0


def test_round_trip_counts_a_refusal_and_gives_none_for_silence():
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    gone = closed.getsockname()[1]
    closed.close()
    rtt = adb.round_trip("127.0.0.1", gone)
    assert rtt is not None and 0 <= rtt < 1


def test_open_ports_measures_its_wait_when_none_is_given(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    asked = []
    monkeypatch.setattr(adb, "round_trip", lambda host, *a, **k: asked.append(host) or 0.5)
    try:
        assert adb.open_ports("127.0.0.1", [port]) == [port]
    finally:
        s.close()
    assert asked == ["127.0.0.1"]


def test_reach_hands_the_scan_its_wait_only_when_one_is_given():
    seen = []

    def scan(host, ports, **kw):
        seen.append(kw)
        return []
    go(Device(), scan=scan)
    go(Device(), scan=scan, scan_wait=6.0)
    assert seen == [{}, {"timeout": 6.0}]


def test_the_cli_takes_a_scan_wait(monkeypatch):
    got = {}
    monkeypatch.setattr(adb, "reach", lambda host, **kw: got.update(kw) or adb.Reach(None, None, "none"))
    assert adb.main([HOST, "--scan-wait", "4.5"]) == 1
    assert got["scan_wait"] == 4.5
    adb.main([HOST])
    assert got["scan_wait"] is None


class Link:
    """A connector for open_ports: `plan[port]` says what each ask of that port does in turn, and the
    last step repeats. "open" connects, "refuse" is refused, "silent" never answers, "late" is refused
    after a twentieth of a second, "slow" connects after half a second and "emfile" fails at once with
    too many open files. A port not in
    the plan is refused. Every ask is counted."""

    class Writer:
        def close(self):
            pass

    def __init__(self, plan=None, default=("refuse",)):
        self.plan = {p: list(v) for p, v in (plan or {}).items()}
        self.default = list(default)
        self.asked = collections.Counter()

    async def __call__(self, host, port):
        self.asked[port] += 1
        steps = self.plan.get(port, self.default)
        what = steps[min(self.asked[port], len(steps)) - 1]
        if what == "refuse":
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        if what == "emfile":
            raise OSError(errno.EMFILE, "Too many open files")
        if what == "silent":
            await asyncio.sleep(3600)
        if what == "late":
            await asyncio.sleep(0.05)
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        if what == "slow":
            await asyncio.sleep(0.5)
        return None, self.Writer()


def scan(link, ports, **kw):
    kw = {"timeout": 0.05, "retry_timeout": 0.2, "retry_budget": 5, **kw}
    return adb.open_ports(HOST, ports, connect=link, **kw)


def test_a_port_that_gave_no_answer_is_asked_again_and_a_refused_one_never():
    link = Link({41759: ["silent", "open"], 41800: ["emfile", "open"], 41900: ["silent"]})
    assert scan(link, range(41000, 42000)) == [41759, 41800]
    assert link.asked[41759] == link.asked[41800] == link.asked[41900] == 2
    refused = [p for p in range(41000, 42000) if p not in (41759, 41800, 41900)]
    assert all(link.asked[p] == 1 for p in refused)


def test_the_second_pass_runs_fewer_at_once_than_the_first(monkeypatch):
    monkeypatch.setattr(adb, "fd_room", lambda: None)   # whatever this machine's file limit
    seen, now = [], [0]

    async def counting(host, port):
        now[0] += 1
        seen.append(now[0])
        try:
            await asyncio.sleep(3600)
        finally:
            now[0] -= 1
    adb.open_ports(HOST, range(300), connect=counting, timeout=0.05, concurrency=150,
                   retry_concurrency=7, retry_timeout=0.05, retry_budget=0.3)
    assert max(seen) == 150 and max(seen[300:]) == 7


def test_a_link_that_drops_everything_is_not_asked_again_port_by_port():
    link = Link(default=["silent"])
    t = time.monotonic()
    assert scan(link, range(2000), timeout=0.01, retry_timeout=0.2, retry_concurrency=10, retry_budget=60) == []
    assert time.monotonic() - t < 5
    assert sum(link.asked.values()) - 2000 < 100     # the second pass gave up after 0.4 s of silence


def test_the_second_pass_ends_at_its_budget():
    link = Link(default=["silent", "late"])          # each answers, but 10 at once take 25 s for 5000
    t = time.monotonic()
    assert scan(link, range(5000), timeout=0.01, retry_concurrency=10, retry_budget=0.5) == []
    assert time.monotonic() - t < 4
    assert sum(link.asked.values()) - 5000 < 1000


def test_the_second_pass_waits_at_least_four_seconds_by_default():
    link = Link({41759: ["silent", "slow"]})       # half a second, over twice the first pass's wait
    assert adb.open_ports(HOST, range(41700, 41800), connect=link, timeout=0.02) == [41759]


def test_a_budget_of_zero_leaves_the_second_pass_out():
    link = Link({41759: ["silent", "open"]})
    assert scan(link, [41759, 41760], retry_budget=0) == []
    assert link.asked[41759] == 1


def test_connections_stay_under_the_open_file_limit():
    """A scan with more connections at once than the process may open files lost the ports over the
    limit to "too many open files" without a word, an open one among them. A launch agent on macOS
    starts with a limit of 256."""
    resource = pytest.importorskip("resource")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 256:
        pytest.skip("the hard open-file limit is under 256")
    child = textwrap.dedent("""
        import resource, socket, sys
        from claude_ops import adb
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1000)
        port = s.getsockname()[1]
        hi = min(65536, port + 500)
        found = adb.open_ports("127.0.0.1", range(hi - 2000, hi), timeout=2, retry_budget=0)
        print(port in found)
    """)
    src = str(Path(adb.__file__).resolve().parents[1])
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": src})
    assert r.stdout.strip() == "True", r.stdout + r.stderr


def test_fd_room_leaves_a_reserve_under_the_limit(monkeypatch):
    resource = pytest.importorskip("resource")
    monkeypatch.setattr(adb.resource, "getrlimit", lambda what: (256, resource.RLIM_INFINITY))
    assert adb.fd_room() == 192 and adb.fd_room(reserve=6) == 250
    monkeypatch.setattr(adb.resource, "getrlimit", lambda what: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    assert adb.fd_room() is None
