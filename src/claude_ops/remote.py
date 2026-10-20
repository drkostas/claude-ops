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
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CODE_URL = "https://claude.ai/code"
TAIL = 512 * 1024
_BRIDGE = re.compile(r'"bridgeSessionId"\s*:\s*"cse_([A-Za-z0-9]+)"')
_OWNER = re.compile(r'"ownerAccountUuid"\s*:\s*"([0-9A-Za-z-]+)"')
#: what rename_js answers when the list has no row for the session
NOT_LISTED = "its row is not in the list"
#: what rename_js answers when the page is signed in to another account than the session's
OTHER_ACCOUNT = "the page is signed in to another account"


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
