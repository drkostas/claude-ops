"""accounts: the Claude login a Mac's chats share, the usage of each account, and when to switch.

Every Claude Code chat on a Mac reads one login, a keychain item. A person with two accounts can keep
a copy of each account's login in an item of its own, read each account's usage with its own token,
and write the other copy into the live item when the one in use runs out. Running chats move to the
new account by themselves; their Remote Control sessions do not, and need `/remote-control` again.

⛔ NEVER RENEW THE LOGIN IN USE. A renewal replaces the refresh token, and the program holding the
old one is signed out. Renew only a saved copy nobody else is using.

⚠️ THE KEYCHAIN IS NOT THE WHOLE LOGIN. Claude Code also keeps who is signed in, without any secret,
in the `oauthAccount` block of its config file (`~/.claude.json`): the account and organization ids,
the email, the profile. `/login` writes both; a program that writes only the keychain item leaves the
config naming the old account, and Claude Code then records the old account as the owner of every new
Remote Control session while the session runs on the new one, and `config.login_digest` does not move.
`read_account` and `write_account` keep that block in step (see them for how the file is written while
running Claude Code processes rewrite it).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Mapping

from . import __version__
from .config import CLAUDE_JSON

LIVE_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BETA = "oauth-2025-04-20"
#: every request names itself. The token endpoint sits behind Cloudflare, which refuses Python's own
#: "Python-urllib/3.x" with 403 (error code 1010, a banned client signature), so a renewal without
#: this header failed every time while usage and profile, on another host, still answered
USER_AGENT = f"claude-ops/{__version__}"
#: an account is spent at this percent of either its 5-hour or its weekly limit
THRESHOLD = 98.0
_LIMIT = re.compile(r"hit your (?:(session|weekly) )?limit", re.I)


#: `security find-generic-password`'s exit code when no such item exists (errSecItemNotFound)
NOT_FOUND = 44


class Unreadable(RuntimeError):
    """The keychain did not answer (locked, access refused, the command failed). Nothing is known
    about the item, which is not the same as the item being missing."""


class RateLimited(RuntimeError):
    """Anthropic answered a usage or profile read with 429. `retry_after_s` is the Retry-After
    header in seconds (None when it was missing or unreadable), and `until(now)` the time the read
    is allowed again. A caller that keeps asking every few minutes keeps the limit going, and says
    nothing about when it ends; one that waits `retry_after_s` does neither."""

    def __init__(self, retry_after_s: "int | None", url: str = ""):
        self.retry_after_s = retry_after_s
        self.url = url
        after = f"retry after {retry_after_s} s" if retry_after_s is not None else "no Retry-After"
        super().__init__(f"HTTP Error 429: rate limited ({after})")

    def until(self, now: "dt.datetime | None" = None) -> "dt.datetime | None":
        if self.retry_after_s is None:
            return None
        return (now or dt.datetime.now(dt.timezone.utc)) + dt.timedelta(seconds=self.retry_after_s)


def retry_after_seconds(value: "str | None", now: "dt.datetime | None" = None) -> "int | None":
    """The seconds a Retry-After header asks for: a number of seconds, or an HTTP date read against
    `now` (never negative). None when the header is missing or unreadable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    return max(0, int((when - now).total_seconds()))


def read_item(service: str, account: str, *, run=subprocess.run,
              env: Mapping[str, str] | None = None, strict: bool = False) -> dict | None:
    """A keychain item's JSON, or None when it is missing or not JSON.

    With `strict`, a read that fails for any reason other than the item not existing raises
    Unreadable, so a caller can tell "there is no such login" from "the keychain did not answer".
    A monitor that reads the second as the first says a login is gone when it only could not look."""
    r = run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, env=env)
    if r.returncode != 0:
        if strict and r.returncode != NOT_FOUND:
            raise Unreadable((r.stderr or "").strip()[:200] or f"security exited {r.returncode}")
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
    """One authenticated GET. A 429 is raised as RateLimited with its Retry-After, so the caller can
    wait that long instead of asking again; every other HTTP error is raised as it came."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "anthropic-beta": BETA,
                                                  "User-Agent": USER_AGENT})
    try:
        with fetch(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            header = e.headers.get("Retry-After") if e.headers is not None else None
            raise RateLimited(retry_after_seconds(header), url) from None
        raise


def _when(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s) if s else None


def usage(token: str, *, fetch=urllib.request.urlopen) -> dict:
    """{"session": (percent, resets_at), "weekly": (percent, resets_at)} for the token's account.
    Raises RateLimited (with the seconds to wait) when Anthropic answers 429."""
    d = _get(USAGE_URL, token, fetch)
    out = {}
    for key, name in (("five_hour", "session"), ("seven_day", "weekly")):
        w = d.get(key) or {}
        out[name] = (float(w.get("utilization") or 0.0), _when(w.get("resets_at")))
    return out


def login_for(account: str, items: list[tuple[str, str]], *, run=subprocess.run,
              fetch=urllib.request.urlopen, env: Mapping[str, str] | None = None) -> dict | None:
    """The first saved login among `items` ((service, account name) keychain pairs, in order) whose
    profile is the account with uuid `account`, or None. A missing item, or a token the profile
    refuses (expired), is passed over; nothing is renewed here."""
    for service, name in items:
        login = read_item(service, name, run=run, env=env)
        token = ((login or {}).get("claudeAiOauth") or {}).get("accessToken")
        if not token:
            continue
        try:
            if profile(token, fetch=fetch)["uuid"] == account:
                return login
        except Exception:  # noqa: BLE001 - a refused token says nothing about whose it is
            continue
    return None


def profile(token: str, *, fetch=urllib.request.urlopen) -> dict:
    """{"email", "uuid", "org"} of the token's account. Raises RateLimited on a 429, as `usage` does."""
    d = _get(PROFILE_URL, token, fetch)
    account, org = d.get("account") or {}, d.get("organization") or {}
    return {"email": account.get("email"), "uuid": account.get("uuid"), "org": org.get("uuid")}


