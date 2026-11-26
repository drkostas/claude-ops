"""Search a Gmail mailbox from a scheduled job, with the two files a Google sign-in leaves behind.

A watcher (see `watch`) often has to ask one question of a mailbox: is there a message matching this
search that it has not seen? Google's client libraries are a large install for that, and a job started
by a scheduler has no browser to sign in with. This module uses only the standard library and two
files that a finished OAuth sign-in leaves on disk:

- the token file, any JSON object with a `refresh_token` (and, as google-auth's authorized-user files
  have, optionally its own `client_id`, `client_secret` and `token_uri`), and
- the OAuth client file, as the Google Cloud console downloads it (`{"installed": {...}}` or
  `{"web": {...}}`), needed only when the token file does not name its client.

    from claude_ops.gmail import Mailbox
    box = Mailbox("/path/to/token.json", "/path/to/credentials.json")
    for m in box.search('subject:"[example.org]" newer_than:7d'):
        print(box.message(m["id"])["headers"].get("Subject"))

A `Mailbox` asks Google for a fresh access token the first time it is used and keeps it for its own
life, so a job that makes one per run never holds an expired one. Nothing is written to either file.
The token needs a scope that can read mail (gmail.readonly is enough).

A request Google failed (a 5xx) or asked to slow down (a 429) is asked again after a short wait, so one
failed request does not cost a job the rest of its read. Once the tries are spent, `GmailError` says what
Google said, and its `needs_sign_in` tells a refused sign-in from everything a later run can get past.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .google_api import ASKED_AGAIN, MAX_WAIT_S, TOKEN_URI, GoogleError, GoogleSession, _client  # noqa: F401

API = "https://gmail.googleapis.com/gmail/v1/users/me/"
#: the headers `message` asks for when it is not told which
HEADERS = ("From", "To", "Subject", "Date", "Reply-To")
#: the name this module's refusals have always had
GmailError = GoogleError


def _part_data(part: dict) -> str:
    """The decoded text of one MIME part's body, or "" when it carries none. Gmail returns body
    data base64url-encoded without padding, so the padding is restored before decoding."""
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _first_part(payload: dict, mime: str) -> str:
    """The first part of this MIME type in a payload tree, decoded, walking the tree depth-first;
    "" when there is none."""
    if payload.get("mimeType") == mime:
        text = _part_data(payload)
        if text:
            return text
    for part in payload.get("parts") or []:
        text = _first_part(part, mime)
        if text:
            return text
    return ""


class _Stripper(HTMLParser):
    """Collect an HTML document's text, skipping script and style and marking tag boundaries with a
    space so adjacent blocks do not run together; HTMLParser decodes character references itself."""

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif not self._skip:
            self._text.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif not self._skip:
            self._text.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self._text.append(data)

    def text(self) -> str:
        return "".join(self._text)


class _Links(HTMLParser):
    """Collect each link of an HTML document as its words and its address, in document order. A link
    with no address, or one that only points inside the page, is left out."""

    def __init__(self):
        super().__init__()
        self._open: "dict | None" = None
        self.found: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            self._open = {"text": [], "href": href} if href and not href.startswith("#") else None

    def handle_endtag(self, tag):
        if tag == "a" and self._open is not None:
            self.found.append({"text": " ".join("".join(self._open["text"]).split()), "href": self._open["href"]})
            self._open = None

    def handle_data(self, data):
        if self._open is not None:
            self._open["text"].append(data)


def _strip_tags(html: str) -> str:
    """An HTML part reduced to its text, runs of whitespace collapsed to single spaces."""
    stripper = _Stripper()
    stripper.feed(html)
    return " ".join(stripper.text().split())


class Mailbox(GoogleSession):
    """One Gmail mailbox, read with a refresh token. It takes what `google_api.GoogleSession` takes."""

    def _get(self, path: str, params: list[tuple[str, str]]) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return self._open(urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token()}))

    def search(self, query: str, max_results: int = 100) -> list[dict]:
        """The messages matching a Gmail search (the same words as the search box), newest first, at
        most `max_results`: each `{"id", "threadId"}`. Gmail's default search leaves out Trash and Spam, so
        a search that must not miss a message starts with `in:anywhere -in:spam`."""
        found: list[dict] = []
        page = None
        while len(found) < max_results:
            params = [("q", query), ("maxResults", str(min(500, max_results - len(found))))]
            if page:
                params.append(("pageToken", page))
            out = self._get("messages", params)
            found += [{"id": m["id"], "threadId": m.get("threadId")} for m in out.get("messages", [])]
            page = out.get("nextPageToken")
            if not page:
                break
        return found[:max_results]

    def thread(self, thread_id: str) -> list[dict]:
        """The messages of one conversation, oldest first, each `{"id", "labels", "internalDate"}`
        with internalDate as a number of epoch milliseconds. A draft is in the list, labelled DRAFT,
        so a caller that wants only what was sent or received leaves it out."""
        out = self._get("threads/" + urllib.parse.quote(thread_id, safe=""), [("format", "minimal")])
        found = [{"id": m["id"], "labels": m.get("labelIds", []), "internalDate": int(m.get("internalDate") or 0)}
                 for m in out.get("messages", [])]
        return sorted(found, key=lambda m: m["internalDate"])

    def raw(self, message_id: str) -> bytes:
        """One message exactly as Gmail holds it (headers, parts and encodings), decoded from the
        base64url Gmail answers with."""
        out = self._get("messages/" + urllib.parse.quote(message_id, safe=""), [("format", "raw")])
        data = out.get("raw") or ""
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

    def raw_labels(self, message_id: str) -> list[str]:
        """The labels of one message (SENT, INBOX, DRAFT and the person's own)."""
        out = self._get("messages/" + urllib.parse.quote(message_id, safe=""), [("format", "minimal")])
        return out.get("labelIds", [])

    def send(self, raw: bytes, thread_id: "str | None" = None) -> dict:
        """Send one finished message. This puts mail in front of other people and cannot be taken
        back, so nothing in this module calls it unless its caller was told to. `thread_id` files the
        message in that conversation, which Gmail does only when the subject and the reply headers
        agree. The token needs the gmail.send scope. Returns `{"id", "threadId"}`."""
        payload = {"raw": base64.urlsafe_b64encode(raw).decode()}
        if thread_id:
            payload["threadId"] = thread_id
        req = urllib.request.Request(API + "messages/send", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + self.token(),
                                              "Content-Type": "application/json"})
        out = self._open(req)
        return {"id": out.get("id"), "threadId": out.get("threadId")}

    def message(self, message_id: str, headers=HEADERS) -> dict:
        """One message's id, thread, labels, internalDate, snippet and the named headers by name,
        without its body (see `body`). internalDate is Gmail's own arrival time as a string of epoch
        milliseconds, for a watcher that checkpoints by when a message arrived rather than by the
        Date header a sender sets; it is None when the response omits it."""
        params = [("format", "metadata")] + [("metadataHeaders", h) for h in headers]
        out = self._get("messages/" + urllib.parse.quote(message_id, safe=""), params)
        got = {h["name"]: h["value"] for h in (out.get("payload") or {}).get("headers", [])}
        return {"id": out.get("id", message_id), "threadId": out.get("threadId"),
                "labels": out.get("labelIds", []), "internalDate": out.get("internalDate"),
                "snippet": out.get("snippet", ""), "headers": got}

    def body(self, message_id: str) -> str:
        """The message's text, for a watcher that must read it and not only its headers: the first
        text/plain part decoded (walking a multipart message), else the text/html part with its
        tags stripped, else "". It takes the same token path as `message`, asks for the full
        message, and writes nothing to either file."""
        out = self._get("messages/" + urllib.parse.quote(message_id, safe=""), [("format", "full")])
        payload = out.get("payload") or {}
        plain = _first_part(payload, "text/plain")
        if plain:
            return plain
        html = _first_part(payload, "text/html")
        return _strip_tags(html) if html else ""

    def links(self, message_id: str) -> list[dict]:
        """The links of the message's HTML part, in order: each `{"text", "href"}`, the words a reader
        sees and the address they open. A text-only message has none. A sign-in or confirmation mail
        often puts its one useful address behind a button, which `body` drops with the tags. An address
        such as a sign-in link is a credential while it lasts, so the caller decides where it goes and
        keeps it out of its logs."""
        out = self._get("messages/" + urllib.parse.quote(message_id, safe=""), [("format", "full")])
        html = _first_part(out.get("payload") or {}, "text/html")
        if not html:
            return []
        parser = _Links()
        parser.feed(html)
        return parser.found
