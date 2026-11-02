"""A signed-in session with a Google API, from the two files a finished Google sign-in leaves on disk.

The session uses only the standard library. It needs no browser, so a job started by a scheduler can
use it, and it writes nothing to either file:

- the token file, a JSON object with a `refresh_token` (and, as google-auth's authorized-user files
  have, optionally its own `client_id`, `client_secret` and `token_uri`), or a JSON object holding one
  such object per account, when the session is given the `account` to use, and
- the OAuth client file, as the Google Cloud console downloads it (`{"installed": {...}}` or
  `{"web": {...}}`), needed only when the token names no client.

`gmail.Mailbox` and `gcal.Calendar` are this session with their own calls on top. It asks Google for an
access token the first time it is used and keeps it for its own life, so a job that makes one per run
never holds an expired one.

A request Google failed (a 5xx) or asked to slow down (a 429) is asked again after a short wait, so one
failed request does not cost a job the rest of its work. Once the tries are spent, `GoogleError` says
what Google said, and its `needs_sign_in` tells a refused sign-in from everything a later run can get
past.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .accounts import retry_after_seconds

TOKEN_URI = "https://oauth2.googleapis.com/token"
#: where Google says what an access token may do
TOKEN_INFO = "https://oauth2.googleapis.com/tokeninfo"
#: the answers worth asking again: Google failed, or it asks for fewer requests. A refusal stays one.
ASKED_AGAIN = frozenset({429, 500, 502, 503, 504})
#: the longest wait between tries, even when an answer's Retry-After names a longer one
MAX_WAIT_S = 30.0


class GoogleError(Exception):
    """Google refused a request. `status` is the HTTP status, 0 when no answer came."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status = status
        self.detail = detail

    @property
    def needs_sign_in(self) -> bool:
        """Whether only a new sign-in can help: Google refused the access token (401), or the token
        exchange refused the saved refresh token (400 invalid_grant)."""
        return self.status == 401 or (self.status == 400 and "invalid_grant" in self.detail)


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


class GoogleSession:
    """One Google account, used with a refresh token. `account` picks an entry from a token file that
    holds several. `fetch` is `urllib.request.urlopen` or a stand-in. `tries` is how many times one
    request is made while Google fails it, `wait_s` the first wait between them (it doubles each time),
    and `sleep` is `time.sleep` or a stand-in."""

    def __init__(self, token_file, client_file=None, *, account: "str | None" = None, fetch=urllib.request.urlopen,
                 timeout: float = 40, tries: int = 3, wait_s: float = 2.0, sleep=time.sleep):
        token = json.loads(Path(token_file).read_text())
        if account is not None:
            if account not in token:
                raise ValueError(f"{token_file} holds no account {account!r}")
            token = token[account]
        if not token.get("refresh_token"):
            raise ValueError(f"{token_file} holds no refresh_token")
        self._refresh = token["refresh_token"]
        self._client = _client(token, client_file)
        self._fetch, self._timeout = fetch, timeout
        self._tries, self._wait_s, self._sleep = max(1, tries), wait_s, sleep
        self._access: str | None = None

    def _open(self, req) -> dict:
        """The answer to one request, asked again while Google fails it or asks for fewer requests.
        A wait the answer names (Retry-After) comes before the doubling one, up to MAX_WAIT_S. A refusal,
        no answer at all and the last failed try raise GoogleError at once."""
        for attempt in range(1, self._tries + 1):
            try:
                with self._fetch(req, timeout=self._timeout) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code not in ASKED_AGAIN or attempt == self._tries:
                    body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
                    raise GoogleError(e.code, " ".join(body.split())[:300] or str(e.reason)) from e
                named = retry_after_seconds((e.headers or {}).get("Retry-After"))
                e.close()
            except urllib.error.URLError as e:
                raise GoogleError(0, f"no answer: {e.reason}") from e
            self._sleep(min(self._wait_s * 2 ** (attempt - 1) if named is None else named, MAX_WAIT_S))
        raise AssertionError("unreachable")

    def token(self) -> str:
        """An access token, asked for once per session."""
        if self._access is None:
            body = urllib.parse.urlencode({"client_id": self._client["client_id"],
                                           "client_secret": self._client["client_secret"],
                                           "refresh_token": self._refresh,
                                           "grant_type": "refresh_token"}).encode()
            out = self._open(urllib.request.Request(self._client["token_uri"], data=body, method="POST"))
            if not out.get("access_token"):
                raise GoogleError(0, "the token answer held no access_token")
            self._access = out["access_token"]
        return self._access

    def scopes(self) -> "frozenset[str]":
        """The scopes Google says this session's access token holds, asked of Google, not read from the
        token file, which can say more than was granted."""
        url = TOKEN_INFO + "?" + urllib.parse.urlencode({"access_token": self.token()})
        return frozenset((self._open(urllib.request.Request(url)).get("scope") or "").split())
