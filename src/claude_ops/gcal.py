"""Add an event to a Google calendar from a job, and check that it landed.

A job that must put one event on a calendar needs less than a client library: a refresh, one insert and
one read. `Calendar` is a `google_api.GoogleSession`, so it uses only the standard library and the two
files a sign-in leaves on disk, and its token needs a scope that can write events.

    from claude_ops.gcal import Calendar
    cal = Calendar("/path/to/tokens.json", "/path/to/credentials.json", account="work")
    event = cal.add("Team lunch", "2031-03-04T13:00", "2031-03-04T14:00", zone="Europe/Paris", location="Cafe")
    print(event["link"], event["minutes"])

`add` reads every event back by its id and raises `NotConfirmed` when the calendar holds something else.
The id comes from the title and the times, so asking twice, or a retry after a failed answer whose insert
did go through, gives the one event, with `already` true. An event a person has since deleted comes back
when it is asked for again.

`last_like` and `last_like_in` find the newest earlier event matching some words, for a job that takes
the calendar or the length from how the same thing went last time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Mapping
from zoneinfo import ZoneInfo

from .google_api import GoogleError, GoogleSession

API = "https://www.googleapis.com/calendar/v3/calendars/"
#: how far back last_like looks when it is not told
LOOK_BACK_DAYS = 5 * 365
#: the most events Google gives on one page
PAGE = 250


class NotConfirmed(Exception):
    """An event was asked for and the calendar, read back by id, holds something else. `event` is what
    it holds and `differs` names the fields that differ."""

    def __init__(self, event: dict, differs: "list[str]"):
        super().__init__(f"event {event.get('id')} reads back with a different {', '.join(differs)}")
        self.event = event
        self.differs = differs


def _is_day(value: str) -> bool:
    return len(value) == 10


def _moment(value: str, zone: "str | None") -> dict:
    """Pure. A start or end in Google's form: a day, or a time with the zone it was given in. Google
    refuses a time written without its seconds, so they are always written. A time with no offset of
    its own needs a zone."""
    if _is_day(value):
        date.fromisoformat(value)
        return {"date": value}
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None and not zone:
        raise ValueError(f"{value} has no offset, so it needs a zone")
    return {"dateTime": parsed.isoformat(timespec="seconds"), **({"timeZone": zone} if zone else {})}


def _instant(moment: dict) -> "date | datetime":
    """Pure apart from the zone database. The moment a start or end names, so two spellings of one time
    compare equal."""
    if moment.get("date"):
        return date.fromisoformat(moment["date"])
    parsed = datetime.fromisoformat(moment["dateTime"].replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(moment["timeZone"]))


def _stamp(moment: dict) -> float:
    """Pure. A moment as seconds, a day counting from its midnight in UTC, for ordering events."""
    when = _instant(moment)
    if isinstance(when, datetime):
        return when.timestamp()
    return datetime(when.year, when.month, when.day, tzinfo=timezone.utc).timestamp()


def event_id(title: str, start: dict, end: dict) -> str:
    """Pure. The id an event with this title and these times gets, in the alphabet Google allows
    (0-9 and a-v), so the same event asked for twice has the same id."""
    digest = hashlib.sha256(json.dumps([title, start, end], sort_keys=True).encode()).digest()
    return base64.b32hexencode(digest).decode().lower().rstrip("=")


def view(event: dict) -> dict:
    """Pure. The fields a caller reads from an event, with `minutes` its length (None for a whole day)."""
    start, end = event.get("start") or {}, event.get("end") or {}
    timed = start.get("dateTime") and end.get("dateTime")
    return {"id": event.get("id"), "title": event.get("summary"), "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"), "location": event.get("location"),
            "link": event.get("htmlLink"), "status": event.get("status"),
            "minutes": round((_instant(end) - _instant(start)).total_seconds() / 60) if timed else None}


def _differs(event: dict, asked: dict) -> "list[str]":
    """Pure. The fields where the calendar's event is not what was asked for."""
    out = []
    if event.get("summary") != asked["summary"]:
        out.append("title")
    if (event.get("location") or "") != (asked.get("location") or ""):
        out.append("location")
    for field in ("start", "end"):
        held = event.get(field) or {}
        if not (held.get("date") or held.get("dateTime")) or _instant(held) != _instant(asked[field]):
            out.append(field)
    return out


