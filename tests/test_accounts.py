import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import urllib.error
from email.message import Message

from claude_ops import accounts as ac

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
H = dt.timedelta(hours=1)


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS keychain")
def test_item_round_trip_never_puts_the_secret_on_a_command_line():
    service = "claude-ops-test-accounts"
    seen = []

    def run(argv, **kw):
        seen.append(list(argv))
        return subprocess.run(argv, **kw)

    secret_a, secret_r = "a" * 20, "r" * 20
    data = {"claudeAiOauth": {"accessToken": secret_a, "refreshToken": secret_r}}
    try:
        assert ac.write_item(service, "tester", data, run=run)
        assert ac.read_item(service, "tester", run=run) == data
        assert not any(secret_a in " ".join(a) or secret_r in " ".join(a) for a in seen)
    finally:
        subprocess.run(["security", "delete-generic-password", "-s", service, "-a", "tester"],
                       capture_output=True)
    assert ac.read_item(service, "tester") is None


def fake(answer):
    calls = []

    class Answer:
        def read(self):
            return json.dumps(answer).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fetch(req, timeout=15):
        calls.append(req)
        return Answer()
    return fetch, calls


def test_usage_reads_the_two_limits():
    fetch, calls = fake({"five_hour": {"utilization": 21.0, "resets_at": "2026-10-18T14:09:59+00:00"},
                         "seven_day": {"utilization": 6.0, "resets_at": "2026-10-25T06:59:59+00:00"}})
    u = ac.usage("tok", fetch=fetch)
    assert u["session"] == (21.0, dt.datetime(2026, 10, 18, 14, 9, 59, tzinfo=UTC))
    assert u["weekly"][0] == 6.0
    assert calls[0].get_header("Authorization") == "Bearer tok"
    assert calls[0].get_header("User-agent") == ac.USER_AGENT


def test_profile_names_the_account():
    fetch, _ = fake({"account": {"email": "a@example.org", "uuid": "u1"}, "organization": {"uuid": "o1"}})
    assert ac.profile("tok", fetch=fetch) == {"email": "a@example.org", "uuid": "u1", "org": "o1"}


def test_renew_keeps_the_rest_and_dates_the_new_token():
    fetch, calls = fake({"access_token": "new-a", "refresh_token": "new-r", "expires_in": 28800})
    old = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 0,
                             "scopes": ["user:inference", "user:profile"], "subscriptionType": "max"},
           "mcpOAuth": {}}
    new = ac.renew(old, fetch=fetch, now=NOW)
    o = new["claudeAiOauth"]
    assert (o["accessToken"], o["refreshToken"], o["subscriptionType"]) == ("new-a", "new-r", "max")
    assert o["expiresAt"] == int((NOW.timestamp() + 28800) * 1000)
    assert new["mcpOAuth"] == {}
    body = json.loads(calls[0].data)
    assert body == {"grant_type": "refresh_token", "refresh_token": "r", "client_id": ac.CLIENT_ID,
                    "scope": "user:inference user:profile"}
    assert ac.expired(old, NOW) and not ac.expired(new, NOW)
    # Cloudflare refuses Python-urllib's own user agent at the token endpoint (403, error code 1010)
    assert calls[0].get_header("User-agent") == ac.USER_AGENT and "urllib" not in ac.USER_AGENT


def test_usable_at():
    assert ac.usable_at({"session": (50.0, NOW + H), "weekly": (10.0, NOW + 100 * H)}, NOW) == NOW
    assert ac.usable_at({"session": (98.0, NOW + 2 * H), "weekly": (10.0, NOW + 100 * H)}, NOW) == NOW + 2 * H
    assert ac.usable_at({"session": (99.0, NOW + 2 * H), "weekly": (100.0, NOW + 100 * H)}, NOW) == NOW + 100 * H
    assert ac.usable_at(None, NOW) is None


def test_choose_switches_only_to_an_account_usable_sooner():
    weekly_gone = {"session": (0.0, NOW + H), "weekly": (100.0, NOW + 72 * H)}
    session_full = {"session": (98.5, NOW + 2 * H), "weekly": (40.0, NOW + 100 * H)}
    assert ac.choose("north", {"north": session_full, "south": weekly_gone}, NOW) is None
    weekly_soon = {"session": (0.0, NOW + H), "weekly": (100.0, NOW + H)}
    assert ac.choose("north", {"north": session_full, "south": weekly_soon}, NOW) is None
    assert ac.choose("north", {"north": session_full, "south": weekly_soon}, NOW + H) == "south"
    # decide, the newer rule, waits out a session block that ends within the hour instead
    assert ac.decide("north", {"north": session_full, "south": weekly_soon}, NOW + H).action == "pause"


