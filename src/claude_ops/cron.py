"""Cron schedules read in a named time zone, for a loop that decides itself what is due.

A scheduler that runs every minute or so can ask `due(expr, last, now)` instead of keeping a
launchd plist or a crontab line per job: it answers the newest time the schedule names that falls
after the last run and at or before now, or None. The five fields are the usual ones (minute, hour,
day of month, month, day of week), with `*`, lists, ranges, `/` steps, and the names jan..dec and
sun..sat. A day of week of 0 and of 7 are both Sunday, and when both day fields are restricted a
day matching either one counts, as in cron itself. `@hourly`, `@daily`, `@weekly`, `@monthly` and
`@yearly` are the usual shorthands.

The schedule is read in a time zone, `zone` (UTC when not given), or the one a `CRON_TZ=<zone>`
prefix names. A wall time a clock change skips (02:30 on a spring-forward night in Central Europe)
fires at the instant after the jump; a wall time a clock change repeats fires once, the first time.

    >>> from datetime import datetime, timezone
    >>> due("0 10 * * sat", None, datetime(2026, 3, 28, 9, 30, tzinfo=timezone.utc), zone="Europe/Berlin")
    datetime.datetime(2026, 3, 28, 10, 0, tzinfo=zoneinfo.ZoneInfo(key='Europe/Berlin'))
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SHORTHANDS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
              "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
              "@annually": "0 0 1 1 *"}
MONTHS = {n: i for i, n in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
WEEKDAYS = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}
#: how far back `previous` looks: a schedule for 29 February holds once in four years at most
LOOKBACK_DAYS = 366 * 8


@dataclass(frozen=True)
class Cron:
    """A parsed schedule: the values each field allows, and the zone it is read in."""
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]        # 0 is Sunday
    any_day: bool                   # the day-of-month field was `*`
    any_weekday: bool               # the day-of-week field was `*`
    zone: ZoneInfo

    def day_matches(self, d: dt.date) -> bool:
        if d.month not in self.months:
            return False
        by_day = d.day in self.days
        by_weekday = (d.isoweekday() % 7) in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return by_weekday
        if self.any_weekday:
            return by_day
        return by_day or by_weekday


def _value(text: str, names: dict[str, int] | None) -> int:
    key = text.strip().lower()
    if names and key in names:
        return names[key]
    if not key.isdigit():
        raise ValueError(f"{text!r} is not a number{' or a name' if names else ''}")
    return int(key)


def _field(text: str, low: int, high: int, names: dict[str, int] | None = None) -> frozenset[int]:
    out: set[int] = set()
    for part in text.split(","):
        rng, slash, step_s = part.partition("/")
        if slash and not step_s.isdigit():
            raise ValueError(f"a step is a number, not {part!r}")
        step = int(step_s) if slash else 1
        if step < 1:
            raise ValueError(f"a step must be 1 or more, not {part!r}")
        if rng == "*":
            a, b = low, high
        else:
            first, dash, last = rng.partition("-")
            a = _value(first, names)
            b = _value(last, names) if dash else (high if slash else a)
        if not (low <= a <= high and low <= b <= high) or a > b:
            raise ValueError(f"{part!r} is outside {low}-{high}")
        out.update(range(a, b + 1, step))
    return frozenset(out)


def parse(expr: str, zone: str | None = None) -> Cron:
    """The schedule `expr` read in `zone` (or its CRON_TZ= prefix, or UTC). ValueError when it is
    not one: the wrong number of fields, a value out of range, a zone nobody knows."""
    text = " ".join((expr or "").split())
    if text.upper().startswith("CRON_TZ="):
        prefix, _, text = text.partition(" ")
        zone = prefix.split("=", 1)[1]
    text = SHORTHANDS.get(text.lower(), text)
    fields = text.split(" ")
    if len(fields) != 5:
        raise ValueError(f"a cron schedule has five fields (minute hour day month weekday), not {expr!r}")
    try:
        tz = ZoneInfo(zone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"no time zone called {zone!r}") from None
    minute, hour, day, month, weekday = fields
    return Cron(_field(minute, 0, 59), _field(hour, 0, 23), _field(day, 1, 31),
                _field(month, 1, 12, MONTHS), frozenset(w % 7 for w in _field(weekday, 0, 7, WEEKDAYS)),
                day == "*", weekday == "*", tz)


def previous(expr: str | Cron, at: dt.datetime, zone: str | None = None) -> dt.datetime | None:
    """The newest time the schedule names at or before `at` (an aware datetime), in the schedule's
    zone, or None when there is none in LOOKBACK_DAYS."""
    cron = expr if isinstance(expr, Cron) else parse(expr, zone)
    if at.tzinfo is None:
        raise ValueError("`at` needs its time zone")
    limit = at.astimezone(dt.timezone.utc)
    hours = sorted(cron.hours, reverse=True)
    minutes = sorted(cron.minutes, reverse=True)
    day = at.astimezone(cron.zone).date()
    for _ in range(LOOKBACK_DAYS):
        if cron.day_matches(day):
            for h in hours:
                for m in minutes:
                    # fold=0: a skipped wall time lands after the jump, a repeated one on its first pass
                    when = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=cron.zone)
                    if when.astimezone(dt.timezone.utc) <= limit:
                        return when
        day -= dt.timedelta(days=1)
    return None


def due(expr: str | Cron, last: dt.datetime | None, now: dt.datetime,
        zone: str | None = None) -> dt.datetime | None:
    """The newest time the schedule names after `last` and at or before `now`, or None when it is
    not due. With no `last` (never run), the newest time at or before `now`: a caller that must not
    fire for a time before it existed passes the time it was created as `last`."""
    when = previous(expr, now, zone)
    if when is None or (last is not None and when <= last):
        return None
    return when
