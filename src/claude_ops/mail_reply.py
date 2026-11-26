"""Answer an email thread correctly, and show the answer before anything is sent.

Sending a reply by hand through an API goes wrong in three quiet ways, and none of them shows in the
sender's own mailbox.

- A body written with bare line feeds reaches Outlook and Exchange as one long paragraph. Most mail
  libraries turn the carriage returns back into line feeds inside the encoded body, so the fault is
  in the message and not in the text that was typed.
- A reply that is not made against the newest message of the thread answers something said earlier,
  or answers the sender's own last message, so the conversation talks to itself.
- A reply with only the reply headers reads as a new message. A real reply carries the attribution
  line and the quoted original.

`reply` reads a thread, takes the newest message that is not a draft, refuses when that message was
sent by the mailbox's own owner, builds the reply with hand-written MIME (CRLF throughout, the
subject encoded as RFC 2047, the reply headers set from the message it answers and a quote of it)
and returns it. Nothing is sent by default. Sending needs the caller to say so (`send=True`, or
`--send` on the command line), and after a send the message is read back from Gmail and checked
(CRLF, no bare line feed, the right recipient, the right thread, `In-Reply-To` equal to the message
answered), so a send that came out wrong is reported as such.

    box = Mailbox("/path/to/token.json", "/path/to/credentials.json")
    plan = reply(box, "18c2f00000000000", "Hello,\\n\\nThank you.\\n\\nKind regards,\\nAlex\\n")
    print(plan["to"], plan["subject"])       # nothing has been sent
    done = reply(box, "18c2f00000000000", body, send=True)
    assert done["verified"]

    claude-ops email-reply --token-file token.json --client-file credentials.json \\
        --thread 18c2f00000000000 --body-file reply.txt            # shows it
    claude-ops email-reply ... --send                              # sends it, then checks it

Read the body from a file and not from an argument, because a body with an apostrophe inside a shell
argument is a parse failure that lands far from its cause. The message is plain text. The `From`
header is left out, so Gmail uses the signed-in account. The token needs the gmail.readonly scope to
show a reply and the gmail.send scope to send one.
"""
from __future__ import annotations

import argparse
import base64
import email
import email.header
import email.policy
import email.utils
import json
import re
import sys
import textwrap

from .gmail import Mailbox, _strip_tags
from .google_api import GoogleError

#: how many lines of the answered message are quoted
QUOTE_LINES = 60
_REPLY_PREFIX = re.compile(r"^\s*re\s*:", re.IGNORECASE)
_BARE_LF = re.compile(rb"(?<!\r)\n")


class Refused(Exception):
    """The reply was not built, and what is wrong is in the message."""


def _one_line(value: object) -> str:
    """A header value on one line. A line break inside a value would start another header."""
    return " ".join(str(value or "").split())


def newest_received(box, thread_id: str) -> str:
    """The id of the newest message of the thread that is not a draft. Raises `Refused` when the
    thread holds none, or when that message was sent from this mailbox, since a reply to the owner's
    own message is a thread talking to itself."""
    found = [m for m in box.thread(thread_id) if "DRAFT" not in m["labels"]]
    if not found:
        raise Refused(f"thread {thread_id} holds no message that is not a draft")
    last = found[-1]
    if "SENT" in last["labels"]:
        raise Refused(f"the newest message of thread {thread_id} ({last['id']}) was sent from this mailbox, "
                      "so there is nothing from the other person to answer")
    return last["id"]


