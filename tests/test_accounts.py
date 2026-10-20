import datetime as dt
import json
import subprocess
import sys

import pytest

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


def test_usable_at():
    assert ac.usable_at({"session": (50.0, NOW + H), "weekly": (10.0, NOW + 100 * H)}, NOW) == NOW
    assert ac.usable_at({"session": (98.0, NOW + 2 * H), "weekly": (10.0, NOW + 100 * H)}, NOW) == NOW + 2 * H
    assert ac.usable_at({"session": (99.0, NOW + 2 * H), "weekly": (100.0, NOW + 100 * H)}, NOW) == NOW + 100 * H
    assert ac.usable_at(None, NOW) is None


def test_choose_switches_only_to_an_account_usable_sooner():
    weekly_gone = {"session": (0.0, NOW + H), "weekly": (100.0, NOW + 72 * H)}
    session_full = {"session": (98.5, NOW + 2 * H), "weekly": (40.0, NOW + 100 * H)}
    assert ac.choose("facterino", {"facterino": session_full, "kgeorgio": weekly_gone}, NOW) is None
    weekly_soon = {"session": (0.0, NOW + H), "weekly": (100.0, NOW + H)}
    assert ac.choose("facterino", {"facterino": session_full, "kgeorgio": weekly_soon}, NOW) is None
    assert ac.choose("facterino", {"facterino": session_full, "kgeorgio": weekly_soon}, NOW + H) == "kgeorgio"


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
