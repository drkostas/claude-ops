from claude_ops.mailsort import (Sorter, address, domain, gitguardian_incidents, is_automated, is_bulk,
                                 neon_usage, vercel_deploy_failed)

SORTER = Sorter(kinds={"scanner": ["gitguardian.com"], "platform": ["neon.tech", "vercel.com"]},
                names={"widget": ["widget-cli"], "gadget": []},
                has_reader=["github.com", "watcher@example.net"], own=["me@example.org"])


def test_address_and_domain():
    assert address("Ana Example <Ana@Example.ORG>") == "ana@example.org"
    assert domain('"Alerts" <alerts@email.example.com>') == "email.example.com"
    assert address("") == "" and domain("nobody") == ""


def test_a_kind_sender_wins_and_subdomains_count():
    got = SORTER.sort({"From": "Neon Alerts <alerts@email.neon.tech>", "Subject": "Compute"}, "nothing")
    assert (got.kind, got.names) == ("platform", [])
    got = SORTER.sort({"from": "security@gitguardian.com", "subject": "acme/widget - 1 incident"}, "")
    assert (got.kind, got.names) == ("scanner", ["widget"])


def test_a_person_naming_a_project():
    got = SORTER.sort({"From": "Ana <ana@example.com>", "Subject": "About widget-cli"}, "Hi, I tried it.")
    assert (got.kind, got.names, got.sender) == ("person", ["widget"], "ana@example.com")


def test_names_are_whole_words():
    assert SORTER.names_in("widgetry and gadgets") == []
    assert SORTER.names_in("the widget.") == ["widget"]
    assert SORTER.names_in("Gadget and WIDGET") == ["widget", "gadget"]
    assert SORTER.names_in("see widget.io for more") == []


def test_left_alone():
    assert SORTER.sort({"From": "me@example.org", "Subject": "widget"}).why == "the user's own mail"
    assert SORTER.sort({"From": "GitHub <notifications@github.com>", "Subject": "widget"}).kind is None
    assert SORTER.sort({"From": "watcher@example.net", "Subject": "widget"}).kind is None
    assert SORTER.sort({"From": "ana@example.com", "Subject": "lunch?"}).kind is None
    news = {"From": "Weekly <news@example.com>", "Subject": "lunch news"}
    assert SORTER.sort(news).kind is None


def test_automated_mail_naming_a_project_is_a_mention():
    pitch = {"From": "Bo <bo@mail.example.net>", "Subject": "widget commit 0a1b2c3",
             "List-Unsubscribe": "<https://mail.example.net/u>"}
    got = SORTER.sort(pitch, "I read your commit")
    assert (got.kind, got.names) == ("mention", ["widget"])


def test_is_automated():
    assert is_automated({"From": "no-reply@example.com"})
    assert is_automated({"From": "notifications+abc@example.com"})
    assert is_automated({"From": "ana@example.com", "List-Unsubscribe": "<mailto:x@example.com>"})
    assert is_automated({"From": "ana@example.com", "Precedence": "bulk"})
    assert is_automated({"From": "ana@example.com", "Auto-Submitted": "auto-generated"})
    assert not is_automated({"From": "ana@example.com", "Auto-Submitted": "no"})
    assert not is_automated({"From": "Ana <ana@example.com>"})


def test_is_bulk():
    assert is_bulk(["INBOX", "CATEGORY_PROMOTIONS"])
    assert not is_bulk(["INBOX", "CATEGORY_UPDATES"]) and not is_bulk(None)


def test_neon_usage_compute():
    full = ("Your compute limit is exceeded Your project quiet-river-123 ( https://console.example ) has used "
            "100% of its 100 CU-hour monthly compute allowance. Apps for this project may fail to connect.")
    assert neon_usage("You've used all your monthly compute allowance", full) == {
        "project": "quiet-river-123", "metric": "compute", "pct": 100.0, "used": None, "allowance": 100.0,
        "unit": "CU-hours"}
    near = ("You're almost out of compute Your project demo-db ( <url> ) has used 80% (80.2 CU-hours) of its "
            "100 CU-hour monthly compute allowance.")
    assert neon_usage("You've used 80% of your monthly compute allowance", near)["used"] == 80.2
    assert neon_usage("Neon Changelog", "Realtime is here") is None


def test_neon_usage_transfer():
    full = "Your project demo-db ( <url> ) has used 100% of its monthly 5 GB network transfer allowance."
    assert neon_usage("You've used all your monthly public network transfer", full) == {
        "project": "demo-db", "metric": "transfer", "pct": 100.0, "used": None, "allowance": 5.0, "unit": "GB"}
    near = "Your project demo-db ( <url> ) has used 96% (4.8 GB) of its 5 GB monthly public network transfer allowance."
    got = neon_usage("You've used 96% of your monthly public network transfer", near)
    assert (got["metric"], got["pct"], got["used"], got["allowance"]) == ("transfer", 96.0, 4.8, 5.0)


def test_vercel_deploy_failed():
    text = ("Production deployment failed Hello, ana. The production deployment for project widget-web "
            "failed on branch main at commit 7b7bc90. Git provider error")
    assert vercel_deploy_failed("Production deployment failed for widget-web", text) == {
        "project": "widget-web", "target": "production", "branch": "main", "commit": "7b7bc90"}
    assert vercel_deploy_failed("Preview deployment failed for w", "")["commit"] is None
    assert vercel_deploy_failed("Watch the livestream", text) is None


def test_gitguardian_incidents():
    one = ("GitGuardian security alert 1 internal secret incident detected! Generic Password "
           "2026-01-10 09:58:27 AM (UTC) See on GitGuardian acme/​widget (commit cf72ea4) See on GitHub "
           "GitGuardian is an automated secrets detection service.")
    assert gitguardian_incidents("acme/widget - 1 internal incident detected", one) == [
        {"kind": "Generic Password", "repo": "acme/widget", "commit": "cf72ea4"}]
    two = ("GitGuardian security alert 2 internal secret incidents detected! Generic Password 2026-01-12 "
           "11:51:34 AM (UTC) See on GitGuardian acme/widget (commit f539d3b) See on GitHub acme/widget "
           "(commit 0a1b2c3) See on GitHub And 2 other occurrences Username Password 2026-01-12 11:51:34 AM "
           "(UTC) See on GitGuardian acme/gadget (commit 1234abc) See on GitHub")
    assert gitguardian_incidents("acme/widget - 2 internal incidents detected", two) == [
        {"kind": "Generic Password", "repo": "acme/widget", "commit": "f539d3b"},
        {"kind": "Generic Password", "repo": "acme/widget", "commit": "0a1b2c3"},
        {"kind": "Username Password", "repo": "acme/gadget", "commit": "1234abc"}]
    assert gitguardian_incidents("Historical Scan Report - 0 Secrets", one) == []
