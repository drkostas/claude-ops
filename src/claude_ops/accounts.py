"""accounts: the Claude login a Mac's chats share, the usage of each account, and when to switch.

Every Claude Code chat on a Mac reads one login, a keychain item. A person with two accounts can keep
a copy of each account's login in an item of its own, read each account's usage with its own token,
and write the other copy into the live item when the one in use runs out. Running chats move to the
new account by themselves; their Remote Control sessions do not, and need `/remote-control` again.

⛔ NEVER RENEW THE LOGIN IN USE. A renewal replaces the refresh token, and the program holding the
old one is signed out. Renew only a saved copy nobody else is using.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Mapping

LIVE_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BETA = "oauth-2025-04-20"
#: an account is spent at this percent of either its 5-hour or its weekly limit
THRESHOLD = 98.0
_LIMIT = re.compile(r"hit your (?:(session|weekly) )?limit", re.I)


def read_item(service: str, account: str, *, run=subprocess.run,
              env: Mapping[str, str] | None = None) -> dict | None:
    """A keychain item's JSON, or None when it is missing or not JSON."""
    r = run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


#: the longest line `security -i` reads whole; a longer one is cut, and the item is saved cut
STDIN_LIMIT = 4032


def write_item(service: str, account: str, data: dict, *, run=subprocess.run,
               env: Mapping[str, str] | None = None) -> bool:
    """Create or replace a keychain item, and read it back. The secret goes to `security -i` on
    stdin, so it is never on a command line another process can read.

    It is sent as quoted text, not hex: a Claude login is about 2,000 characters of JSON, twice that
    in hex, and `security -i` cuts a line longer than STDIN_LIMIT, which saved a cut login. A login
    too long even as text is refused (ValueError), never sent on a command line instead."""
    text = json.dumps(data, separators=(",", ":"))
    quoted = text.replace("\\", "\\\\").replace('"', '\\"')
    line = f'add-generic-password -U -s "{service}" -a "{account}" -w "{quoted}"\n'
    if len(line) > STDIN_LIMIT:
        raise ValueError(f"the item is {len(text)} characters, too long for security -i; "
                         "it is never written on a command line")
    r = run(["security", "-i"], input=line, capture_output=True, text=True, env=env)
    return r.returncode == 0 and read_item(service, account, run=run, env=env) == data


def _get(url: str, token: str, fetch) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "anthropic-beta": BETA})
    with fetch(req, timeout=15) as r:
        return json.loads(r.read())


def _when(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s) if s else None


def usage(token: str, *, fetch=urllib.request.urlopen) -> dict:
    """{"session": (percent, resets_at), "weekly": (percent, resets_at)} for the token's account."""
    d = _get(USAGE_URL, token, fetch)
    out = {}
    for key, name in (("five_hour", "session"), ("seven_day", "weekly")):
        w = d.get(key) or {}
        out[name] = (float(w.get("utilization") or 0.0), _when(w.get("resets_at")))
    return out


def profile(token: str, *, fetch=urllib.request.urlopen) -> dict:
    """{"email", "uuid", "org"} of the token's account."""
    d = _get(PROFILE_URL, token, fetch)
    account, org = d.get("account") or {}, d.get("organization") or {}
    return {"email": account.get("email"), "uuid": account.get("uuid"), "org": org.get("uuid")}


def renew(login: dict, *, fetch=urllib.request.urlopen, now: dt.datetime | None = None) -> dict:
    """The login renewed with its refresh token (never the login in use, see the module's note)."""
    o = dict(login["claudeAiOauth"])
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": o["refreshToken"],
                       "client_id": CLIENT_ID, "scope": " ".join(o.get("scopes") or [])}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with fetch(req, timeout=15) as r:
        t = json.loads(r.read())
    now = now or dt.datetime.now(dt.timezone.utc)
    o.update(accessToken=t["access_token"], refreshToken=t.get("refresh_token", o["refreshToken"]),
             expiresAt=int((now.timestamp() + int(t.get("expires_in", 0))) * 1000))
    return {**login, "claudeAiOauth": o}


def expired(login: dict, now: dt.datetime, margin_s: int = 600) -> bool:
    """Whether the login's access token has expired, or will within `margin_s`."""
    exp = (login.get("claudeAiOauth") or {}).get("expiresAt") or 0
    return exp / 1000 <= now.timestamp() + margin_s


def usable_at(u: dict | None, now: dt.datetime, threshold: float = THRESHOLD) -> dt.datetime | None:
    """When the account can next be used: now while both limits are under `threshold`, otherwise the
    latest reset among the limits at or over it. None when its usage is unknown."""
    if not u:
        return None
    full = [resets for pct, resets in u.values() if pct >= threshold]
    if not full:
        return now
    if any(r is None for r in full):
        return None
    return max(full)


def choose(current: str, usages: dict[str, dict | None], now: dt.datetime, *, hit: bool = False,
           threshold: float = THRESHOLD) -> str | None:
    """The account to switch to now, or None. The one in use is spent when it is at the threshold or
    a chat hit its limit (`hit`). Switch only to an account usable now, so only when it is usable
    sooner; an account whose usage is unknown is never chosen."""
    mine = usable_at(usages.get(current), now, threshold)
    if not hit and (mine is None or mine <= now):
        return None                  # usable, or unknown: nothing says it is spent
    best = None
    for name, u in usages.items():
        if name == current:
            continue
        at = usable_at(u, now, threshold)
        if at is not None and at <= now and (best is None or at < best[1]):
            best = (name, at)
    return best[0] if best else None


def limit_hit(transcript: Path, scan: int = 400) -> dict | None:
    """{"kind", "text", "at"} when the conversation's newest message is a usage-limit stop: Claude
    Code's API error record saying the session, the weekly or the plain limit was hit."""
    from claude_ops.update import ACTIVITY, _lines_from_end, _text_of
    try:
        for n, line in enumerate(_lines_from_end(Path(transcript))):
            if n >= scan:
                return None
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") not in ACTIVITY or d.get("isSidechain"):
                continue
            if d.get("type") != "assistant" or not d.get("isApiErrorMessage"):
                return None
            text = _text_of(d)
            m = _LIMIT.search(text)
            return {"kind": m.group(1) or "limit", "text": text.strip(), "at": d.get("timestamp")} if m else None
    except OSError:
        return None
    return None
