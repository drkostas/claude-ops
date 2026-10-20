"""Reach an Android device over the network with adb again, after a restart undid `adb tcpip`.

`adb tcpip 5555` makes a device listen on a fixed port on every network it joins, Wi-Fi or mobile,
but a restart undoes it, and only adbd itself can turn it back on. Android 11 and later can also
listen through wireless debugging, which a restart keeps while the device stays on a Wi-Fi network
it trusts. Wireless debugging picks a new random port each time it starts and announces it only over
mDNS, which does not cross a VPN or another subnet, so a machine elsewhere cannot see it.

`reach` puts the two together. It tries the fixed port first. When that refuses, it scans the
address for the wireless-debugging port, connects there, and runs `tcpip`, so the fixed port
listens again until the next restart. It needs an address that reaches every port of the device
(its address on a VPN, for example) and a key the device already accepts.

    r = reach("192.0.2.7")
    if r.serial:            # "192.0.2.7:5555"
        ...

Every adb call goes through `run`, which a test replaces, and the port scan through `scan`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

#: the port `adb tcpip` listens on unless told otherwise
FIXED = 5555

#: where wireless debugging's random port falls: the ephemeral range of Linux (32768-60999), with
#: room on both sides for a device that changed it
WIRELESS_PORTS = range(30000, 65536)


@dataclass
class Reach:
    serial: str | None      # the adb serial that answers, None when nothing did
    how: str | None         # "fixed" (the fixed port answered), "wireless" (re-armed from wireless
                            # debugging) or "wireless-only" (wireless answered, the fixed port did not)
    detail: str
    wireless_port: int | None = None
    wireless_on: bool | None = None   # whether wireless debugging is still on afterwards (None: not asked)


def default_run(argv: list[str], *, timeout: float = 15, env: dict[str, str] | None = None
                ) -> subprocess.CompletedProcess:
    """`argv` captured as text. A timeout or a missing binary comes back as return code 124 with the
    reason in stderr, never an exception."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except (subprocess.SubprocessError, OSError) as e:
        return subprocess.CompletedProcess(argv, 124, "", f"{type(e).__name__}: {e}")


def open_ports(host: str, ports, *, timeout: float = 2.0, concurrency: int = 800) -> list[int]:
    """The ports of `host` that accept a TCP connection, in order. A port that refuses or does not
    answer within `timeout` seconds is left out. Over a VPN with a 100 ms round trip, 35,000 ports
    take about ten seconds."""
    async def scan() -> list[int]:
        found: list[int] = []
        gate = asyncio.Semaphore(concurrency)

        async def one(port: int) -> None:
            async with gate:
                try:
                    _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
                except (OSError, asyncio.TimeoutError):
                    return
                found.append(port)
                w.close()
        await asyncio.gather(*(one(p) for p in ports))
        return sorted(found)
    return asyncio.run(scan())


def alive(serial: str, *, run=default_run, adb: str = "adb") -> bool:
    """Whether the device at `serial` runs a command. A connection adb lists as connected can still
    be offline (a device that restarted leaves one behind), so only an answer counts."""
    r = run([adb, "-s", serial, "shell", "echo", "ok"], timeout=8)
    return r.returncode == 0 and r.stdout.strip() == "ok"


def _connect(serial: str, run, adb: str) -> bool:
    run([adb, "disconnect", serial], timeout=8)      # a stale offline entry answers "already connected"
    run([adb, "connect", serial], timeout=10)
    return alive(serial, run=run, adb=adb)


