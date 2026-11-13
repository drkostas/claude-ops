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
import contextlib
import functools
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

try:
    import resource
except ImportError:      # not on Windows
    resource = None

#: the port `adb tcpip` listens on unless told otherwise
FIXED = 5555

#: where wireless debugging's random port falls: the ephemeral range of Linux (32768-60999), with
#: room on both sides for a device that changed it
WIRELESS_PORTS = range(30000, 65536)

#: file descriptors a port scan leaves to the rest of the program when it keeps its connections
#: under the process's open-file limit
FD_RESERVE = 64


@dataclass
class Reach:
    serial: str | None      # the adb serial that answers, None when nothing did
    how: str | None         # "fixed" (the fixed port answered), "wireless" (re-armed from wireless
                            # debugging) or "wireless-only" (wireless answered, the fixed port did not)
    detail: str
    wireless_port: int | None = None
    wireless_on: bool | None = None   # whether wireless debugging is still on afterwards (None: not asked)
    unanswered: int = 0     # ports the scan got no answer from, so it cannot say nothing listens there
    unasked: int = 0        # of those, the ports its second pass did not ask again before it ended


class Ports(list):
    """The open ports a scan found, in order, and how much of the range it could not read.
    `unanswered` counts the ports that neither connected nor refused on any ask, and `unasked` those
    of them the second pass did not ask again before it ended (on its budget, on a link that answered
    nothing, or with `retry_budget=0`). With both 0 every other port refused, so nothing else listens.
    A scan that leaves ports unanswered found no port there, which is not the same as no port."""

    def __init__(self, ports=(), *, unanswered: int = 0, unasked: int = 0):
        super().__init__(ports)
        self.unanswered, self.unasked = unanswered, unasked