def _text_of(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content()
    part = msg.get_body(preferencelist=("html",))
    return _strip_tags(part.get_content()) if part is not None else ""


def build(original: bytes, body: str) -> dict:
    """The reply to one message, built and not sent. `original` is the message exactly as Gmail
    holds it. Returns `to`, `subject`, `in_reply_to`, `references`, `text` (the whole body, with the
    quote) and `raw` (the finished message, bytes). Pure."""
    if not body.strip():
        raise Refused("the reply body is empty")
    msg = email.message_from_bytes(original, policy=email.policy.default)
    mid = _one_line(msg["Message-ID"])
    if not mid:
        raise Refused("the message being answered has no Message-ID, so a reply cannot be tied to it")
    people = email.utils.getaddresses([str(msg["Reply-To"] or msg["From"] or "")])
    to = next((a for _, a in people if a and "@" in a), "")
    if not to:
        raise Refused("the message being answered names no sender to reply to")
    subject = _one_line(msg["Subject"])
    if not _REPLY_PREFIX.match(subject):
        subject = "Re: " + subject
    references = (_one_line(msg["References"]) + " " + mid).strip()
    quoted_text = _text_of(msg).replace("\r\n", "\n").strip().split("\n")[:QUOTE_LINES]
    quote = "\n".join("> " + line if line else ">" for line in quoted_text)
    when = _one_line(msg["Date"]) or "an earlier date"
    who = _one_line(msg["From"]) or to
    text = body.replace("\r\n", "\n").rstrip("\n") + f"\n\nOn {when}, {who} wrote:\n{quote}\n"
    # Built by hand: MIMEText and set_content turn CRLF back into LF inside the encoded body.
    crlf = text.replace("\n", "\r\n").encode("utf-8")
    b64 = "\r\n".join(textwrap.wrap(base64.b64encode(crlf).decode(), 76))
    headers = [("To", to), ("Subject", email.header.Header(subject, "utf-8").encode()),
               ("In-Reply-To", mid), ("References", references), ("MIME-Version", "1.0"),
               ("Content-Type", 'text/plain; charset="utf-8"'), ("Content-Transfer-Encoding", "base64")]
    try:
        raw = ("\r\n".join(f"{k}: {v}" for k, v in headers) + "\r\n\r\n" + b64 + "\r\n").encode("ascii")
    except UnicodeEncodeError as e:
        raise Refused("a header of the message being answered holds a character a header cannot carry") from e
    return {"to": to, "subject": subject, "in_reply_to": mid, "references": references, "text": text, "raw": raw}


def problems(plan: dict, back: bytes, labels: list[str], thread_id: str, sent_thread: "str | None") -> list[str]:
    """What is wrong with a message read back from Gmail after a send, as a list of sentences (empty
    when nothing is). The check reads the decoded body, never the base64 text around it, because
    counting line breaks in the encoded text passes while the body is still broken."""
    out = []
    msg = email.message_from_bytes(back, policy=email.policy.default)
    payload = msg.get_payload(decode=True) or b""
    if b"\r\n" not in payload:
        out.append("the sent body has no CRLF line breaks")
    if _BARE_LF.search(payload):
        out.append("the sent body holds bare line feeds")
    if "SENT" not in labels:
        out.append("Gmail does not label the message SENT")
    if sent_thread != thread_id:
        out.append(f"the message is in thread {sent_thread}, not {thread_id}")
    if _one_line(msg["In-Reply-To"]) != plan["in_reply_to"]:
        out.append("In-Reply-To is not the message that was answered")
    if plan["to"].lower() not in str(msg["To"] or "").lower():
        out.append("the recipient is not the sender of the message that was answered")
    return out


def reply(box, thread_id: str, body: str, send: bool = False) -> dict:
    """The reply to a thread's newest received message. Returns the plan of `build` plus `answers`
    (the id of the message answered), `sent` (False, or the id of the message that was sent) and, after
    a send, `verified` and `problems`. Nothing is sent unless `send` is true."""
    answers = newest_received(box, thread_id)
    plan = build(box.raw(answers), body)
    plan.update(answers=answers, sent=False, verified=None, problems=[])
    if not send:
        return plan
    out = box.send(plan["raw"], thread_id)
    plan["sent"] = out["id"]
    plan["problems"] = problems(plan, box.raw(out["id"]), box.raw_labels(out["id"]), thread_id, out["threadId"])
    plan["verified"] = not plan["problems"]
    return plan


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops email-reply",
                                description="Show the reply to a Gmail thread's newest received message, and send "
                                            "it only with --send. Nothing is sent by default.")
    p.add_argument("--token-file", required=True, help="the OAuth token file (a JSON object with a refresh_token)")
    p.add_argument("--client-file", help="the OAuth client file, when the token file does not name its client")
    p.add_argument("--account", help="the entry to use when the token file holds several accounts")
    p.add_argument("--thread", required=True, help="the id of the thread to answer")
    p.add_argument("--body-file", required=True,
                   help="a file holding the reply text (a file and not an argument, so no quoting can break it)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true", help="show the reply and send nothing (the default)")
    mode.add_argument("--send", action="store_true", help="send the reply, then read it back and check it")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    a = p.parse_args(argv)
    try:
        body = open(a.body_file, encoding="utf-8").read()
        result = reply(Mailbox(a.token_file, a.client_file, account=a.account), a.thread, body, send=a.send)
    except (Refused, GoogleError, OSError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    shown = {k: v for k, v in result.items() if k != "raw"}
    if a.json:
        print(json.dumps(shown, indent=2))
    else:
        print(f"To: {result['to']}\nSubject: {result['subject']}\nIn-Reply-To: {result['in_reply_to']}\n"
              f"Answers message: {result['answers']}\n\n{result['text']}")
    if not a.send:
        print("DRY RUN, nothing was sent. Add --send to send it.", file=sys.stderr)
        return 0
    if result["verified"]:
        print(f"sent {result['sent']}\nVERIFIED", file=sys.stderr)
        return 0
    print(f"sent {result['sent']}, but the read-back found problems:\n- " + "\n- ".join(result["problems"]),
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
