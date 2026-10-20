"""adb.reach: the fixed port, else the wireless-debugging port found by a scan and tcpip from there."""
from __future__ import annotations

import socket
import subprocess

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