def test_choose_on_the_error_before_the_threshold_and_never_on_unknown():
    fine = {"session": (60.0, NOW + 3 * H), "weekly": (40.0, NOW + 100 * H)}
    assert ac.choose("a", {"a": fine, "b": fine}, NOW, hit=True) == "b"
    assert ac.choose("a", {"a": fine, "b": fine}, NOW) is None
    assert ac.choose("a", {"a": fine, "b": None}, NOW, hit=True) is None
    assert ac.choose("a", {"a": None, "b": fine}, NOW) is None


def test_limit_hit(tmp_path):
    t = tmp_path / "c.jsonl"
    rec = {"type": "assistant", "isApiErrorMessage": True, "timestamp": "2026-10-18T10:00:00Z",
           "message": {"content": [{"type": "text", "text": "You've hit your session limit · resets 2:50pm"}]}}
    t.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n" + json.dumps(rec) + "\n")
    assert ac.limit_hit(t)["kind"] == "session"
    rec["message"]["content"][0]["text"] = "You've hit your limit · resets 6pm"
    t.write_text(json.dumps(rec) + "\n")
    assert ac.limit_hit(t)["kind"] == "limit"
    t.write_text(t.read_text() + json.dumps({"type": "user", "message": {"content": "go on"}}) + "\n")
    assert ac.limit_hit(t) is None
    assert ac.limit_hit(tmp_path / "missing.jsonl") is None


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS keychain")
def test_a_login_as_long_as_a_real_one_is_saved_whole_and_a_longer_one_is_refused():
    service = "claude-ops-test-accounts-long"
    data = {"claudeAiOauth": {"accessToken": "x" * 150, "refreshToken": "y" * 150, "scopes": ['a"b', "c\\d"]},
            "mcpOAuth": {f"server{i}": {"accessToken": "t" * 40, "url": "https://e.example/p?q=1&r=2"}
                         for i in range(18)}}
    assert len(json.dumps(data)) > 2000
    try:
        assert ac.write_item(service, "tester", data)
        assert ac.read_item(service, "tester") == data
        too_long = {"blob": "z" * ac.STDIN_LIMIT}
        with pytest.raises(ValueError):
            ac.write_item(service, "tester", too_long)
        assert ac.read_item(service, "tester") == data      # the refused write changed nothing
    finally:
        subprocess.run(["security", "delete-generic-password", "-s", service, "-a", "tester"],
                       capture_output=True)


def test_login_for_finds_the_saved_login_of_an_account():
    import subprocess
    logins = {"live": {"claudeAiOauth": {"accessToken": "t-live"}},
              "saved-b": {"claudeAiOauth": {"accessToken": "t-b"}},
              "stale": {"claudeAiOauth": {"accessToken": "t-old"}}}

    def run(argv, **kw):
        login = logins.get(argv[argv.index("-s") + 1])
        return subprocess.CompletedProcess(argv, 0 if login else 44, json.dumps(login) if login else "", "")

    uuids = {"t-live": "uuid-a", "t-b": "uuid-b"}

    class Answer:
        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fetch(req, timeout=15):
        token = req.get_header("Authorization").split()[-1]
        if token not in uuids:
            raise OSError("401")
        return Answer({"account": {"email": "x@example.org", "uuid": uuids[token]}, "organization": {}})

    items = [("missing", "u"), ("stale", "u"), ("live", "u"), ("saved-b", "u")]
    assert ac.login_for("uuid-b", items, run=run, fetch=fetch) == logins["saved-b"]
    assert ac.login_for("uuid-a", items, run=run, fetch=fetch) == logins["live"]
    assert ac.login_for("uuid-z", items, run=run, fetch=fetch) is None


