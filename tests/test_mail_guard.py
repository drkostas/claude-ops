"""The mail-guard hook: drafts pass and are logged, a send passes only for a rule or a one-time approval."""
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from claude_ops.hooks import mail_guard as mg

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
SEND, DRAFT = "mcp__mail__send_email", "mcp__mail__create_draft"


@pytest.fixture()
def cfg(tmp_path):
    (tmp_path / "approvals").mkdir()
    return {"mode": "enforce", "send_tools": [SEND], "draft_tools": [DRAFT, "mcp__mail__update_draft"],
            "rules": [{"name": "weekly report", "account": "work", "thread": "t1",
                       "recipients": ["reports@example.org"]}],
            "approvals": str(tmp_path / "approvals"), "log": str(tmp_path / "log.jsonl")}


def call(tool=SEND, **ti):
    base = {"account": "work", "to": "reports@example.org", "thread_id": "t1", "subject": "Week 2", "body": "Hello"}
    base.update(ti)
    return {"tool_name": tool, "tool_input": {k: v for k, v in base.items() if v is not None}}


def approve(cfg, name, *, to=("someone@example.org",), body="Hello", thread="", account="work", minutes=30):
    rec = {"account": account, "recipients": list(to), "thread": thread,
           "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
           "expires": (NOW + timedelta(minutes=minutes)).isoformat()}
    path = f"{cfg['approvals']}/{name}.json"
    with open(path, "w") as f:
        json.dump(rec, f)
    return path


def test_a_send_to_an_unknown_address_is_refused(cfg):
    decision, why = mg.decide(cfg, call(to="stranger@example.net"), NOW)
    assert decision == "refused"
    assert "stranger@example.net" in why


def test_a_send_under_a_rule_passes(cfg):
    assert mg.decide(cfg, call(), NOW) == ("rule", "weekly report")


def test_a_rule_does_not_cover_an_extra_recipient_another_thread_or_account(cfg):
    assert mg.decide(cfg, call(cc="other@example.org"), NOW)[0] == "refused"
    assert mg.decide(cfg, call(bcc="Other <other@example.org>"), NOW)[0] == "refused"
    assert mg.decide(cfg, call(thread_id="t2"), NOW)[0] == "refused"
    assert mg.decide(cfg, call(thread_id=None), NOW)[0] == "refused"
    assert mg.decide(cfg, call(account="home"), NOW)[0] == "refused"


def test_recipients_are_read_from_names_and_lists():
    ti = {"to": "A <A@Example.org>; b@example.org", "cc": "a@example.org", "bcc": ""}
    assert mg.recipients(ti) == ["a@example.org", "b@example.org"]


def test_a_draft_always_passes_and_is_logged(cfg, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call(DRAFT, to="stranger@example.net"))))
    monkeypatch.setattr(mg, "load", lambda argv: cfg)
    assert mg.main([]) == 0
    line = json.loads(open(cfg["log"]).read().splitlines()[-1])
    assert line["decision"] == "draft" and line["recipients"] == ["stranger@example.net"]
    assert line["body_sha256"] == hashlib.sha256(b"Hello").hexdigest()


def test_an_approval_lets_one_exact_message_through_once(cfg):
    approve(cfg, "a1")
    msg = call(to="someone@example.org", thread_id=None)
    assert mg.decide(cfg, msg, NOW) == ("approval", "a1.json")
    assert mg.decide(cfg, msg, NOW)[0] == "refused", "an approval is used once"


def test_an_approval_does_not_cover_another_body_recipient_or_a_late_send(cfg):
    approve(cfg, "a1")
    assert mg.decide(cfg, call(to="someone@example.org", thread_id=None, body="Changed"), NOW)[0] == "refused"
    assert mg.decide(cfg, call(to="someone@example.org, x@example.org", thread_id=None), NOW)[0] == "refused"
    assert mg.decide(cfg, call(to="someone@example.org", thread_id=None), NOW + timedelta(hours=1))[0] == "refused"


def test_observe_mode_refuses_nothing_and_keeps_the_approval(cfg):
    cfg["mode"] = "observe"
    path = approve(cfg, "a1")
    assert mg.decide(cfg, call(to="stranger@example.net"), NOW)[0] == "would-refuse"
    assert mg.decide(cfg, call(to="someone@example.org", thread_id=None), NOW)[0] == "approval"
    assert "used" not in json.load(open(path))


def test_main_refuses_with_blocked_and_logs_it(cfg, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call(to="stranger@example.net"))))
    monkeypatch.setattr(mg, "load", lambda argv: cfg)
    assert mg.main([]) == 2
    assert capsys.readouterr().err.startswith("Blocked: ")
    assert json.loads(open(cfg["log"]).read().splitlines()[-1])["decision"] == "refused"


def test_a_tool_the_guard_does_not_cover_passes_unlogged(cfg, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})))
    monkeypatch.setattr(mg, "load", lambda argv: cfg)
    assert mg.main([]) == 0
    with pytest.raises(FileNotFoundError):
        open(cfg["log"])


def test_no_configuration_checks_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_OPS_MAIL_GUARD_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call(to="stranger@example.net"))))
    assert mg.main([]) == 0


def test_a_log_that_cannot_be_written_still_refuses(cfg, monkeypatch, tmp_path):
    cfg["log"] = str(tmp_path / "a-file" / "log.jsonl")
    (tmp_path / "a-file").write_text("not a folder")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call(to="stranger@example.net"))))
    monkeypatch.setattr(mg, "load", lambda argv: cfg)
    assert mg.main([]) == 2
