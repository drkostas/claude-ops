"""What an https address answers when it is asked with a bearer value, and nothing else of the answer.

For a job that rotates or checks an access token. After a new token is made, "does the new one work and
does the old one still work" is two questions with one answer each, and the caller must never hold the
value in a log, an event or a message while it asks. The value goes into one Authorization header and
comes back out of nothing.

- Only https, held to `url_check.problem` (no user name or password in the address, a host, a readable
  port). A bad address raises `url_check.NotHttps` before anything is sent.
- The value is refused when it is empty or holds a space, a control character or a non-ASCII character,
  because a header built from it could carry a second header. The refusal names the rule and never the
  value.
- Redirects are never followed. A redirect is an answer of its own (its status), because following one
  would send the value to an address the caller did not name.
- The answer holds the status and, only when the caller names one, that one response header (for example
  `X-OAuth-Scopes`, which says what a GitHub token may do). Nothing else of the response is read. The
  value is in no field, no `why` and no repr of the answer.
- The timeout is a total for the whole answer. A server that sends one byte at a time is cut off.

    from claude_ops import bearer_check
    a = bearer_check.status("https://api.github.com/user", token, header="X-OAuth-Scopes")
    a.status       # 200, 401, ... or None when nothing answered
    a.header       # the named header's value, or None
"""
from __future__ import annotations

import http.client
import socket
import ssl
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from claude_ops import url_check

TIMEOUT_S = 10.0
USER_AGENT = "claude-ops bearer_check"


@dataclass(frozen=True)
class Answer:
    """`status` is None when nothing answered, and `why` says what happened in words that name no value."""
    status: "int | None"
    header: "str | None"
    why: str


def value_problem(value: str) -> "str | None":
    """Why `value` cannot be sent as a bearer value, or None when it can. Never repeats the value."""
    if not isinstance(value, str) or not value:
        return "wants a bearer value, and it is empty"
    if not value.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value):
        return "wants a bearer value with no space, control or non-ASCII character"
    return None


def status(url: str, bearer: str, *, header: "str | None" = None, timeout: float = TIMEOUT_S,
           context: "ssl.SSLContext | None" = None) -> Answer:
    """Ask `url` once with `bearer` and return its status, and `header`'s value when `header` is named.
    Raises `url_check.NotHttps` for an address `url_check.problem` refuses and `ValueError` for a value
    `value_problem` refuses, in both cases before anything is sent."""
    why = url_check.problem(url)
    if why:
        raise url_check.NotHttps(why)
    bad = value_problem(bearer)
    if bad:
        raise ValueError(bad)
    parts = urlsplit(url.strip())
    host = parts.hostname
    conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=timeout,
                                       context=context or ssl.create_default_context())
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    got: dict = {}

    def ask() -> None:
        try:
            conn.request("GET", target, headers={"Authorization": f"Bearer {bearer}",
                                                 "User-Agent": USER_AGENT})
            r = conn.getresponse()
            got["status"] = r.status
            got["header"] = r.getheader(header) if header else None
        except (socket.timeout, TimeoutError):
            got["timeout"] = True
        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
            got["error"] = type(e).__name__          # the class only, a message could repeat the request

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        conn.close()
        return Answer(None, None, f"timed out after {timeout:g} s")
    conn.close()
    if "timeout" in got:
        return Answer(None, None, f"timed out after {timeout:g} s")
    if "error" in got:
        return Answer(None, None, f"{host} did not answer ({got['error']})")
    return Answer(got["status"], got["header"], f"{got['status']} from {host}")
