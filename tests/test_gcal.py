import io
import json
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import pytest

from claude_ops.gcal import Calendar, NotConfirmed, event_id, last_like_in, view
from claude_ops.gmail import GmailError, Mailbox
from claude_ops.google_api import GoogleError


class Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def refused(code, body=""):
    return urllib.error.HTTPError("https://example.org", code, "refused", {}, io.BytesIO(body.encode()))


class Google:
    """A stand-in for urlopen that holds one calendar's events, as the real API treats them: an id that
    exists is a 409, and a read gives the event with Google's own spelling of its times."""

    def __init__(self, events=None, fail_next=(), pages=None):
        self.events = dict(events or {})
        self.fail_next, self.pages, self.requests = list(fail_next), pages, []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.full_url.startswith("https://oauth2.googleapis.com/token"):
            return Answer({"access_token": "access-1"})
        if self.fail_next:
            raise self.fail_next.pop(0)
        body = json.loads(req.data) if req.data else None
        path = urllib.parse.urlparse(req.full_url).path.split("/events")[1].strip("/")
        if req.get_method() == "POST":
            if body["id"] in self.events:
                raise refused(409, "The requested identifier already exists.")
            self.events[body["id"]] = {**body, "status": "confirmed", "htmlLink": "https://example.org/e"}
            return Answer(self.events[body["id"]])
        if req.get_method() == "PUT":
            self.events[path] = {**self.events[path], **body}
            return Answer(self.events[path])
        if path:
            return Answer(self.events[path]) if path in self.events else (_ for _ in ()).throw(refused(404))
        return Answer(self.pages.pop(0) if self.pages else {"items": list(self.events.values())})


def calendar(tmp_path, google, account=None, tokens=None):
    t = tmp_path / "tokens.json"
    t.write_text(json.dumps(tokens if tokens is not None else {"refresh_token": "refresh-1"}))
    c = tmp_path / "credentials.json"
    c.write_text(json.dumps({"installed": {"client_id": "id-1", "client_secret": "secret-1"}}))
    return Calendar(t, c, account=account, fetch=google, sleep=lambda s: None)


START, END = "2031-03-04T13:00", "2031-03-04T14:00"


def test_add_inserts_reads_back_and_gives_the_length(tmp_path):
    google = Google()
    event = calendar(tmp_path, google).add("Team lunch", START, END, zone="Europe/Paris", location="Cafe")
    assert event["title"] == "Team lunch" and event["location"] == "Cafe"
    assert event["minutes"] == 60 and event["already"] is False and event["link"] == "https://example.org/e"
    methods = [r.get_method() for r in google.requests]
    assert methods == ["POST", "POST", "GET"], "the token, the insert and the read by id"
    sent = json.loads(google.requests[1].data)
    assert sent["start"] == {"dateTime": START + ":00", "timeZone": "Europe/Paris"}, "Google refuses a time with no seconds"
    assert google.requests[1].get_header("Authorization") == "Bearer access-1"


def test_add_twice_gives_the_one_event(tmp_path):
    google = Google()
    cal = calendar(tmp_path, google)
    first = cal.add("Team lunch", START, END, zone="Europe/Paris")
    second = cal.add("Team lunch", START, END, zone="Europe/Paris")
    assert first["id"] == second["id"] and second["already"] is True
    assert len(google.events) == 1


def test_a_retry_after_a_failed_answer_does_not_double_the_event(tmp_path):
    """Google stored the event and then answered 503, so the second try of the insert meets its own id."""
    class Stored(Google):
        def __call__(self, req, timeout=None):
            if req.get_method() == "POST" and req.data and b"summary" in req.data and not self.events:
                body = json.loads(req.data)
                self.events[body["id"]] = {**body, "status": "confirmed"}
                self.requests.append(req)
                raise refused(503)
            return super().__call__(req, timeout)
    google = Stored()
    event = calendar(tmp_path, google).add("Team lunch", START, END, zone="Europe/Paris")
    assert len(google.events) == 1 and event["already"] is True


