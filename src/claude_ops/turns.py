"""turns — which of a machine's waiting jobs go next, by class and then by place, with short jobs first.

    serve_order(waiters, classes, short=3, jump_s=600, now=None)    every waiter, in the order served
    assign(waiters, free, classes, short=3, jump_s=600, now=None)   {waiter id: the free place it takes}

WHY. Jobs that share one machine wait for a few places (slots, locks). Served oldest first, one class
of job waits behind another that matters less, and a run of three tests waits an hour behind long
runs that asked a minute earlier. This is the one rule for who goes next, kept apart from how places
are held, so a lock folder, a database row or a slot file can all ask it.

A WAITER is a mapping with:
  id      anything that names it (a pid, a ticket)
  cls     its class, one of `classes`, which are given highest first; a class not listed comes last
  place   when it took its place in the line, in seconds since the epoch (a waiter started again can
          be given its earlier place)
  size    how many units of work it has (tests, files), or None when it cannot say, which counts as long

THE ORDER. Classes go in the order given. Within a class the next is the oldest SHORT waiter (a size
from 1 to `short`), unless a long waiter before it has waited `jump_s` already, in which case the
oldest goes. So short jobs go ahead of long ones that have not waited long, and no long job is passed
for ever. `short=0` turns that off and the order is by place alone. Ties in place go by id.

THE PLACES. `free` lists the places open now, in the order they should be filled. A place named
`short_place` ("short") is kept for short waiters, and goes to the first short waiter (by class, then
place) that no other place went to. Every other place goes to the waiters in the order served. A
waiter takes one place at most.
"""
from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence


def _rank(classes: Sequence[str]):
    order = {c: i for i, c in enumerate(classes)}
    return lambda w: order.get(w.get("cls"), len(order))


def is_short(w: Mapping, short: int) -> bool:
    """Whether a waiter is short: a size from 1 to `short`. A size that is missing or not a number is long."""
    size = w.get("size")
    return isinstance(size, int) and not isinstance(size, bool) and short >= 1 and 1 <= size <= short


def _key(w: Mapping):
    return (float(w.get("place") or 0), str(w.get("id")))


def serve_order(waiters: Iterable[Mapping], classes: Sequence[str], short: int = 3, jump_s: float = 600,
                now: float | None = None) -> list[Mapping]:
    """Every waiter, in the order they are served (see THE ORDER)."""
    now = time.time() if now is None else now
    rank = _rank(classes)
    groups: dict[int, list[Mapping]] = {}
    for w in waiters:
        groups.setdefault(rank(w), []).append(w)
    out: list[Mapping] = []
    for r in sorted(groups):
        left = sorted(groups[r], key=_key)
        while left:
            pick = None
            for w in left:
                if is_short(w, short):
                    pick = w
                    break
                if now - float(w.get("place") or 0) >= jump_s:
                    break                              # a long waiter has waited long enough: the oldest goes
            pick = pick if pick is not None else left[0]
            out.append(pick)
            left.remove(pick)
    return out


def assign(waiters: Iterable[Mapping], free: Sequence[str], classes: Sequence[str], short: int = 3,
           jump_s: float = 600, now: float | None = None, short_place: str = "short") -> dict:
    """{waiter id: the place it takes now} for the places in `free` (see THE PLACES)."""
    waiters = list(waiters)
    order = serve_order(waiters, classes, short=short, jump_s=jump_s, now=now)
    general = [p for p in free if p != short_place]
    got: dict = {}
    for w, p in zip(order, general):
        got[w.get("id")] = p
    if short_place in free:
        rank = _rank(classes)
        for w in sorted(waiters, key=lambda w: (rank(w), *_key(w))):
            if is_short(w, short) and w.get("id") not in got:
                got[w.get("id")] = short_place
                break
    return got