def renew(login: dict, *, fetch=urllib.request.urlopen, now: dt.datetime | None = None) -> dict:
    """The login renewed with its refresh token (never the login in use, see the module's note)."""
    o = dict(login["claudeAiOauth"])
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": o["refreshToken"],
                       "client_id": CLIENT_ID, "scope": " ".join(o.get("scopes") or [])}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
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


#: an account is switched to only while both its limits are under this share: one near its weekly
#: limit would be spent by the switch and then locked for days
FLOOR = 90.0
#: a block on the session limit alone that resets within this is waited out, not switched away from:
#: a switch moves every chat, and an hour's pause costs less than that
SHORT_WAIT = dt.timedelta(minutes=60)


@dataclass(frozen=True)
class Decision:
    """What to do about the account in use. `action` is "stay", "switch" (to `account`) or "pause"
    (until `until`, the reset waited for; `account` is then the one whose reset it is, the one to
    switch to at that time when it is not the one in use). `reason` says why in one line."""
    action: str
    account: str | None = None
    until: dt.datetime | None = None
    reason: str = ""


def _share(pct: float, resets: dt.datetime | None, now: dt.datetime) -> float:
    """A limit's share now: a reset that has passed empties it, whatever the last reading said."""
    return 0.0 if resets is not None and resets <= now else pct


def _room(u: dict, now: dt.datetime, floor: float) -> bool:
    return all(_share(pct, r, now) < floor for pct, r in u.values())


def _room_at(u: dict, now: dt.datetime, floor: float) -> dt.datetime | None:
    """When the account has room on both limits: now, or the latest reset among the limits at or
    over `floor`; None when such a reset is unknown."""
    full = [r for pct, r in u.values() if _share(pct, r, now) >= floor]
    if not full:
        return now
    return None if any(r is None for r in full) else max(full)


def _clock(at: dt.datetime | None) -> str:
    return at.strftime("%H:%M") if at else "an unknown time"


def decide(current: str, usages: dict[str, dict | None], now: dt.datetime, *, hit: "bool | str" = False,
           threshold: float = THRESHOLD, floor: float = FLOOR,
           short_wait: dt.timedelta = SHORT_WAIT) -> Decision:
    """The action for the account in use, decided against every account's usage, never from its own
    alone. It is spent when a limit of its is at `threshold`, or a chat hit its limit (`hit`: True,
    or the limit's name, "session" or "weekly", when the stop said which).

    Then, in this order: a block on the session limit alone that resets within `short_wait` is
    waited out (pause); an account with room on both limits (under `floor`) is switched to, the one
    with the most weekly room first; with none, the wait is for the earliest time any account has
    room again, and that account is the one to switch to then. An account whose usage is unknown
    is never switched to, and one in use whose usage is unknown is left alone unless a chat hit
    its limit."""
    mine = usages.get(current)
    if mine is None and not hit:
        return Decision("stay", reason=f"{current}'s usage is unknown")
    spent = {name: r for name, (pct, r) in (mine or {}).items() if _share(pct, r, now) >= threshold}
    if hit and not spent:
        kind = hit if hit in ("session", "weekly") else "session"
        spent = {kind: ((mine or {}).get(kind) or (None, None))[1]}
    if not spent:
        return Decision("stay", reason=f"{current} has room")
    blocked = ", ".join(f"{k} limit" for k in spent)
    others = {n: u for n, u in usages.items() if n != current and u is not None}
    room = sorted((n for n, u in others.items() if _room(u, now, floor)),
                  key=lambda n: (_share(*others[n]["weekly"], now), _share(*others[n]["session"], now)))
    session_only = set(spent) == {"session"}
    reset = spent.get("session")
    if session_only and reset is not None and reset <= now + short_wait:
        return Decision("pause", current, reset,
                        f"{current}'s session limit resets at {_clock(reset)}, within the hour")
    if room:
        u = others[room[0]]
        return Decision("switch", room[0], None,
                        f"{current} is at its {blocked}; {room[0]} has room ({u['session'][0]:.0f}% session, "
                        f"{u['weekly'][0]:.0f}% weekly)")
    # nowhere has room: wait for the earliest time an account has it, the one in use included
    waits = {}
    own = reset if session_only else max((r for r in spent.values() if r is not None), default=None)
    if own is not None:
        waits[current] = own
    for n, u in others.items():
        at = _room_at(u, now, floor)
        if at is not None:
            waits[n] = at
    if not waits:
        return Decision("pause", current, None, f"{current} is at its {blocked}, and no reset time is known")
    name = min(waits, key=waits.get)
    at = waits[name]
    what = "the same account" if name == current else f"{name}, to switch to then"
    return Decision("pause", name, at,
                    f"{current} is at its {blocked} and no account has room; the earliest reset is "
                    f"{what} at {_clock(at)}")


