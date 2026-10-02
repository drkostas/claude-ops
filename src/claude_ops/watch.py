"""Building blocks for watchers that wake a Claude chat only when something new happened.

The pattern (proven over months of use): a scheduler (launchd, cron, systemd) runs a short script
every few minutes. The script checks a source (a repo, an inbox, an API) for items it has not seen,
and only when there is something new does it send one prompt into a named, running Claude chat.
Nothing is sent when nothing changed, and the user is not notified separately.

Rules this module enforces, each learned from a watcher that went wrong:
- The seen-set advances only after the prompt was delivered. If the chat is closed or the send
  fails, the same items are offered again next run, so nothing is dropped.
- The first run records what is already there and sends nothing, so history is not replayed.
- A lock stops two scheduled runs from both firing.
- A source that keeps failing (expired token, blocked IP) raises one alert into the chat after a
  few runs, instead of going silent.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from .inject import inject


class State:
    """Per-watcher state in one JSON file: the seen ids per source, and failure counts."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"seen": {}, "fails": {}, "alerted": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    def first_run(self, source: str) -> bool:
        return source not in self.data["seen"]

    def new_items(self, source: str, ids: list[str]) -> list[str]:
        seen = set(self.data["seen"].get(source, []))
        return [i for i in ids if i not in seen]

    def mark_seen(self, source: str, ids: list[str], keep: int = 5000) -> None:
        merged = list(dict.fromkeys(self.data["seen"].get(source, []) + list(ids)))
        self.data["seen"][source] = merged[-keep:]


@contextmanager
def lock(path: Path):
    """An exclusive lock directory with the owner's pid. A stale lock (dead pid) is taken over."""
    path = Path(path)
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        try:
            pid = int((path / "pid").read_text())
            os.kill(pid, 0)
            yield False
            return
        except (OSError, ValueError):
            pass  # the owner is gone
    (path / "pid").write_text(str(os.getpid()))
    try:
        yield True
    finally:
        (path / "pid").unlink(missing_ok=True)
        try:
            path.rmdir()
        except OSError:
            pass


def run_source(state: State, source: str, fetch, describe, target: str, *, transport: str = "iterm",
               alert_after: int = 5, send=inject) -> dict:
    """Check one source and wake the chat if it has new items.

    fetch() returns a list of item ids (or raises). describe(new_ids) returns the prompt text.
    """
    try:
        ids = list(fetch())
    except Exception as e:  # noqa: BLE001 - any failure of the source is counted
        n = state.data["fails"].get(source, 0) + 1
        state.data["fails"][source] = n
        if n >= alert_after and not state.data["alerted"].get(source):
            r = send(target, f"[watcher] {source} has failed {n} runs in a row: {type(e).__name__}: {e}", transport=transport)
            if r.get("result") == "sent":
                state.data["alerted"][source] = True
        state.save()
        return {"source": source, "result": "fetch-failed", "fails": n}
    state.data["fails"][source] = 0
    state.data["alerted"][source] = False
    if state.first_run(source):
        state.mark_seen(source, ids)
        state.save()
        return {"source": source, "result": "baseline", "items": len(ids)}
    new = state.new_items(source, ids)
    if not new:
        state.save()
        return {"source": source, "result": "nothing-new"}
    r = send(target, describe(new), transport=transport)
    if r.get("result") == "sent":
        state.mark_seen(source, new)
    state.save()
    return {"source": source, "result": r.get("result"), "new": len(new)}
