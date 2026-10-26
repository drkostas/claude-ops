"""ladder: notice, push past the queue, interrupt, act, and stop as soon as the chat ends its turn."""
from __future__ import annotations

import pytest

from claude_ops import ladder as L

T0 = 1_000_000.0
TIMING = L.Timing(skip_after=120, interrupt_at=T0 + 600, grace=120, skip_every=60)


def at(now, may=(True, ""), **kw):
    return L.next_step(L.Seen(started=T0, now=T0 + now, **kw), TIMING, may)


def test_a_chat_that_reads_the_notice_and_ends_its_turn_is_acted_on_with_no_escalation():
    assert at(30).name == L.WAIT
    assert at(60, delivered=T0 + 40).name == L.WAIT
    s = at(90, delivered=T0 + 40, turn_ended=T0 + 80)
    assert s.name == L.ACT and "after the notice" in s.why


def test_an_earlier_turn_end_does_not_count():
    assert at(90, delivered=T0 + 40, turn_ended=T0 + 10).name == L.WAIT


def test_a_notice_never_delivered_is_pushed_at_skip_after_and_retried_every_skip_every():
    assert at(119).name == L.WAIT
    assert at(120).name == L.SKIP
    assert at(150, skipped=T0 + 120).name == L.WAIT
    assert at(180, skipped=T0 + 120).name == L.SKIP


def test_a_chat_working_past_the_interrupt_time_is_interrupted_once_then_acted_on():
    assert at(600, delivered=T0 + 40).name == L.INTERRUPT
    assert at(650, delivered=T0 + 40, interrupted=T0 + 600).name == L.WAIT
    assert at(660, delivered=T0 + 40, interrupted=T0 + 600, turn_ended=T0 + 655).name == L.ACT
    late = at(720, delivered=T0 + 40, interrupted=T0 + 600)
    assert late.name == L.ACT and "after the interrupt" in late.why


def test_no_interrupt_while_a_person_types_and_the_action_runs_after_the_grace():
    typing = (False, "a person is typing in it")
    s = at(600, typing, delivered=T0 + 40)
    assert s.name == L.WAIT and "typing" in s.why
    assert at(719, typing, delivered=T0 + 40).name == L.WAIT
    s = at(720, typing, delivered=T0 + 40)
    assert s.name == L.ACT and "could not be pressed" in s.why


def test_an_undelivered_notice_is_interrupted_at_the_interrupt_time_too():
    assert at(600, skipped=T0 + 590).name == L.INTERRUPT


def test_a_chat_that_is_not_running_is_acted_on_at_once_and_a_done_ladder_stays_done():
    assert at(5, running=False).name == L.ACT
    assert at(5, acted=True).name == L.DONE


def test_interrupt_at_takes_a_deadline_less_its_margin_but_never_before_the_floor():
    assert L.interrupt_at(T0, after=600) == T0 + 600
    assert L.interrupt_at(T0, deadline=T0 + 900, margin=120) == T0 + 780
    assert L.interrupt_at(T0, deadline=T0 + 100, margin=120) == T0 + 180
    with pytest.raises(ValueError):
        L.interrupt_at(T0)