def test_a_deleted_event_stays_deleted_unless_it_is_revived(tmp_path):
    google = Google()
    cal = calendar(tmp_path, google)
    first = cal.add("Team lunch", START, END, zone="Europe/Paris")
    google.events[first["id"]]["status"] = "cancelled"
    with pytest.raises(NotConfirmed) as e:
        cal.add("Team lunch", START, END, zone="Europe/Paris")
    assert e.value.differs == ["status"] and google.events[first["id"]]["status"] == "cancelled"
    assert "PUT" not in [r.get_method() for r in google.requests], "a person's delete stands"
    again = cal.add("Team lunch", START, END, zone="Europe/Paris", revive=True)
    assert again["id"] == first["id"] and again["already"] is False and again["status"] == "confirmed"


def test_can_write_asks_google_for_the_scopes(tmp_path):
    class Scopes(Google):
        def __init__(self, scope):
            super().__init__()
            self.scope = scope

        def __call__(self, req, timeout=None):
            if req.full_url.startswith("https://oauth2.googleapis.com/tokeninfo"):
                self.requests.append(req)
                return Answer({"scope": self.scope})
            return super().__call__(req, timeout)
    reader = Scopes("https://www.googleapis.com/auth/calendar.readonly openid")
    assert calendar(tmp_path, reader).can_write() is False
    assert "access_token=access-1" in reader.requests[-1].full_url
    writer = Scopes("openid https://www.googleapis.com/auth/calendar.events")
    assert calendar(tmp_path, writer).can_write() is True
    assert calendar(tmp_path, writer).scopes() == {"openid", "https://www.googleapis.com/auth/calendar.events"}


def test_two_spellings_of_one_time_are_the_same_moment(tmp_path):
    """Google answers with an offset where the caller gave a zone, and that is not a difference."""
    class Respelled(Google):
        def __call__(self, req, timeout=None):
            answer = super().__call__(req, timeout)
            if req.get_method() == "GET":
                body = json.loads(answer.body)
                body["start"] = {"dateTime": "2031-03-04T13:00:00+01:00", "timeZone": "Europe/Paris"}
                body["end"] = {"dateTime": "2031-03-04T13:00:00Z", "timeZone": "UTC"}
                return Answer(body)
            return answer
    event = calendar(tmp_path, Respelled()).add("Team lunch", START, END, zone="Europe/Paris")
    assert event["minutes"] == 60


@pytest.mark.parametrize("field, change", [
    ("summary", "Another title"), ("location", "Elsewhere"), ("start", {"dateTime": "2031-03-04T15:00:00+01:00"}),
    ("end", {"dateTime": "2031-03-04T15:00:00+01:00"})])
def test_a_read_back_that_differs_is_not_confirmed(tmp_path, field, change):
    google = Google()
    cal = calendar(tmp_path, google)
    first = cal.add("Team lunch", START, END, zone="Europe/Paris", location="Cafe")
    google.events[first["id"]][field] = change
    with pytest.raises(NotConfirmed) as e:
        cal.add("Team lunch", START, END, zone="Europe/Paris", location="Cafe")
    assert e.value.differs == [{"summary": "title"}.get(field, field)]
    assert e.value.event["id"] == first["id"]


def test_a_whole_day_event(tmp_path):
    event = calendar(tmp_path, Google()).add("Conference", "2031-03-04", "2031-03-06")
    assert event["start"] == "2031-03-04" and event["minutes"] is None


@pytest.mark.parametrize("start, end, zone, says", [
    (START, END, None, "needs a zone"),
    ("2031-03-04", END, "Europe/Paris", "both"),
    (END, START, "Europe/Paris", "not after"),
    (START, START, "Europe/Paris", "not after")])
def test_times_that_cannot_be_an_event_ask_nothing(tmp_path, start, end, zone, says):
    google = Google()
    with pytest.raises(ValueError, match=says):
        calendar(tmp_path, google).add("Team lunch", start, end, zone=zone)
    assert google.requests == []


