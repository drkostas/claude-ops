"""ladder — tell a running chat before acting on it, and escalate only as far as needed.

A supervisor that pauses, restarts or stops a chat, or moves it to another login, can do it at once,
and the chat loses the step it was in the middle of. A chat told first can finish or park that step
and end its turn, and nothing is lost. Telling is not enough on its own: the message can wait in a
queue behind a long turn, and a chat can keep working past the time the action must happen.

So the notice climbs a ladder, one rung at a time, and stops as soon as the chat has ended its turn:

1. queue    the notice waits for the chat like any other message.
2. skip     if it has not been delivered `skip_after` seconds after the start, push it now.
3. interrupt  if the chat has not ended a turn since the notice reached it by `interrupt_at`, press
            the key that stops its turn once (and tell it again why).
4. act      once it ends a turn after the notice or the interrupt, or once `grace` has passed after
            the interrupt, or after `interrupt_at` when the key could not be pressed.

This module is only the rule. `next_step(seen, timing, may_interrupt)` answers what to do now from
what was seen; the caller delivers, pushes, presses and acts, and records each step itself.

    from claude_ops import ladder
    t = ladder.Timing(skip_after=120, interrupt_at=ladder.interrupt_at(start, after=600))
    step = ladder.next_step(ladder.Seen(started=start, now=time.time(), delivered=...), t)
"""
from __future__ import annotations

from dataclasses import dataclass

WAIT, SKIP, INTERRUPT, ACT, DONE = "wait", "skip", "interrupt", "act", "done"


@dataclass(frozen=True)
class Timing:
    """When to climb. Times are epoch seconds except where named as a span.

    skip_after    seconds after the start before an undelivered notice is pushed past the queue
    interrupt_at  the time after which a chat still working is interrupted
    grace         seconds given after the interrupt (or after `interrupt_at` when the key could not be
                  pressed) before acting anyway
    skip_every    seconds between two tries to push a notice that is held (for example while a person
                  types in that chat)"""
    skip_after: float
    interrupt_at: float
    grace: float = 120.0
    skip_every: float = 60.0


@dataclass(frozen=True)
class Seen:
    """What the caller saw, in epoch seconds; None for what has not happened.

    delivered    when the notice reached the chat (the first time)
    turn_ended   when the chat last ended a turn
    skipped      when the last push past the queue was tried
    interrupted  when the interrupt key was pressed
    acted        whether the action already ran
    running      whether the chat is running at all"""
    started: float
    now: float
    delivered: float | None = None
    turn_ended: float | None = None
    skipped: float | None = None
    interrupted: float | None = None
    acted: bool = False
    running: bool = True


@dataclass(frozen=True)
class Step:
    name: str
    why: str


def interrupt_at(started: float, *, after: float | None = None, deadline: float | None = None,
                 margin: float = 120.0, skip_after: float = 120.0, floor: float = 60.0) -> float:
    """The interrupt time for a notice started at `started`: `deadline - margin` when the action has a
    deadline (a limit about to be hit, a restart time), else `started + after`. Never earlier than
    `floor` seconds after the push past the queue, so a chat always has the notice for a moment
    before it can be interrupted."""
    if deadline is None and after is None:
        raise ValueError("an interrupt time needs `after` or `deadline`")
    at = deadline - margin if deadline is not None else started + after
    return max(at, started + skip_after + floor)


def next_step(seen: Seen, timing: Timing, may_interrupt: tuple[bool, str] = (True, "")) -> Step:
    """What to do now. `may_interrupt` is (whether the key may be pressed now, why not): the caller
    says no while a person types in that chat or while it waits on a question for a person."""
    if seen.acted:
        return Step(DONE, "the action already ran")
    if not seen.running:
        return Step(ACT, "the chat is not running, so there is nobody to tell")
    ended = seen.turn_ended
    if seen.interrupted is not None:
        if ended is not None and ended > seen.interrupted:
            return Step(ACT, "its turn ended after the interrupt")
        if seen.now >= seen.interrupted + timing.grace:
            return Step(ACT, f"{int(timing.grace)} s passed after the interrupt")
        return Step(WAIT, "interrupted; waiting for its turn to end")
    if seen.delivered is not None and ended is not None and ended > seen.delivered:
        return Step(ACT, "its turn ended after the notice reached it")
    if seen.now >= timing.interrupt_at:
        ok, why = may_interrupt
        if ok:
            return Step(INTERRUPT, "still working at the interrupt time")
        if seen.now >= timing.interrupt_at + timing.grace:
            return Step(ACT, f"the interrupt could not be pressed ({why or 'refused'}), and "
                             f"{int(timing.grace)} s passed")
        return Step(WAIT, f"the interrupt waits: {why or 'refused'}")
    if seen.delivered is None and seen.now >= seen.started + timing.skip_after:
        if seen.skipped is None or seen.now >= seen.skipped + timing.skip_every:
            return Step(SKIP, f"not delivered {int(seen.now - seen.started)} s after the start")
        return Step(WAIT, "pushed a moment ago; trying again shortly")
    if seen.delivered is None:
        return Step(WAIT, "the notice waits in the queue")
    return Step(WAIT, "the notice reached it; waiting for its turn to end")
