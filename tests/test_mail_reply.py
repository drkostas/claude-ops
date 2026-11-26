import base64
import email
import email.policy
import json
import re
from email.message import EmailMessage

import pytest

from claude_ops import mail_reply
from claude_ops.gmail import Mailbox
from claude_ops.mail_reply import Refused, build, newest_received, reply

THREAD = "18c2f00000000000"


class Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class Google:
    """A stand-in for urlopen: answers the token exchange itself and each Gmail call from `pages`, in order."""

    def __init__(self, pages=()):
        self.pages, self.requests = list(pages), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.full_url.startswith("https://oauth2.googleapis.com/token"):
            return Answer({"access_token": "access-1", "expires_in": 3599})
        return Answer(self.pages.pop(0))

    def posts(self):
        return [r for r in self.requests if r.get_method() == "POST" and "messages/send" in r.full_url]


def box(tmp_path, g):
    t = tmp_path / "token.json"
    t.write_text(json.dumps({"refresh_token": "refresh-1"}))
    c = tmp_path / "credentials.json"
    c.write_text(json.dumps({"installed": {"client_id": "id-1", "client_secret": "secret-1"}}))
    return Mailbox(t, c, fetch=g)


def original(subject="Invoice 42 not received", reply_to=None, references=None, text="Hello Alex,\nIt is not here.\n\nBest, Sam\n"):
    m = EmailMessage()
    m["From"] = '"Example, Sam" <sam@example.org>'
    m["To"] = "alex@example.net"
    if reply_to:
        m["Reply-To"] = reply_to
    m["Subject"] = subject
    m["Date"] = "Mon, 23 Nov 2026 15:03:18 +0000"
    m["Message-ID"] = "<abc123@mail.example.org>"
    if references:
        m["References"] = references
    m.set_content(text)
    return m.as_bytes()


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decoded(raw: bytes):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return msg, msg.get_payload(decode=True)


def thread(*messages):
    return {"messages": [{"id": i, "labelIds": labels, "internalDate": str(t)} for i, labels, t in messages]}


def test_the_body_has_crlf_and_no_bare_line_feed_in_the_decoded_text():
    plan = build(original(), "Hello Sam,\n\nThanks, it arrived.\n\nKind regards,\nAlex\n")
    msg, payload = decoded(plan["raw"])
    assert payload.count(b"\r\n") >= 6
    assert not re.findall(rb"(?<!\r)\n", payload)
    assert msg["Content-Transfer-Encoding"] == "base64"


def test_the_reply_headers_answer_the_message_and_extend_its_references():
    plan = build(original(references="<first@mail.example.org>"), "Thanks.")
    msg, _ = decoded(plan["raw"])
    assert msg["In-Reply-To"] == "<abc123@mail.example.org>"
    assert msg["References"] == "<first@mail.example.org> <abc123@mail.example.org>"
    assert msg["To"] == "sam@example.org"
    assert msg["Subject"] == "Re: Invoice 42 not received"
    assert "From" not in msg


def test_a_reply_to_header_decides_the_recipient():
    plan = build(original(reply_to="Desk <desk@example.org>"), "Thanks.")
    assert plan["to"] == "desk@example.org"


def test_a_subject_that_is_already_a_reply_keeps_one_prefix():
    assert build(original(subject="RE: Invoice"), "x")["subject"] == "RE: Invoice"
    assert build(original(subject="re : Invoice"), "x")["subject"] == "re : Invoice"


def test_a_subject_with_non_ascii_letters_is_encoded_as_rfc_2047():
    plan = build(original(subject="Τιμολόγιο 42"), "x")
    assert b"=?utf-8?" in plan["raw"].split(b"\r\n\r\n")[0].lower()
    msg, _ = decoded(plan["raw"])
    assert msg["Subject"] == "Re: Τιμολόγιο 42"


def test_the_body_quotes_the_original_with_an_attribution_line():
    plan = build(original(), "Thanks.")
    _, payload = decoded(plan["raw"])
    text = payload.decode()
    assert 'On Mon, 23 Nov 2026 15:03:18 +0000, "Example, Sam" <sam@example.org> wrote:' in text
    assert "> Hello Alex,\r\n> It is not here.\r\n>\r\n> Best, Sam" in text


def test_an_apostrophe_and_a_dollar_sign_in_the_body_arrive_unchanged():
    plan = build(original(), "It's fine, it costs $5 and `ls` is just text.")
    _, payload = decoded(plan["raw"])
    assert "It's fine, it costs $5 and `ls` is just text." in payload.decode()


def test_a_folded_subject_becomes_one_line_and_starts_no_other_header():
    folded = original().replace(b"Subject: Invoice 42 not received\n", b"Subject: Hello\r\n Bcc: x@example.org\n")
    plan = build(folded, "x")
    msg, _ = decoded(plan["raw"])
    assert msg["Bcc"] is None
    assert plan["subject"] == "Re: Hello Bcc: x@example.org"


def test_an_empty_body_a_missing_message_id_and_no_sender_are_refused():
    with pytest.raises(Refused, match="empty"):
        build(original(), "  \n")
    with pytest.raises(Refused, match="Message-ID"):
        build(original().replace(b"Message-ID: <abc123@mail.example.org>\n", b""), "x")
    with pytest.raises(Refused, match="no sender"):
        build(original().replace(b'From: "Example, Sam" <sam@example.org>\n', b""), "x")


def test_the_newest_message_that_is_not_a_draft_is_answered(tmp_path):
    g = Google([thread(("m1", ["INBOX"], 1000), ("m3", ["DRAFT"], 3000), ("m2", ["INBOX"], 2000))])
    assert newest_received(box(tmp_path, g), THREAD) == "m2"