def wireless_on(serial: str, *, turn_on: bool = False, run=default_run, adb: str = "adb",
                sleep=time.sleep) -> bool | None:
    """Whether wireless debugging is on (the setting `adb_wifi_enabled`), None when it cannot be
    read. With `turn_on`, a setting found off is turned on and read again a second later. Android
    turns it straight back off on a network it does not trust and shows the person a prompt, so only
    turn it on where it was on a moment before."""
    def read() -> bool | None:
        r = run([adb, "-s", serial, "shell", "settings", "get", "global", "adb_wifi_enabled"], timeout=8)
        v = r.stdout.strip()
        return None if r.returncode != 0 or v not in ("0", "1") else v == "1"
    on = read()
    if on is False and turn_on:
        run([adb, "-s", serial, "shell", "settings", "put", "global", "adb_wifi_enabled", "1"], timeout=8)
        sleep(1)
        on = read()
    return on


def reach(host: str, *, fixed: int = FIXED, ports=WIRELESS_PORTS, skip=(), keep_wireless: bool = False,
          run=None, scan=open_ports, adb: str = "adb", settle: float = 4.0,
          sleep=time.sleep, env: dict[str, str] | None = None) -> Reach:
    """Reach the device at `host`: the fixed port if it answers, otherwise the wireless-debugging port
    found by scanning `ports`, from which `tcpip <fixed>` makes the fixed port listen again.
    `skip` names ports to leave alone. With `keep_wireless`, wireless debugging is turned back on
    after `tcpip` when it went off (it was on, so the network is one the device trusts). `env` is
    the environment every adb command runs with (None inherits); it is ignored when `run` is given."""
    if run is None:
        def run(argv, *, timeout=15):
            return default_run(argv, timeout=timeout, env=env)
    fixed_serial = f"{host}:{fixed}"
    if _connect(fixed_serial, run, adb):
        return Reach(fixed_serial, "fixed", f"port {fixed} answered")
    candidates = [p for p in scan(host, [p for p in ports if p != fixed and p not in skip])]
    for port in candidates:
        serial = f"{host}:{port}"
        if not _connect(serial, run, adb):
            run([adb, "disconnect", serial], timeout=8)   # leave no offline entry behind
            continue
        r = run([adb, "-s", serial, "tcpip", str(fixed)], timeout=15)
        sleep(settle)                                     # adbd restarts to listen on the fixed port
        run([adb, "disconnect", serial], timeout=8)
        if _connect(fixed_serial, run, adb):
            on = wireless_on(fixed_serial, turn_on=keep_wireless, run=run, adb=adb, sleep=sleep)
            return Reach(fixed_serial, "wireless", f"wireless debugging answered on port {port}; "
                         f"port {fixed} listens again", port, on)
        if _connect(serial, run, adb):
            return Reach(serial, "wireless-only", f"wireless debugging answered on port {port}, but "
                         f"port {fixed} did not after tcpip ({(r.stdout + r.stderr).strip()[:120]})", port)
    tried = f"{len(candidates)} open port{'s' if len(candidates) != 1 else ''} tried"
    return Reach(None, None, f"port {fixed} refused and no wireless debugging answered ({tried})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops adb-reach",
                                description="reach an Android device over adb again after a restart "
                                            "(exit 0 reached, 1 not)")
    p.add_argument("host", help="the device's address on a network that reaches all its ports")
    p.add_argument("--fixed", type=int, default=FIXED, help="the port tcpip listens on (5555)")
    p.add_argument("--from", dest="lo", type=int, default=WIRELESS_PORTS.start, help="first port to scan")
    p.add_argument("--to", dest="hi", type=int, default=WIRELESS_PORTS.stop - 1, help="last port to scan")
    p.add_argument("--skip", type=int, action="append", default=[], help="a port to leave alone")
    p.add_argument("--keep-wireless", action="store_true",
                   help="turn wireless debugging back on after tcpip when it went off")
    p.add_argument("--adb", default="adb", help="the adb binary")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    r = reach(a.host, fixed=a.fixed, ports=range(a.lo, a.hi + 1), skip=set(a.skip),
              keep_wireless=a.keep_wireless, adb=a.adb)
    print(json.dumps(asdict(r)) if a.json else (f"{r.serial}: {r.detail}" if r.serial else r.detail))
    return 0 if r.serial else 1


if __name__ == "__main__":
    sys.exit(main())
