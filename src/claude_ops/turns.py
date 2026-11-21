"""turns — which of a machine's waiting jobs go next, by class and then by place, with short jobs first.

    serve_order(waiters, classes, short=3, jump_s=600, now=None)    every waiter, in the order served
    short_order(waiters, classes, short=3, small=0, jump_s=600, now=None)
                                                                    the short waiters, in the order the short place takes them
    assign(waiters, free, classes, short=3, jump_s=600, now=None, small=0)
                                                                    {waiter id: the free place it takes}

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
`short_place` ("short") is kept for short waiters, and goes to the first short waiter in `short_order`
that no other place went to. Every other place goes to the waiters in the order served. A waiter takes
one place at most.

THE SHORT LINE. With `small=0` the short place takes the short waiters by class, then place. With
`small` from 1 to `short`, a short waiter of at most `small` units goes ahead of bigger short ones that
have not waited `jump_s`, oldest first within each of the two sizes. It is THE ORDER again, inside the
short waiters, with `small` as its short. So a one-test job is not held behind an older job of fifteen
that keeps the short place for several minutes, and a bigger short job that has waited `jump_s` goes next
whatever smaller job comes after it.
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


def short_order(waiters: Iterable[Mapping], classes: Sequence[str], short: int = 3, small: int = 0,
                jump_s: float = 600, now: float | None = None) -> list[Mapping]:
    """The short waiters (a size from 1 to `short`), in the order the short place takes them (see THE SHORT LINE).
    A `small` below 1 orders them by class and place alone, and one of `short` or more puts every short waiter in
    the smaller size, which is the same order."""
    return serve_order([w for w in waiters if is_short(w, short)], classes,
                       short=small if 1 <= small < short else 0, jump_s=jump_s, now=now)


def assign(waiters: Iterable[Mapping], free: Sequence[str], classes: Sequence[str], short: int = 3,
           jump_s: float = 600, now: float | None = None, short_place: str = "short", small: int = 0) -> dict:
    """{waiter id: the place it takes now} for the places in `free` (see THE PLACES and THE SHORT LINE)."""
    waiters = list(waiters)
    order = serve_order(waiters, classes, short=short, jump_s=jump_s, now=now)
    general = [p for p in free if p != short_place]
    got: dict = {}
    for w, p in zip(order, general):
        got[w.get("id")] = p
    if short_place in free:
        for w in short_order(waiters, classes, short=short, small=small, jump_s=jump_s, now=now):
            if w.get("id") not in got:
                got[w.get("id")] = short_place
                break
    return got