def choose(current: str, usages: dict[str, dict | None], now: dt.datetime, *, hit: "bool | str" = False,
           threshold: float = THRESHOLD) -> str | None:
    """The account `decide` switches to now, or None (stay, or pause for a reset)."""
    d = decide(current, usages, now, hit=hit, threshold=threshold)
    return d.account if d.action == "switch" else None


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


#: the key of a Claude Code config file that names the account signed in (no secret in it)
ACCOUNT_KEY = "oauthAccount"
#: how a block built from `profile` names the account: the config's names for the profile's fields
_FROM_PROFILE = (("accountUuid", "uuid"), ("emailAddress", "email"), ("organizationUuid", "org"))


class ConfigChanged(RuntimeError):
    """The config file changed under every try to write it. Nothing was written."""


def _read_bytes(path: Path) -> bytes:
    """The file's bytes, read in one call. One name, so a test can stand in another writer."""
    return path.read_bytes()


def read_account(path: "os.PathLike | str" = CLAUDE_JSON) -> "dict | None":
    """The account a Claude Code config file says is signed in: its `oauthAccount` block, a copy.
    None when the file is missing, is not a JSON object, or holds no such block."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    block = data.get(ACCOUNT_KEY) if isinstance(data, dict) else None
    return dict(block) if isinstance(block, dict) else None


def account_block(profile: dict) -> dict:
    """An `oauthAccount` block built from `profile(token)`, for an account whose block was never
    kept: the ids and the email, which is what names the account. Claude Code fills in the rest of
    the profile when it next fetches it."""
    return {key: profile.get(field) for key, field in _FROM_PROFILE}


def account_email(block: "dict | None") -> "str | None":
    """The email an `oauthAccount` block names, or None."""
    return (block or {}).get("emailAddress") or None


def write_account(path: "os.PathLike | str", block: dict, *, backup: "os.PathLike | str | None" = None,
                  tries: int = 3) -> dict:
    """Make the config file's `oauthAccount` block equal to `block`, and leave every other key as it
    is. Returns {"changed": whether anything was written, "backup": the copy taken or None,
    "mtime_ns": the file's time afterwards}.

    ⚠️ RUNNING CLAUDE CODE PROCESSES REWRITE THIS FILE EVERY MINUTE OR TWO, so it is written the way
    they could not break: read whole, the block replaced in the parsed object, the result written to
    a temporary file beside it, and only when the file still holds the bytes that were read is the
    temporary file renamed over it (one atomic step, with the original's mode). A file that changed
    in between is read again and the write tried again, `tries` times, then ConfigChanged is raised
    with nothing written. A file whose block already equals `block` is not written at all, and no
    backup is taken. The result is read back before returning, so a "changed" answer means the file
    on disk now holds the block.

    `backup`, when given, receives a copy of the file as it was before the first write (not before
    a write that did not happen). A caller writing a person's real config keeps one.

    A file that is missing or not a JSON object raises ValueError: this writes an account into an
    existing config, it does not make one."""
    path = Path(path)
    for _ in range(max(1, tries)):
        try:
            raw = _read_bytes(path)
        except OSError as e:
            raise ValueError(f"{path} cannot be read: {e.strerror or type(e).__name__}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError(f"{path} is not JSON") from None
        if not isinstance(data, dict):
            raise ValueError(f"{path} does not hold a JSON object")
        if data.get(ACCOUNT_KEY) == block:
            return {"changed": False, "backup": None, "mtime_ns": path.stat().st_mtime_ns}
        data[ACCOUNT_KEY] = dict(block)
        text = json.dumps(data, indent=2) + ("\n" if raw.endswith(b"\n") else "")
        if backup is not None:
            shutil.copy2(path, backup)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, path.stat().st_mode & 0o7777)
            if _read_bytes(path) != raw:
                os.unlink(tmp)
                continue            # another program wrote meanwhile: start from what it wrote
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        if read_account(path) != block:
            raise ConfigChanged(f"{path} does not hold the account just written")
        return {"changed": True, "backup": None if backup is None else str(backup),
                "mtime_ns": path.stat().st_mtime_ns}
    raise ConfigChanged(f"{path} changed under each of {tries} tries; nothing was written")


def changed_since(path: "os.PathLike | str", mtime_ns: int) -> "bool | None":
    """Whether the file has been written since a `write_account` whose result said `mtime_ns`, so
    a caller can read the block again after the next rewrite by a running Claude Code and see
    whether it was kept. None when the file cannot be read."""
    try:
        return Path(path).stat().st_mtime_ns != mtime_ns
    except OSError:
        return None
