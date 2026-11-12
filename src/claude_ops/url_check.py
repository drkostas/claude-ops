"""Whether an https address answers 200 now, in one short GET.

For a job that waits until a page exists (a package's listing, a release page, a badge), so it can
ask once on each pass instead of someone rechecking it every day by hand. One request, with a short
timeout so a slow site cannot hold the caller, and nothing of the caller's sent with it.

- Only https. A plain http address, an address that carries a user name or password, or one with
  no host is refused before anything is sent (`problem` says why), because it can never be a safe
  answer.
- Redirects are followed only to https. A redirect to any other scheme is an answer of its own
  ("redirected to a non-https address"), not a pass.
- No cookies and no credentials. The request carries only a plain User-Agent.
- Only a 200 passes. Any other status, a timeout or a connection error is (False, why), and the why
  names it ("404 from example.org", "timed out after 10 s"), so whatever waits on it can say why.

    from claude_ops import url_check
    ok, why = url_check.ok("https://example.org/")
"""
from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit

#: seconds one request may take, connection included
TIMEOUT_S = 10.0
USER_AGENT = "claude-ops url_check"


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


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only when it stays on https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise _LeftHttps(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _LeftHttps(Exception):
    def __init__(self, newurl: str):
        super().__init__(newurl)
        self.newurl = newurl


def _opener(context: "ssl.SSLContext | None"):
    # build_opener adds no cookie handler, so nothing a site sets is kept or sent back
    return urllib.request.build_opener(_HttpsOnly(), urllib.request.HTTPSHandler(context=context))


def ok(url: str, *, timeout: float = TIMEOUT_S, context: "ssl.SSLContext | None" = None) -> tuple[bool, str]:
    """(True, why) when `url` answers 200 now, else (False, why). Raises NotHttps for an address
    `problem` refuses. `context` is the TLS context (the system's trust store when None)."""
    why = problem(url)
    if why:
        raise NotHttps(why)
    url = url.strip()
    host = urlsplit(url).hostname
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    try:
        with _opener(context).open(req, timeout=timeout) as r:
            status = r.status
    except _LeftHttps as e:
        return False, f"redirected to a non-https address, {e.newurl}"
    except urllib.error.HTTPError as e:
        return False, f"{e.code} from {host}"
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            return False, f"timed out after {timeout:g} s"
        return False, f"{host} did not answer ({e.reason})"
    except (socket.timeout, TimeoutError):
        return False, f"timed out after {timeout:g} s"
    except (OSError, ssl.SSLError) as e:
        return False, f"{host} did not answer ({type(e).__name__}: {e})"
    return status == 200, f"{status} from {host}"
