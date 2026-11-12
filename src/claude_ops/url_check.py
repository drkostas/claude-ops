"""Whether an https address answers 200 now, in one short GET.

For a job that waits until a page exists (a package's listing, a release page, a badge), so it can
ask once on each pass instead of someone rechecking it every day by hand. One request, with a short
deadline so a slow site cannot hold the caller, and nothing of the caller's sent with it.

- Only https. A plain http address, an address that carries a user name or password, or one with
  no host is refused before anything is sent (`problem` says why), because it can never be a safe
  answer.
- Redirects are followed only to https, and at most `MAX_REDIRECTS` of them. A redirect to any other
  scheme is an answer of its own ("redirected to a non-https address"), not a pass.
- The timeout is a total for the whole answer, redirects included. A server that sends one byte at a
  time, or a chain of redirects, is cut off when it runs out.
- No cookies and no credentials. The request carries only a plain User-Agent.
- Only a 200 passes. Any other status, a timeout or a connection error is (False, why), and the why
  names it ("404 from example.org", "timed out after 10 s"), so whatever waits on it can say why.

    from claude_ops import url_check
    ok, why = url_check.ok("https://example.org/")
"""
from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

#: seconds the whole answer may take, every connection, redirect and read included
TIMEOUT_S = 10.0
#: redirects followed before giving up
MAX_REDIRECTS = 5
USER_AGENT = "claude-ops url_check"
_REDIRECTS = {301, 302, 303, 307, 308}


class NotHttps(ValueError):
    """The address can never pass as written (not https, credentials in it, no host)."""


def problem(url: str) -> "str | None":
    """Why `url` can never pass, or None when it may. Nothing is sent."""
    text = (url or "").strip()
    if not text or any(c.isspace() for c in text):
        return f"wants an https address, not {url!r}"
    try:
        parts = urlsplit(text)
        parts.port                     # noqa: B018 - a port that is not a number raises here
    except ValueError as e:
        return f"cannot read {url!r} as an address ({e})"
    if not parts.scheme:
        return f"wants an https address, not {url!r}"
    if parts.scheme != "https":
        return f"takes only https addresses, not {parts.scheme} ({url!r})"
    if not parts.hostname:
        return f"wants a host in {url!r}"
    if parts.username is not None or parts.password is not None:
        return "takes no user name or password in the address"
    return None


def _one(url: str, left: float, context: "ssl.SSLContext | None") -> dict:
    """One GET of `url`, given at most `left` seconds in all: {"status", "location"}, {"timeout"}
    or {"error"}. The request runs in a thread, and when the time is up its socket is shut, so a
    server that trickles its answer cannot keep it."""
    parts = urlsplit(url)
    conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=left,
                                       context=context or ssl.create_default_context())
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    got: dict = {}

    def ask() -> None:
        try:
            conn.request("GET", target, headers={"User-Agent": USER_AGENT})
            r = conn.getresponse()
            got.update(status=r.status, location=r.getheader("Location"))
        except (socket.timeout, TimeoutError):
            got.update(timeout=True)
        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
            got.update(error=f"{type(e).__name__}: {e}")

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    worker.join(left)
    if worker.is_alive():
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        conn.close()
        return {"timeout": True}
    conn.close()
    return got


def ok(url: str, *, timeout: float = TIMEOUT_S, context: "ssl.SSLContext | None" = None,
       redirects: int = MAX_REDIRECTS) -> tuple[bool, str]:
    """(True, why) when `url` answers 200 now, else (False, why), within `timeout` seconds in all.
    Raises NotHttps for an address `problem` refuses. `context` is the TLS context (the system's
    trust store when None)."""
    why = problem(url)
    if why:
        raise NotHttps(why)
    current = url.strip()
    deadline = time.monotonic() + timeout
    for _ in range(redirects + 1):
        left = deadline - time.monotonic()
        if left <= 0:
            return False, f"timed out after {timeout:g} s"
        host = urlsplit(current).hostname
        got = _one(current, left, context)
        if got.get("timeout"):
            return False, f"timed out after {timeout:g} s"
        if "error" in got:
            return False, f"{host} did not answer ({got['error']})"
        status = got["status"]
        if status in _REDIRECTS and got.get("location"):
            try:
                nxt = urljoin(current, got["location"])
            except ValueError as e:
                return False, f"redirected to an address it cannot read ({e})"
            if urlsplit(nxt).scheme != "https":
                return False, f"redirected to a non-https address, {nxt}"
            # a target is held to the same rules as the first address, so a bad one is an answer
            why = problem(nxt)
            if why:
                return False, f"redirected to an address it refuses ({why})"
            current = nxt
            continue
        return status == 200, f"{status} from {host}"
    return False, f"more than {redirects} redirect{'' if redirects == 1 else 's'}"
