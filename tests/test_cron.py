from datetime import datetime, timedelta, timezone

import pytest

from claude_ops import cron

UTC = timezone.utc
ZONE = "Europe/Berlin"     # +01:00 in winter, +02:00 from the last Sunday of March


def at(*a):
    return datetime(*a, tzinfo=UTC)


def test_a_weekday_time_is_due_once_after_it_passes():
    # Saturday 2026-03-21 10:00 in Berlin is 09:00 UTC
    assert cron.due("0 10 * * sat", None, at(2026, 3, 21, 8, 59), zone=ZONE).day == 14   # the week before
    fired = cron.due("0 10 * * sat", at(2026, 3, 14, 9, 0), at(2026, 3, 21, 9, 0), zone=ZONE)
    assert fired is not None and fired.astimezone(UTC) == at(2026, 3, 21, 9, 0)
    assert cron.due("0 10 * * sat", fired, at(2026, 3, 21, 9, 1), zone=ZONE) is None
    assert cron.due("0 10 * * sat", fired, at(2026, 3, 27, 23, 0), zone=ZONE) is None


def test_the_wall_time_holds_across_the_clock_change():
    # after the last Sunday of March the same 10:00 is 08:00 UTC, an hour earlier
    last = at(2026, 3, 28, 9, 0)        # the Saturday before the change, still +01:00
    assert cron.due("0 10 * * sat", last, at(2026, 4, 4, 7, 59), zone=ZONE) is None
    fired = cron.due("0 10 * * sat", last, at(2026, 4, 4, 8, 0), zone=ZONE)
    assert fired.astimezone(UTC) == at(2026, 4, 4, 8, 0) and fired.hour == 10


def test_a_skipped_wall_time_fires_after_the_jump_and_a_repeated_one_once():
    # 2026-03-29 02:30 does not exist in Berlin; it fires at 01:30 UTC (03:30 local)
    fired = cron.previous("30 2 * * *", at(2026, 3, 29, 2, 0), zone=ZONE)
    assert fired.astimezone(UTC) == at(2026, 3, 29, 1, 30)
    # 2026-10-25 02:30 happens twice; the first is 00:30 UTC, and the second does not fire again
    first = cron.previous("30 2 * * *", at(2026, 10, 25, 0, 45), zone=ZONE)
    assert first.astimezone(UTC) == at(2026, 10, 25, 0, 30)
    assert cron.due("30 2 * * *", first, at(2026, 10, 25, 1, 45), zone=ZONE) is None


def test_fields_lists_ranges_steps_and_names():
    c = cron.parse("*/15 9-17 * jan,jul mon-fri")
    assert c.minutes == {0, 15, 30, 45} and c.hours == set(range(9, 18))
    assert c.months == {1, 7} and c.weekdays == {1, 2, 3, 4, 5}
    assert cron.parse("0 0 * * 7").weekdays == {0} == cron.parse("0 0 * * sun").weekdays
    assert cron.parse("5/20 * * * *").minutes == {5, 25, 45}
    assert cron.parse("@daily") == cron.parse("0 0 * * *")


def test_day_of_month_and_weekday_either_one_when_both_are_given():
    c = cron.parse("30 9 5 * mon")
    assert c.day_matches(datetime(2026, 11, 5).date())          # the 5th, a Thursday
    assert c.day_matches(datetime(2026, 11, 2).date())          # a Monday
    assert not c.day_matches(datetime(2026, 11, 3).date())
    assert cron.previous("30 9 5 * *", at(2026, 11, 4, 12, 0), zone=ZONE).date().isoformat() == "2026-10-05"


def test_cron_tz_prefix_names_the_zone():
    fired = cron.previous("CRON_TZ=Europe/Berlin 0 10 * * sat", at(2026, 3, 21, 9, 30))
    assert fired.astimezone(UTC) == at(2026, 3, 21, 9, 0)
    assert cron.previous("0 10 * * sat", at(2026, 3, 21, 9, 30)).astimezone(UTC) == at(2026, 3, 14, 10, 0)


@pytest.mark.parametrize("bad", ["", "0 10 * *", "60 * * * *", "0 24 * * *", "0 0 0 * *",
                                 "0 0 * 13 *", "0 0 * * 8", "*/0 * * * *", "*/x * * * *",
                                 "0 0 * * someday", "5-1 * * * *"])
def test_a_schedule_that_is_not_one_is_refused(bad):
    with pytest.raises(ValueError):
        cron.parse(bad)


def test_an_unknown_zone_or_a_naive_time_is_refused():
    with pytest.raises(ValueError, match="no time zone"):
        cron.parse("0 0 * * *", zone="Mars/Olympus")
    with pytest.raises(ValueError, match="time zone"):
        cron.previous("0 0 * * *", datetime(2026, 1, 1))


def test_a_date_that_never_comes_has_no_previous():
    assert cron.previous("0 0 31 2 *", at(2026, 1, 1)) is None
    assert cron.previous("0 0 29 2 *", at(2026, 1, 1)).year == 2024


def test_every_minute_is_the_minute_itself():
    now = at(2026, 5, 1, 12, 34, 56)
    assert cron.previous("* * * * *", now) == datetime(2026, 5, 1, 12, 34, tzinfo=cron.parse("* * * * *").zone)
    assert cron.due("* * * * *", now - timedelta(seconds=30), now) is None