def test_read_item_tells_a_missing_item_from_a_keychain_that_did_not_answer():
    def missing(argv, **kw):
        return subprocess.CompletedProcess(argv, ac.NOT_FOUND, "", "The specified item could not be found in the keychain.")

    def locked(argv, **kw):
        return subprocess.CompletedProcess(argv, 36, "", "User interaction is not allowed.")
    assert ac.read_item("svc", "tester", run=missing) is None
    assert ac.read_item("svc", "tester", run=missing, strict=True) is None
    assert ac.read_item("svc", "tester", run=locked) is None
    with pytest.raises(ac.Unreadable):
        ac.read_item("svc", "tester", run=locked, strict=True)


# --- the account named in a Claude Code config file ------------------------------------------

BLOCK_A = {"accountUuid": "a-1", "emailAddress": "a@example.org", "organizationUuid": "o-1",
           "displayName": "A", "profileFetchedAt": 1}
BLOCK_B = {"accountUuid": "b-2", "emailAddress": "b@example.org", "organizationUuid": "o-2"}


def config_file(tmp_path, block=BLOCK_A, extra=None, newline=True):
    f = tmp_path / "claude.json"
    data = {"numStartups": 5, "mcpServers": {"x": {"command": "x"}}, **(extra or {})}
    if block is not None:
        data["oauthAccount"] = block
    f.write_text(json.dumps(data, indent=2) + ("\n" if newline else ""))
    return f