class Calendar(GoogleSession):
    """One account's calendars. Each call takes `calendar`, which is that account's main one unless told."""

    def _url(self, calendar: str, path: str = "", params: "list[tuple[str, str]] | None" = None) -> str:
        url = API + urllib.parse.quote(calendar, safe="") + "/events" + path
        return url + ("?" + urllib.parse.urlencode(params) if params else "")

    def _send(self, method: str, url: str, body: "dict | None" = None) -> dict:
        headers = {"Authorization": "Bearer " + self.token()}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        return self._open(urllib.request.Request(url, data=data, headers=headers, method=method))

    def get(self, event: str, *, calendar: str = "primary") -> dict:
        """One event as Google gives it, a deleted one included (its `status` is "cancelled")."""
        return self._send("GET", self._url(calendar, "/" + urllib.parse.quote(event, safe="")))

    def add(self, title: str, start: str, end: str, *, zone: "str | None" = None, location: "str | None" = None,
            calendar: str = "primary") -> dict:
        """Put an event on the calendar and read it back. `start` and `end` are ISO times (with an offset,
        or naming `zone`) or ISO days, where a whole-day event's `end` is the day after its last, as Google
        counts it. Returns `view` of the event with `already` true when it was there before this call.
        Raises ValueError before any request for times that cannot be an event, GoogleError for a refusal,
        and NotConfirmed when the calendar holds something else, for example an event a person has moved."""
        first, last = _moment(start, zone), _moment(end, zone)
        if _is_day(start) != _is_day(end):
            raise ValueError("start and end must both be days or both be times")
        if _instant(first) >= _instant(last):
            raise ValueError(f"{end} is not after {start}")
        asked = {"id": event_id(title, first, last), "summary": title, "start": first, "end": last,
                 **({"location": location} if location else {})}
        already = False
        try:
            self._send("POST", self._url(calendar), asked)
        except GoogleError as e:
            if e.status != 409:
                raise
            already = True
        event = self.get(asked["id"], calendar=calendar)
        if event.get("status") == "cancelled":
            self._send("PUT", self._url(calendar, "/" + asked["id"]), {**asked, "status": "confirmed"})
            event, already = self.get(asked["id"], calendar=calendar), False
        differs = _differs(event, asked)
        if differs:
            raise NotConfirmed(event, differs)
        return {**view(event), "already": already}

    def last_like(self, words: str, *, calendar: str = "primary", before: "datetime | None" = None,
                  days: int = LOOK_BACK_DAYS) -> "dict | None":
        """`view` of the newest event starting before `before` (now, unless told) in the last `days` that
        Google's search finds for `words`, or None. The search also reads locations and descriptions, so
        the words are best kept specific."""
        before = before or datetime.now(timezone.utc)
        newest, page = None, None
        while True:
            params = [("q", words), ("singleEvents", "true"), ("orderBy", "startTime"), ("maxResults", str(PAGE)),
                      ("timeMin", (before - timedelta(days=days)).isoformat()), ("timeMax", before.isoformat())]
            if page:
                params.append(("pageToken", page))
            answer = self._send("GET", self._url(calendar, params=params))
            items = [e for e in answer.get("items") or [] if e.get("start")]
            if items:
                newest = max([*items, *([newest] if newest else [])], key=lambda e: _stamp(e["start"]))
            page = answer.get("nextPageToken")
            if not page:
                return view(newest) if newest else None


def last_like_in(calendars: "Mapping[str, Calendar]", words: str, *, before: "datetime | None" = None,
                 days: int = LOOK_BACK_DAYS) -> "tuple[str, dict] | None":
    """The newest earlier event matching `words` across several accounts, as (name, its view), or None.
    An account that cannot be read raises, because a search that skipped one could name the wrong one."""
    found = []
    for name, calendar in calendars.items():
        event = calendar.last_like(words, before=before, days=days)
        if event:
            found.append((name, event))
    if not found:
        return None
    return max(found, key=lambda pair: _stamp({("date" if _is_day(pair[1]["start"]) else "dateTime"): pair[1]["start"]}))