def default_run(argv: list[str], *, timeout: float = 15, env: dict[str, str] | None = None
                ) -> subprocess.CompletedProcess:
    """`argv` captured as text. A timeout or a missing binary comes back as return code 124 with the
    reason in stderr, never an exception."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except (subprocess.SubprocessError, OSError) as e:
        return subprocess.CompletedProcess(argv, 124, "", f"{type(e).__name__}: {e}")


def round_trip(host: str, port: int = 1, *, timeout: float = 5.0) -> float | None:
    """Seconds one TCP connect to `port` of `host` takes. A refusal counts, because it is the device's
    own answer; None when nothing answered within `timeout`. Pick a port nothing listens on, so the
    answer comes back at once and the time is the distance."""
    async def one() -> float | None:
        start = time.monotonic()
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
            w.close()
        except ConnectionRefusedError:
            pass
        except (OSError, asyncio.TimeoutError):
            return None
        return time.monotonic() - start
    return asyncio.run(one())


def scan_timeout(rtt: float | None, *, floor: float = 2.0, factor: float = 4.0, ceiling: float = 15.0) -> float:
    """How long a scan waits for one port, from the measured round trip: `factor` round trips, never
    under `floor` and never over `ceiling`. A fixed two seconds missed a port over a link of 700 ms,
    where a slow connect plus a busy machine took longer. Without a measurement it is `floor`."""
    if rtt is None:
        return floor
    return min(ceiling, max(floor, factor * rtt))


def fd_room(reserve: int = FD_RESERVE) -> int | None:
    """How many connections can be open at once under this process's open-file limit, leaving
    `reserve` files to the rest of the program. None when the limit is unlimited or cannot be read."""
    if resource is None:
        return None
    try:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None
    if soft == resource.RLIM_INFINITY:
        return None
    return max(1, soft - reserve)


def open_ports(host: str, ports, *, timeout: float | None = None, concurrency: int = 800,
               retry_timeout: float | None = None, retry_concurrency: int = 200, retry_budget: float = 90.0,
               connect=None) -> Ports:
    """The ports of `host` that accept a TCP connection, in order, as a `Ports` list that also says
    how many ports gave no answer. Over a VPN with a 100 ms round trip, 35,000 ports take about ten
    seconds.

    The first pass asks every port, `concurrency` at once, and waits `timeout` seconds for each. With
    no `timeout` it is measured first, from a connect to a port nothing listens on
    (`scan_timeout(round_trip(host))`), so a slow link gets a longer wait and a near one the usual two
    seconds. A port that refuses has answered and is never asked again. A port that gave no answer (a
    timeout, or a local error such as too many open files) is asked again in a second pass,
    `retry_concurrency` at once with `retry_timeout` seconds each (with none, twice the first pass's
    wait and never under four seconds). Over a slow link, hundreds of connections at once can crowd
    it until most of them time out, where fewer at once all answer.

    The second pass ends after `retry_budget` seconds whatever is left, and after `2 * retry_timeout`
    seconds when no port has answered it yet, so a link that drops everything is not asked again
    port by port. `retry_budget=0` leaves the second pass out.

    Both passes keep the connections open at once under the process's open-file limit (`fd_room`).
    A connection over that limit fails at once without asking the host, so a scan above it misses
    ports that are open.

    `connect(host, port)` is the coroutine that opens a connection, `asyncio.open_connection` unless
    the caller gives another (a test does)."""
    if timeout is None:
        timeout = scan_timeout(round_trip(host))
    if retry_timeout is None:
        retry_timeout = max(4.0, 2 * timeout)
    connect = connect or asyncio.open_connection
    room = fd_room()

    def at_once(n: int) -> int:
        return max(1, min(n, room) if room else n)

    async def scan() -> Ports:
        found: list[int] = []
        silent: list[int] = []
        again: list[int] = []
        heard = 0

        async def ask(port: int, gate: asyncio.Semaphore, wait: float, misses: list[int] | None) -> None:
            nonlocal heard
            async with gate:
                try:
                    _, w = await asyncio.wait_for(connect(host, port), wait)
                except (ConnectionRefusedError, ConnectionResetError):
                    heard += 1                  # the host answered, and nothing listens there
                    return
                except (OSError, asyncio.TimeoutError):
                    if misses is not None:
                        misses.append(port)
                    return
                heard += 1
                found.append(port)
                w.close()

        gate = asyncio.Semaphore(at_once(concurrency))
        await asyncio.gather(*(ask(p, gate, timeout, silent) for p in ports))
        if not silent or retry_budget <= 0:
            return Ports(sorted(found), unanswered=len(silent), unasked=len(silent))
        heard = 0
        gate = asyncio.Semaphore(at_once(retry_concurrency))
        second = asyncio.ensure_future(asyncio.gather(*(ask(p, gate, retry_timeout, again) for p in silent)))
        quiet = min(retry_budget, 2 * retry_timeout)
        await asyncio.wait({second}, timeout=quiet)
        if not second.done() and heard:
            await asyncio.wait({second}, timeout=retry_budget - quiet)
        if not second.done():
            second.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await second
        # a port the second pass answered counts in `heard`, one it asked in vain is in `again`, and
        # one it never finished asking is in neither
        return Ports(sorted(found), unanswered=len(silent) - heard, unasked=len(silent) - heard - len(again))
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
          sleep=time.sleep, env: dict[str, str] | None = None,
          scan_wait: float | None = None) -> Reach:
    """Reach the device at `host`: the fixed port if it answers, otherwise the wireless-debugging port
    found by scanning `ports`, from which `tcpip <fixed>` makes the fixed port listen again.
    `skip` names ports to leave alone. With `keep_wireless`, wireless debugging is turned back on
    after `tcpip` when it went off (it was on, so the network is one the device trusts). `env` is
    the environment every adb command runs with (None inherits); it is ignored when `run` is given.
    `scan_wait` is how many seconds the scan waits for one port (None measures it, see `open_ports`)."""
    if run is None:
        def run(argv, *, timeout=15):
            return default_run(argv, timeout=timeout, env=env)
    fixed_serial = f"{host}:{fixed}"
    if _connect(fixed_serial, run, adb):
        return Reach(fixed_serial, "fixed", f"port {fixed} answered")
    find = scan if scan_wait is None else functools.partial(scan, timeout=scan_wait)
    found = find(host, [p for p in ports if p != fixed and p not in skip])
    candidates = list(found)
    unread = {"unanswered": getattr(found, "unanswered", 0), "unasked": getattr(found, "unasked", 0)}
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
                         f"port {fixed} listens again", port, on, **unread)
        if _connect(serial, run, adb):
            return Reach(serial, "wireless-only", f"wireless debugging answered on port {port}, but "
                         f"port {fixed} did not after tcpip ({(r.stdout + r.stderr).strip()[:120]})", port,
                         **unread)
    tried = f"{len(candidates)} open port{'s' if len(candidates) != 1 else ''} tried"
    if unread["unanswered"]:
        tried += (f", {unread['unanswered']} port{'s' if unread['unanswered'] != 1 else ''} gave no answer "
                  f"and {unread['unasked']} of them were not asked again")
    return Reach(None, None, f"port {fixed} refused and no wireless debugging answered ({tried})", **unread)


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
    p.add_argument("--scan-wait", type=float, default=None,
                   help="seconds the scan waits for one port (default: measured from the round trip)")
    p.add_argument("--adb", default="adb", help="the adb binary")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    r = reach(a.host, fixed=a.fixed, ports=range(a.lo, a.hi + 1), skip=set(a.skip),
              keep_wireless=a.keep_wireless, adb=a.adb, scan_wait=a.scan_wait)
    print(json.dumps(asdict(r)) if a.json else (f"{r.serial}: {r.detail}" if r.serial else r.detail))
    return 0 if r.serial else 1


if __name__ == "__main__":
    sys.exit(main())
