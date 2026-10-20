"""A chat's Remote Control session, and its title on claude.ai.

A chat started with Remote Control appears on claude.ai and the phone under a title of its own. A
resume reattaches to the chat's existing Remote Control session, which keeps the title it was first
made with, and neither `--remote-control <name>` nor `/rename` changes it: the transcript's title
changes and the claude.ai list does not. What does change it is the claude.ai Code list itself, the
row of the session, its menu, Rename.

`remote_id` reads which Remote Control session a transcript belongs to (its newest
`bridgeSessionId`, as the Code list names it), and `remote_session` also which account owns it.
`rename_js` is the page script that renames that row on https://claude.ai/code and reads the title
back; run it in a signed-in browser (claude_human.brave.run is one way). A restart can make a new
Remote Control session, so remember the title per session, not per chat.

The Code list shows only the sessions of the account the page is signed in to. When the Mac's chats
move to another Claude account, their new sessions are never in that list, and a rename that only
waits for the row reports it missing for ever. Give `rename_js` the session's account and it checks
the page's account first, answering `OTHER_ACCOUNT` instead.

No page is needed at all with the account's own login. `set_title(row, name, token)` puts the title
through the sessions API that Claude Code itself uses (`PUT /v1/code/sessions/<id>`), with the OAuth
access token of the account that owns the session, and reads it back with `title`. It works for any
account whose login is at hand, whichever account a browser is signed in to, and opens no window.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

CODE_URL = "https://claude.ai/code"
API_URL = "https://api.anthropic.com"
BETA = "oauth-2025-04-20"
TAIL = 512 * 1024
_BRIDGE = re.compile(r'"bridgeSessionId"\s*:\s*"cse_([A-Za-z0-9]+)"')
_OWNER = re.compile(r'"ownerAccountUuid"\s*:\s*"([0-9A-Za-z-]+)"')
#: what rename_js answers when the list has no row for the session
NOT_LISTED = "its row is not in the list"
#: what rename_js answers when the page is signed in to another account than the session's
OTHER_ACCOUNT = "the page is signed in to another account"
#: what set_title answers when the login cannot see the session (another account's, or gone)
NOT_FOUND = "the login does not see this session"
#: what set_title answers when the login was refused (expired or revoked)
REFUSED = "the login was refused"


def remote_session(transcript: Path | str | None, tail: int = TAIL) -> dict | None:
    """The chat's Remote Control session from the newest record with a `bridgeSessionId` ("cse_<X>")
    in the transcript's last `tail` bytes: {"row": "session_<X>" as the claude.ai list names it,
    "account": the record's ownerAccountUuid or None}. None without such a record."""
    if transcript is None:
        return None
    try:
        with open(transcript, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [ln for ln in text.splitlines() if _BRIDGE.search(ln)]
    if not lines:
        return None
    owner = _OWNER.search(lines[-1])
    return {"row": f"session_{_BRIDGE.findall(lines[-1])[-1]}",
            "account": owner.group(1) if owner else None}


def remote_id(transcript: Path | str | None, tail: int = TAIL) -> str | None:
    """The chat's Remote Control session as the claude.ai list names it ("session_<X>"), or None."""
    found = remote_session(transcript, tail)
    return found["row"] if found else None


def account_js() -> str:
    """The page script that reads which account a claude.ai page is signed in to: {account} (its
    uuid, or null when the page could not say). Compare it with remote_session's account before
    renaming, so a session of another account is not looked for in a list that cannot show it."""
    return """(async () => {
  try {
    const r = await fetch('/api/account', {credentials: 'include'});
    return {account: (await r.json()).uuid || null};
  } catch (e) { return {account: null}; }
})()"""


def rename_js(row_id: str, name: str, account: str | None = None) -> str:
    """The page script that renames one row of the claude.ai Code list and reads its title back:
    {ok, label, changed} or {ok: false, why}. A row already titled `name` is left alone. With
    `account` (the session's owner, from remote_session), a page signed in to another account
    answers {ok: false, why: OTHER_ACCOUNT, signed_in} before it waits for a row it cannot show.
    The id, the name and the account go in as JSON, so none can break out of the script."""
    return """(async () => {
  const ID = %s, NAME = %s, ACCOUNT = %s, OTHER = %s, NOT_LISTED = %s;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  if (ACCOUNT) {
    let mine = null;
    try { mine = (await (await fetch('/api/account', {credentials: 'include'})).json()).uuid; } catch (e) {}
    if (mine && mine !== ACCOUNT) return {ok: false, why: OTHER, signed_in: mine};
  }
  const row = () => document.querySelector('a[href="/code/' + ID + '"]');
  const label = () => ((row() || document).querySelector('[data-row-label]') || {}).textContent;
  for (let i = 0; i < 60 && !row(); i++) await sleep(250);
  if (!row()) return {ok: false, why: NOT_LISTED};
  // the row's label holds the title, then its folder and status ("build app · Connected")
  const named = t => { t = (t || '').trim(); return t === NAME || t.startsWith(NAME + ' '); };
  if (named(label())) return {ok: true, label: NAME, changed: false};
  const btn = row().closest('[data-row]').querySelector('button[aria-haspopup="menu"]');
  btn.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
  btn.click();
  await sleep(700);
  const item = [...document.querySelectorAll('[role="menuitem"]')]
    .find(e => e.textContent.trim().startsWith('Rename'));
  if (!item) return {ok: false, why: 'its menu has no Rename'};
  item.click();
  await sleep(700);
  const input = document.activeElement;
  if (!input || input.tagName !== 'INPUT') return {ok: false, why: 'no name field opened'};
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, NAME);
  input.dispatchEvent(new Event('input', {bubbles: true}));
  await sleep(200);
  input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
  await sleep(1500);
  const now = (label() || '').trim();
  return {ok: named(now), label: now, changed: true};
})()""" % (json.dumps(row_id), json.dumps(name), json.dumps(account), json.dumps(OTHER_ACCOUNT),
             json.dumps(NOT_LISTED))


# ------------------------------------------------------------------------------------ the API

def api_id(row: str) -> str:
    """The sessions API's id for a row: "session_<X>" becomes "cse_<X>"; any other id is kept."""
    return "cse_" + row[len("session_"):] if row.startswith("session_") else row


def _call(method: str, row: str, token: str, body: dict | None, fetch, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{API_URL}/v1/code/sessions/{api_id(row)}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "anthropic-version": "2023-06-01", "anthropic-beta": BETA})
    try:
        with fetch(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {}


def _why(status: int) -> str:
    if status == 404:
        return NOT_FOUND
    if status in (401, 403):
        return REFUSED
    return f"the sessions API answered {status}"


def title(row: str, token: str, *, fetch=urllib.request.urlopen, timeout: float = 15) -> dict:
    """The session's title through the sessions API: {"ok": True, "title"} or {"ok": False, "why"}.
    `token` is the OAuth access token of the account that owns the session; it is sent only in the
    Authorization header and never kept."""
    status, body = _call("GET", row, token, None, fetch, timeout)
    if status != 200:
        return {"ok": False, "why": _why(status)}
    return {"ok": True, "title": (body.get("response_shape") or body).get("title")}


def set_title(row: str, name: str, token: str, *, fetch=urllib.request.urlopen,
              timeout: float = 15) -> dict:
    """Give the session the title `name` and read it back: {"ok", "title", "changed"} or
    {"ok": False, "why"}. A session already titled `name` is left alone (changed False)."""
    now = title(row, token, fetch=fetch, timeout=timeout)
    if not now["ok"] or now["title"] == name:
        return {**now, "changed": False} if now["ok"] else now
    status, _ = _call("PUT", row, token, {"title": name}, fetch, timeout)
    if status != 200:
        return {"ok": False, "why": _why(status)}
    after = title(row, token, fetch=fetch, timeout=timeout)
    if not after["ok"]:
        return after
    return {"ok": after["title"] == name, "title": after["title"], "changed": True}