def test_a_thread_whose_newest_message_was_sent_from_this_mailbox_is_refused(tmp_path):
    g = Google([thread(("m1", ["INBOX"], 1000), ("m2", ["SENT"], 2000))])
    with pytest.raises(Refused, match="sent from this mailbox"):
        newest_received(box(tmp_path, g), THREAD)


def test_a_thread_of_drafts_only_is_refused(tmp_path):
    g = Google([thread(("m1", ["DRAFT"], 1000))])
    with pytest.raises(Refused, match="no message that is not a draft"):
        newest_received(box(tmp_path, g), THREAD)


def test_the_default_shows_the_reply_and_sends_nothing(tmp_path):
    g = Google([thread(("m1", ["INBOX"], 1000)), {"raw": b64(original())}])
    plan = reply(box(tmp_path, g), THREAD, "Thanks.")
    assert plan["sent"] is False and plan["verified"] is None
    assert plan["answers"] == "m1" and plan["to"] == "sam@example.org"
    assert g.posts() == []


def test_a_send_posts_the_message_in_the_thread_and_verifies_the_read_back(tmp_path):
    g = Google([thread(("m1", ["INBOX"], 1000)), {"raw": b64(original())}])
    b = box(tmp_path, g)
    plan = build(original(), "Thanks.")
    g.pages += [{"id": "m9", "threadId": THREAD}, {"raw": b64(plan["raw"])}, {"labelIds": ["SENT"]}]
    done = reply(b, THREAD, "Thanks.", send=True)
    assert done["sent"] == "m9" and done["verified"] is True and done["problems"] == []
    (post,) = g.posts()
    body = json.loads(post.data)
    assert body["threadId"] == THREAD
    assert base64.urlsafe_b64decode(body["raw"]) == done["raw"]


def test_a_send_that_read_back_with_bare_line_feeds_is_reported(tmp_path):
    g = Google([thread(("m1", ["INBOX"], 1000)), {"raw": b64(original())}])
    b = box(tmp_path, g)
    plan = build(original(), "Thanks.")
    broken = plan["raw"].split(b"\r\n\r\n")[0] + b"\r\n\r\n" + base64.encodebytes(b"one\ntwo\n")
    g.pages += [{"id": "m9", "threadId": THREAD}, {"raw": b64(broken)}, {"labelIds": ["SENT"]}]
    done = reply(b, THREAD, "Thanks.", send=True)
    assert done["verified"] is False
    assert any("bare line feeds" in p for p in done["problems"])


def test_a_send_filed_in_another_thread_or_not_labelled_sent_is_reported(tmp_path):
    g = Google([thread(("m1", ["INBOX"], 1000)), {"raw": b64(original())}])
    b = box(tmp_path, g)
    plan = build(original(), "Thanks.")
    g.pages += [{"id": "m9", "threadId": "other"}, {"raw": b64(plan["raw"])}, {"labelIds": ["INBOX"]}]
    done = reply(b, THREAD, "Thanks.", send=True)
    text = " ".join(done["problems"])
    assert "thread other" in text and "SENT" in text


def test_the_command_defaults_to_a_dry_run_and_says_so(tmp_path, monkeypatch, capsys):
    body = tmp_path / "reply.txt"
    body.write_text("Hello Sam,\n\nIt's here, thank you.\n")
    g = Google([thread(("m1", ["INBOX"], 1000)), {"raw": b64(original())}])
    monkeypatch.setattr("claude_ops.mail_reply.Mailbox",
                        lambda t, c, account=None: Mailbox(t, c, account=account, fetch=g))
    t = tmp_path / "token.json"
    t.write_text(json.dumps({"refresh_token": "refresh-1"}))
    c = tmp_path / "credentials.json"
    c.write_text(json.dumps({"installed": {"client_id": "id-1", "client_secret": "secret-1"}}))
    rc = mail_reply.main(["--token-file", str(t), "--client-file", str(c), "--thread", THREAD,
                          "--body-file", str(body)])
    out = capsys.readouterr()
    assert rc == 0 and g.posts() == []
    assert "To: sam@example.org" in out.out and "It's here, thank you." in out.out
    assert "DRY RUN, nothing was sent" in out.err


def test_send_and_dry_cannot_be_given_together(tmp_path, capsys):
    with pytest.raises(SystemExit):
        mail_reply.main(["--token-file", "t", "--thread", THREAD, "--body-file", "b", "--send", "--dry"])
    assert "not allowed with" in capsys.readouterr().err


def test_the_command_says_why_it_refused(tmp_path, monkeypatch, capsys):
    body = tmp_path / "reply.txt"
    body.write_text("Hello.\n")
    g = Google([thread(("m1", ["INBOX"], 1000), ("m2", ["SENT"], 2000))])
    monkeypatch.setattr("claude_ops.mail_reply.Mailbox",
                        lambda t, c, account=None: Mailbox(t, c, account=account, fetch=g))
    t = tmp_path / "token.json"
    t.write_text(json.dumps({"refresh_token": "refresh-1"}))
    c = tmp_path / "credentials.json"
    c.write_text(json.dumps({"installed": {"client_id": "id-1", "client_secret": "secret-1"}}))
    rc = mail_reply.main(["--token-file", str(t), "--client-file", str(c), "--thread", THREAD,
                          "--body-file", str(body), "--send"])
    assert rc == 1 and g.posts() == []
    assert "sent from this mailbox" in capsys.readouterr().err


def test_the_command_is_reachable_from_the_cli_table():
    from claude_ops.cli import TOOLS
    assert TOOLS["email-reply"][0] == "mail_reply"
