"""The check a program makes before it signals processes.

A tool that ends a process (a chat closer, a test runner cleaning up, a deploy script) is one wrong
number away from ending far more than it meant to. `kill(-1)` signals every process the caller may
signal, `kill(0)` the caller's whole process group, `kill(1)` the init process, and a list built
from a pattern that matched too much signals all of it. The check here refuses those shapes before
anything is sent:

- pid 1, pid 0 and pid -1;
- a negative pid (a process group) the caller did not start, where `own_groups` names the groups it
  did;
- more than `max_targets` targets (3 by default).

A pid that is not an integer is refused too. The check has no approval path. It gives the reason as
text, so a caller can show it, and `require` raises `Refused` with the same text.

    from claude_ops import killguard
    killguard.require(pids)                      # raises Refused
    killguard.kill(pids, signal.SIGTERM)         # checks, then signals

From a shell script: `python3 -m claude_ops.killguard 123 456` exits 0 when those targets may be
signalled and 2 with the reason when not; `--own-group N` (repeatable) names a group the caller
started, and `--max N` changes the number of targets allowed.
"""
from __future__ import annotations

import os
import re
import signal as _signal
import subprocess
import sys
from collections.abc import Callable, Iterable

MAX_TARGETS = 3


class Refused(Exception):
    """The targets are too wide to signal."""


def check(targets: Iterable[int], own_groups: Iterable[int] = (), max_targets: int = MAX_TARGETS) -> str | None:
    """Why these targets may not be signalled, or None. A negative target is a process group."""
    pids = list(targets)
    own = set(own_groups)
    for p in pids:
        if isinstance(p, bool) or not isinstance(p, int):
            return f"{p!r} is not a process id"
        if p == 1:
            return "pid 1 is the init process"
        if p == 0:
            return "pid 0 is the caller's whole process group"
        if p == -1:
            return "pid -1 is every process the caller may signal"
        if p < 0 and -p not in own:
            return f"{p} is a process group this caller did not start"
    if len(pids) > max_targets:
        return f"{len(pids)} targets is more than the {max_targets} one call may signal"
    return None


def require(targets: Iterable[int], own_groups: Iterable[int] = (), max_targets: int = MAX_TARGETS) -> None:
    why = check(targets, own_groups, max_targets)
    if why:
        raise Refused(why)


def kill(targets: Iterable[int], sig: int = _signal.SIGTERM, own_groups: Iterable[int] = (),
         max_targets: int = MAX_TARGETS, send: Callable[[int, int], None] | None = None) -> None:
    """Check `targets`, then send `sig` to each (a group with killpg). `send(pid, sig)` replaces the
    real call, for a caller that already has its own."""
    pids = list(targets)
    require(pids, own_groups, max_targets)
    for p in pids:
        if send is not None:
            send(p, sig)
        elif p < 0:
            os.killpg(-p, sig)
        else:
            os.kill(p, sig)


#: a process-table row: pid -> (parent pid, process group, command)
Table = dict


def process_table() -> Table | None:
    """Every process as pid -> (ppid, pgid, command), or None when `ps` cannot be read."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,command="], capture_output=True, text=True,
                             timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table: Table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and all(x.isdigit() for x in parts[:3]):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3] if len(parts) > 3 else "")
    return table


def ancestors(pid: int, table: Table) -> list[int]:
    """The parents of `pid`, nearest first."""
    chain: list[int] = []
    while pid in table and table[pid][0] > 1 and table[pid][0] not in chain:
        pid = table[pid][0]
        chain.append(pid)
    return chain


def session_root(table: Table, start: int | None = None) -> int | None:
    """The Claude Code process that runs the caller: the nearest ancestor whose program is `claude`."""
    for pid in ancestors(os.getpid() if start is None else start, table):
        first = table[pid][2].split(None, 1)[0] if table[pid][2].strip() else ""
        if re.fullmatch(r"(?:.*/)?claude(?:-code)?", first):
            return pid
    return None


def started_groups(table: Table, root: int | None = None) -> set[int]:
    """The process groups that hold a process descended from the session `root` (this caller's own
    Claude Code process by default): groups the session started, such as a background task.
    A group of the session's own process, which is also the terminal's, is not one of them."""
    root = session_root(table) if root is None else root
    if root is None:
        return set()
    groups = {g for pid, (_, g, _) in table.items() if root in ancestors(pid, table)}
    return {g for g in groups if g > 1 and g != table[root][1]}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    own: list[int] = []
    limit = MAX_TARGETS
    targets: list[object] = []
    i = 0
    try:
        while i < len(argv):
            a = argv[i]
            if a == "--own-group" and i + 1 < len(argv):
                own.append(int(argv[i + 1]))
                i += 2
            elif a == "--max" and i + 1 < len(argv):
                limit = int(argv[i + 1])
                i += 2
            elif a == "--":
                targets += argv[i + 1:]
                break
            else:
                targets.append(a)
                i += 1
        pids = [int(t) for t in targets]
    except ValueError:
        print("Blocked: a target that is not an integer process id", file=sys.stderr)
        return 2
    why = check(pids, own, limit)
    if why:
        print(f"Blocked: {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