def test_read_account_is_the_block_or_none(tmp_path):
    f = config_file(tmp_path)
    assert ac.read_account(f) == BLOCK_A
    assert ac.read_account(f) is not ac.read_account(f)              # a copy each time
    assert ac.read_account(config_file(tmp_path, block=None)) is None
    assert ac.read_account(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert ac.read_account(tmp_path / "bad.json") is None
    (tmp_path / "list.json").write_text("[1, 2]")
    assert ac.read_account(tmp_path / "list.json") is None
    assert ac.account_email(BLOCK_A) == "a@example.org" and ac.account_email(None) is None


def test_account_block_from_a_profile():
    p = {"email": "b@example.org", "uuid": "b-2", "org": "o-2"}
    assert ac.account_block(p) == BLOCK_B
    assert ac.account_block({}) == {"accountUuid": None, "emailAddress": None, "organizationUuid": None}


def test_write_account_replaces_only_the_block_and_keeps_the_rest(tmp_path):
    f = config_file(tmp_path, extra={"projects": {"/path/to": {"allowed": True}}})
    os.chmod(f, 0o600)
    before = json.loads(f.read_text())
    backup = tmp_path / "claude.json.backup"
    out = ac.write_account(f, BLOCK_B, backup=backup)
    assert out["changed"] is True and out["backup"] == str(backup)
    after = json.loads(f.read_text())
    assert after["oauthAccount"] == BLOCK_B
    assert {k: v for k, v in after.items() if k != "oauthAccount"} == \
           {k: v for k, v in before.items() if k != "oauthAccount"}
    assert json.loads(backup.read_text()) == before                     # the copy is the file as it was
    assert (f.stat().st_mode & 0o777) == 0o600                          # the mode survives the rename
    assert f.read_text().endswith("}\n")                                # and so does the final newline
    assert ac.read_account(f) == BLOCK_B
    assert out["mtime_ns"] == f.stat().st_mtime_ns
    assert ac.changed_since(f, out["mtime_ns"]) is False
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_write_account_writes_nothing_when_the_block_is_already_there(tmp_path):
    f = config_file(tmp_path, newline=False)
    stat = f.stat()
    backup = tmp_path / "claude.json.backup"
    out = ac.write_account(f, dict(BLOCK_A), backup=backup)
    assert out == {"changed": False, "backup": None, "mtime_ns": stat.st_mtime_ns}
    assert f.stat().st_mtime_ns == stat.st_mtime_ns and not backup.exists()


def test_write_account_starts_over_when_another_program_wrote_meanwhile(tmp_path, monkeypatch):
    f = config_file(tmp_path)
    real = ac._read_bytes
    calls = []

    def other_writer_between(path):
        calls.append(len(calls))
        if len(calls) == 2:                 # the check before the first rename: Claude Code wrote
            data = json.loads(real(path))
            data["numStartups"] = 6
            data["oauthAccount"] = BLOCK_A
            Path(path).write_text(json.dumps(data, indent=2) + "\n")
        return real(path)

    monkeypatch.setattr(ac, "_read_bytes", other_writer_between)
    out = ac.write_account(f, BLOCK_B)
    after = json.loads(f.read_text())
    assert out["changed"] is True
    assert after["oauthAccount"] == BLOCK_B and after["numStartups"] == 6   # built on what it wrote
    assert len(calls) == 4                                                 # two tries, two reads each


def test_write_account_gives_up_after_its_tries_and_writes_nothing(tmp_path, monkeypatch):
    f = config_file(tmp_path)
    real = ac._read_bytes
    n = {"reads": 0}

    def always_changing(path):
        n["reads"] += 1
        if n["reads"] % 2 == 0:
            data = json.loads(real(path))
            data["numStartups"] = n["reads"]
            Path(path).write_text(json.dumps(data, indent=2) + "\n")
        return real(path)

    monkeypatch.setattr(ac, "_read_bytes", always_changing)
    with pytest.raises(ac.ConfigChanged):
        ac.write_account(f, BLOCK_B, tries=2)
    assert json.loads(f.read_text())["oauthAccount"] == BLOCK_A
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_write_account_refuses_a_file_that_is_not_a_config(tmp_path):
    with pytest.raises(ValueError):
        ac.write_account(tmp_path / "missing.json", BLOCK_B)
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(ValueError):
        ac.write_account(tmp_path / "bad.json", BLOCK_B)
    (tmp_path / "list.json").write_text("[]")
    with pytest.raises(ValueError):
        ac.write_account(tmp_path / "list.json", BLOCK_B)


def test_changed_since_sees_the_next_rewrite(tmp_path):
    f = config_file(tmp_path)
    out = ac.write_account(f, BLOCK_B)
    assert ac.changed_since(f, out["mtime_ns"]) is False
    os.utime(f, ns=(out["mtime_ns"] + 1_000_000, out["mtime_ns"] + 1_000_000))
    assert ac.changed_since(f, out["mtime_ns"]) is True
    assert ac.changed_since(tmp_path / "missing.json", 0) is None


# --- a 429 says how long to wait ------------------------------------------------------------

def refusing(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    calls = []

    def fetch(req, timeout=15):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, code, "refused", headers, None)
    return fetch, calls


def test_a_429_is_rate_limited_with_its_retry_after():
    fetch, calls = refusing(429, "3448")
    with pytest.raises(ac.RateLimited) as got:
        ac.usage("tok", fetch=fetch)
    assert got.value.retry_after_s == 3448 and got.value.url == ac.USAGE_URL
    assert got.value.until(NOW) == NOW + dt.timedelta(seconds=3448)
    assert "429" in str(got.value) and "3448" in str(got.value)
    assert len(calls) == 1                                  # asked once, never again by itself
    with pytest.raises(ac.RateLimited) as got:
        ac.profile("tok", fetch=refusing(429, "60")[0])
    assert got.value.retry_after_s == 60 and got.value.url == ac.PROFILE_URL


def test_a_429_without_retry_after_is_still_rate_limited_but_says_no_time():
    with pytest.raises(ac.RateLimited) as got:
        ac.usage("tok", fetch=refusing(429)[0])
    assert got.value.retry_after_s is None and got.value.until(NOW) is None
    assert "no Retry-After" in str(got.value)


def test_other_http_errors_come_through_as_they_were():
    with pytest.raises(urllib.error.HTTPError) as got:
        ac.usage("tok", fetch=refusing(401)[0])
    assert got.value.code == 401
    with pytest.raises(urllib.error.HTTPError):
        ac.profile("tok", fetch=refusing(500, "10")[0])


def test_retry_after_reads_seconds_or_an_http_date():
    assert ac.retry_after_seconds("120") == 120
    assert ac.retry_after_seconds(" 7 ") == 7
    assert ac.retry_after_seconds("Sun, 18 Oct 2026 12:30:00 GMT", now=NOW) == 1800
    assert ac.retry_after_seconds("Sun, 18 Oct 2026 11:00:00 GMT", now=NOW) == 0     # a date in the past
    assert ac.retry_after_seconds(None) is None and ac.retry_after_seconds("") is None
    assert ac.retry_after_seconds("soon") is None


def test_decide_waits_out_a_short_session_block_instead_of_spending_the_other_weekly():
    # the readings of one afternoon: in use at 98% of its session limit, 43 minutes from its reset,
    # with weekly room; the other at 6% session and 97% weekly, which a switch would spend for days
    north = {"session": (98.0, NOW + dt.timedelta(minutes=43)), "weekly": (48.0, NOW + 60 * H)}
    south = {"session": (6.0, NOW + 4 * H), "weekly": (97.0, NOW + 70 * H)}
    d = ac.decide("north", {"north": north, "south": south}, NOW)
    assert (d.action, d.account, d.until) == ("pause", "north", NOW + dt.timedelta(minutes=43))
    assert "within the hour" in d.reason
    assert ac.choose("north", {"north": north, "south": south}, NOW) == "south"   # choose keeps the old rule


def test_decide_switches_to_room_and_prefers_the_most_weekly_room():
    north = {"session": (98.0, NOW + 3 * H), "weekly": (48.0, NOW + 60 * H)}
    south = {"session": (6.0, NOW + 4 * H), "weekly": (40.0, NOW + 70 * H)}
    east = {"session": (10.0, NOW + 4 * H), "weekly": (20.0, NOW + 70 * H)}
    near = {"session": (6.0, NOW + 4 * H), "weekly": (92.0, NOW + 70 * H)}
    d = ac.decide("north", {"north": north, "south": south, "east": east}, NOW)
    assert (d.action, d.account) == ("switch", "east") and "has room" in d.reason
    assert ac.decide("north", {"north": north, "near": near, "south": south}, NOW).account == "south"
    assert ac.choose("north", {"north": north, "south": south}, NOW) == "south"


def test_decide_waits_for_the_soonest_weekly_reset_when_every_account_is_near_it():
    north = {"session": (30.0, NOW + 3 * H), "weekly": (98.0, NOW + 72 * H)}
    south = {"session": (6.0, NOW + 4 * H), "weekly": (96.0, NOW + 20 * H)}
    d = ac.decide("north", {"north": north, "south": south}, NOW)
    assert (d.action, d.account, d.until) == ("pause", "south", NOW + 20 * H)
    assert "to switch to then" in d.reason
    # and when the account in use resets first, the wait is on it
    south_late = {"session": (6.0, NOW + 4 * H), "weekly": (96.0, NOW + 90 * H)}
    d = ac.decide("north", {"north": north, "south": south_late}, NOW)
    assert (d.action, d.account, d.until) == ("pause", "north", NOW + 72 * H)


def test_decide_on_a_limit_hit_and_on_unknown_usage():
    fine = {"session": (60.0, NOW + 3 * H), "weekly": (40.0, NOW + 100 * H)}
    assert ac.decide("a", {"a": fine, "b": fine}, NOW).action == "stay"
    # the limit was hit before a reading showed it: a session block three hours from its reset
    # is left for the other account, one that ends within the hour is waited out
    assert ac.decide("a", {"a": fine, "b": fine}, NOW, hit="session").account == "b"
    soon = {"session": (60.0, NOW + dt.timedelta(minutes=30)), "weekly": (40.0, NOW + 100 * H)}
    hit = ac.decide("a", {"a": soon, "b": fine}, NOW, hit="session")
    assert (hit.action, hit.until) == ("pause", NOW + dt.timedelta(minutes=30))
    assert ac.decide("a", {"a": fine, "b": fine}, NOW, hit="weekly").account == "b"
    assert ac.decide("a", {"a": None, "b": fine}, NOW).action == "stay"
    assert ac.decide("a", {"a": fine, "b": None}, NOW, hit="weekly").action == "pause"   # unknown is never chosen


def test_decide_takes_a_target_whose_session_limit_is_merely_usable():
    # the weekly floor is the rule; a target's session limit resets within hours either way, so one at
    # 90% of it with weekly room is still a switch when the account in use is blocked for hours
    north = {"session": (99.0, NOW + 4 * H), "weekly": (85.0, NOW + 100 * H)}
    south = {"session": (90.0, NOW + 3 * H), "weekly": (40.0, NOW + 100 * H)}
    d = ac.decide("north", {"north": north, "south": south}, NOW)
    assert (d.action, d.account) == ("switch", "south")
    spent = {"session": (98.5, NOW + 3 * H), "weekly": (40.0, NOW + 100 * H)}
    assert ac.decide("north", {"north": north, "south": spent}, NOW).action == "pause"