def test_a_refusal_is_not_read_as_an_event_that_exists(tmp_path):
    google = Google(fail_next=[refused(403, "insufficient scope")])
    with pytest.raises(GoogleError) as e:
        calendar(tmp_path, google).add("Team lunch", START, END, zone="Europe/Paris")
    assert e.value.status == 403 and google.events == {}


def test_the_account_picks_an_entry_of_a_shared_token_file(tmp_path):
    tokens = {"work": {"refresh_token": "refresh-work"}, "home": {"refresh_token": "refresh-home"}}
    google = Google()
    calendar(tmp_path, google, account="home", tokens=tokens).add("Team lunch", START, END, zone="Europe/Paris")
    assert b"refresh-home" in google.requests[0].data
    with pytest.raises(ValueError, match="no account"):
        calendar(tmp_path, Google(), account="other", tokens=tokens)


def test_the_id_uses_only_what_google_allows():
    made = event_id("Team lunch ✓", {"date": "2031-03-04"}, {"date": "2031-03-05"})
    assert set(made) <= set("0123456789abcdefghijklmnopqrstuv") and 5 <= len(made) <= 1024
    assert made != event_id("Team lunch", {"date": "2031-03-04"}, {"date": "2031-03-05"})


def item(title, start, end):
    return {"id": title, "summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}}


def test_last_like_walks_every_page_for_the_newest(tmp_path):
    old = item("old", "2030-01-01T10:00:00+00:00", "2030-01-01T11:00:00+00:00")
    new = item("new", "2030-06-01T10:00:00+00:00", "2030-06-01T13:00:00+00:00")
    google = Google(pages=[{"items": [old], "nextPageToken": "next-1"}, {"items": [new]}])
    before = datetime(2031, 1, 1, tzinfo=timezone.utc)
    got = calendar(tmp_path, google).last_like("quiz", before=before)
    assert got["title"] == "new" and got["minutes"] == 180
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(google.requests[1].full_url).query))
    assert query["q"] == "quiz" and query["singleEvents"] == "true" and query["timeMax"].startswith("2031-01-01")
    assert "pageToken=next-1" in google.requests[2].full_url


def test_last_like_finds_nothing(tmp_path):
    assert calendar(tmp_path, Google(pages=[{"items": []}])).last_like("quiz") is None


def test_last_like_in_names_the_account_holding_the_newest(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    older = Google(pages=[{"items": [item("x", "2030-01-01T10:00:00+00:00", "2030-01-01T11:00:00+00:00")]}])
    newer = Google(pages=[{"items": [item("y", "2030-02-01T10:00:00+02:00", "2030-02-01T11:00:00+02:00")]}])
    none = Google(pages=[{"items": []}])
    got = last_like_in({"work": calendar(tmp_path / "a", older), "home": calendar(tmp_path / "b", newer),
                        "club": calendar(tmp_path / "c", none)}, "lunch")
    assert got[0] == "home" and got[1]["title"] == "y"
    assert last_like_in({"club": calendar(tmp_path / "c", Google(pages=[{"items": []}]))}, "lunch") is None


def test_last_like_in_says_when_an_account_cannot_be_read(tmp_path):
    with pytest.raises(GoogleError):
        last_like_in({"work": calendar(tmp_path, Google(fail_next=[refused(403)]))}, "lunch")


def test_view_of_an_event_with_no_times():
    assert view({"id": "x"})["minutes"] is None


def test_the_old_gmail_names_still_work(tmp_path):
    assert GmailError is GoogleError and issubclass(Mailbox, object)
    google = Google(fail_next=[refused(401)])
    t = tmp_path / "token.json"
    t.write_text(json.dumps({"refresh_token": "r", "client_id": "i", "client_secret": "s"}))
    box = Mailbox(t, fetch=google, sleep=lambda s: None)
    with pytest.raises(GmailError) as e:
        box.search("from:someone@example.org")
    assert e.value.needs_sign_in
