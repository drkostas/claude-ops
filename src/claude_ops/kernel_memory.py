"""kernel_memory — where a Mac's memory goes when no process's footprint explains it.

Wired memory and the compressor hold memory that no process's footprint shows. When a machine runs
short and the footprints do not add up, the cause is often in the kernel: a zone that only grows, or a
process that never reads the Mach messages sent to its ports. Each such message stays queued in the
kernel, with the memory it carries, until the process reads it or exits.

Three readings, each from a macOS tool:

- `wired_gb()`: wired memory (what Activity Monitor shows as Wired Memory), from `vm_stat`, which needs
  no special rights.
- `read_zones()`: every kernel zone with its size now and its largest since boot, from `zprint -t`.
- `read_queued()`: for each process, the messages waiting on the ports it receives on, from `lsmp -a`.

`zprint` and `lsmp` read the kernel's and other processes' state, so they need root. The readers run
them through `sudo -n`, which fails at once instead of asking for a password, each under a timeout, and
answer None when the tool cannot run or does not finish. The `parse_*` functions read the tools' text
and need no rights. macOS only. Sizes are in bytes, and GB here means 1024 ** 3 bytes.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

GB = 1024 ** 3
#: how long one tool may run before its reading counts as unknown. Each takes a second or two on a
#: busy machine, and a caller that reads on a schedule must never wait long for either.
TIMEOUT_S = 30

_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
_SIZE = r"(\d+(?:\.\d+)?)([BKMG]?)"
# a zone's row in zprint -t: name, element size, size now, largest size, elements now, largest count,
# elements in use, allocation size, elements per allocation, flags (may be empty), total allocated
_ZONE = re.compile(rf"^(\S+)\s+(\d+)\s+{_SIZE}\s+{_SIZE}\s+(\d+)\s+(\d+)\s+(\d+)\s+{_SIZE}\s+(\d+)"
                   rf"\s+(?:[A-Z]+\s+)?{_SIZE}\s*$")
_PROCESS = re.compile(r"^Process \((\d+)\) : (.*)$")
# a receive right's row in lsmp: ... qlimit msgcount context, the context always 0x and 16 hex digits
_RECEIVE = re.compile(r"\s(\d+)\s+(\d+)\s+0x[0-9a-fA-F]{16}(?:\s|$)")


@dataclass(frozen=True)
class Zone:
    """One kernel zone. `size` is the memory it holds now and `peak` the most it has held since boot,
    both in bytes. A zone at its peak holds as much now as it ever has since boot."""
    name: str
    size: int
    peak: int
    elements: int

    @property
    def at_peak(self) -> bool:
        return self.size >= self.peak


@dataclass(frozen=True)
class Holder:
    """A process with messages waiting on the ports it receives on. `queued` counts the messages,
    `ports` the receive ports with any waiting and `full` those whose queue has reached its limit,
    where a sender has to wait or give up."""
    pid: int
    name: str
    queued: int
    ports: int
    full: int


def _bytes(number: str, unit: str) -> int:
    return int(float(number) * _UNITS[unit])


def parse_vm_stat(text: str) -> float | None:
    """Wired memory in GB from `vm_stat`'s text (its page size times "Pages wired down"), or None when
    the text does not say."""
    page = re.search(r"page size of (\d+) bytes", text or "")
    wired = re.search(r"Pages wired down:\s+(\d+)", text or "")
    if not page or not wired:
        return None
    return int(wired.group(1)) * int(page.group(1)) / GB


def parse_zprint(text: str) -> list[Zone]:
    """Every zone in `zprint -t`'s zone table, largest first. Rows of its other tables (wired memory
    by tag, the kernel maps) have other columns and are left out."""
    zones = []
    for line in (text or "").splitlines():
        m = _ZONE.match(line)
        if m:
            zones.append(Zone(name=m.group(1), size=_bytes(m.group(3), m.group(4)),
                              peak=_bytes(m.group(5), m.group(6)), elements=int(m.group(7))))
    return sorted(zones, key=lambda z: (-z.size, z.name))


def parse_lsmp(text: str) -> list[Holder]:
    """Every process with messages waiting on its receive ports in `lsmp -a`'s text, the most first.
    Only a port's own row counts. A row that starts with "-" lists a member of a port set, which is a
    port already counted on its own row."""
    counts: dict[tuple[int, str], list[int]] = {}
    who: tuple[int, str] | None = None
    for line in (text or "").splitlines():
        p = _PROCESS.match(line)
        if p:
            who = (int(p.group(1)), p.group(2).strip())
            continue
        if who is None or not line.startswith("0x") or "recv" not in line:
            continue
        m = _RECEIVE.search(line)
        if not m:
            continue
        limit, queued = int(m.group(1)), int(m.group(2))
        if queued:
            c = counts.setdefault(who, [0, 0, 0])
            c[0] += queued
            c[1] += 1
            c[2] += queued >= limit > 0
    holders = [Holder(pid=pid, name=name, queued=q, ports=n, full=f)
               for (pid, name), (q, n, f) in counts.items()]
    return sorted(holders, key=lambda h: (-h.queued, h.pid))


#: how long a command that ran past its timeout has to end after SIGTERM before it gets SIGKILL
TERM_GRACE_S = 2


def _run(cmd: Sequence[str], timeout: float, env: Mapping[str, str] | None) -> str | None:
    """A command's standard output, or None when it fails, cannot start or runs past the timeout.

    A command past its timeout gets SIGTERM first, and SIGKILL only if it is still running
    TERM_GRACE_S later. sudo passes SIGTERM on to the program it runs, but it cannot pass on a
    SIGKILL, so killing sudo outright would leave the tool running as root after the reader gave up."""
    try:
        proc = subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(env) if env is not None else None)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.communicate(timeout=TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        return None
    return out if proc.returncode == 0 else None


Runner = Callable[[Sequence[str], float, "Mapping[str, str] | None"], "str | None"]


def wired_gb(*, timeout: float = TIMEOUT_S, env: Mapping[str, str] | None = None,
             run: Runner = _run) -> float | None:
    """Wired memory in GB, from `vm_stat`. None when it cannot be read."""
    return parse_vm_stat(run(["/usr/bin/vm_stat"], timeout, env) or "")


def read_zones(*, timeout: float = TIMEOUT_S, env: Mapping[str, str] | None = None,
               run: Runner = _run) -> list[Zone] | None:
    """Every kernel zone, largest first, from `sudo -n zprint -t`. None when it cannot run (no root
    without a password, a timeout) or prints no zone, never an empty list that reads as no zones."""
    zones = parse_zprint(run(["/usr/bin/sudo", "-n", "/usr/bin/zprint", "-t"], timeout, env) or "")
    return zones or None


def read_queued(*, timeout: float = TIMEOUT_S, env: Mapping[str, str] | None = None,
                run: Runner = _run) -> list[Holder] | None:
    """Every process with messages waiting on its receive ports, the most first, from
    `sudo -n lsmp -a`. None when it cannot run. An empty list means it ran and found none waiting."""
    text = run(["/usr/bin/sudo", "-n", "/usr/bin/lsmp", "-a"], timeout, env)
    if text is None or "Process (" not in text:
        return None
    return parse_lsmp(text)
