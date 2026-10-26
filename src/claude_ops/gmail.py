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
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://gmail.googleapis.com/gmail/v1/users/me/"
TOKEN_URI = "https://oauth2.googleapis.com/token"
#: the headers `message` asks for when it is not told which
HEADERS = ("From", "To", "Subject", "Date", "Reply-To")


class GmailError(Exception):
    """Google refused a request. `status` is the HTTP status, 0 when no answer came."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status = status


def _client(token: dict, client_file) -> dict:
    """Pure apart from reading `client_file`. The OAuth client's id, secret and token address: the
    token file's own when it names them, else the client file's."""
    if token.get("client_id") and token.get("client_secret"):
        return {"client_id": token["client_id"], "client_secret": token["client_secret"],
                "token_uri": token.get("token_uri") or TOKEN_URI}
    if client_file is None:
        raise ValueError("the token file names no client_id and client_secret, so a client file is needed")
    data = json.loads(Path(client_file).read_text())
    inner = data.get("installed") or data.get("web") or data
    if not (inner.get("client_id") and inner.get("client_secret")):
        raise ValueError(f"{client_file} holds no client_id and client_secret")
    return {"client_id": inner["client_id"], "client_secret": inner["client_secret"],
            "token_uri": inner.get("token_uri") or TOKEN_URI}


class Mailbox:
    """One Gmail mailbox, read with a refresh token. `fetch` is `urllib.request.urlopen` or a stand-in."""

    def __init__(self, token_file, client_file=None, *, fetch=urllib.request.urlopen, timeout: float = 40):
        token = json.loads(Path(token_file).read_text())
        if not token.get("refresh_token"):
            raise ValueError(f"{token_file} holds no refresh_token")
        self._refresh = token["refresh_token"]
        self._client = _client(token, client_file)
        self._fetch, self._timeout = fetch, timeout
        self._access: str | None = None

    def _open(self, req) -> dict:
        try:
            with self._fetch(req, timeout=self._timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
            raise GmailError(e.code, " ".join(body.split())[:300] or str(e.reason)) from e
        except urllib.error.URLError as e:
            raise GmailError(0, f"no answer: {e.reason}") from e

    def token(self) -> str:
        """An access token, asked for once per Mailbox."""
        if self._access is None:
            body = urllib.parse.urlencode({"client_id": self._client["client_id"],
                                           "client_secret": self._client["client_secret"],
                                           "refresh_token": self._refresh,
                                           "grant_type": "refresh_token"}).encode()
            out = self._open(urllib.request.Request(self._client["token_uri"], data=body, method="POST"))
            if not out.get("access_token"):
                raise GmailError(0, "the token answer held no access_token")
            self._access = out["access_token"]
        return self._access

    def _get(self, path: str, params: list[tuple[str, str]]) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return self._open(urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token()}))

    def search(self, query: str, max_results: int = 100) -> list[dict]:
        """The messages matching a Gmail search (the same words as the search box), newest first, at
        most `max_results`: each `{"id", "threadId"}`."""
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

    def message(self, message_id: str, headers=HEADERS) -> dict:
        """One message's headers (only those named, by name), snippet, labels and thread, without its body."""
        params = [("format", "metadata")] + [("metadataHeaders", h) for h in headers]
        out = self._get("messages/" + urllib.parse.quote(message_id, safe=""), params)
        got = {h["name"]: h["value"] for h in (out.get("payload") or {}).get("headers", [])}
        return {"id": out.get("id", message_id), "threadId": out.get("threadId"),
                "labels": out.get("labelIds", []), "snippet": out.get("snippet", ""), "headers": got}
