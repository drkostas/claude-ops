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

`stall` is the other kind of watch: it waits on a session's transcript (often a subagent's) and
says whether the session finished its turn, stopped on an error, or stalled, so whoever started it
hears about a session that stopped moving instead of waiting for it for ever.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from .inject import inject
from .update import last_message


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
    send(target, text, transport=...) carries the prompt and returns {"result": "sent"} once it is
    handed over: the default pastes it into the chat (inject); pass your own to hand it to a message
    queue the chats read from, which keeps it until the chat is free and sends one copy of the same
    text while it is open. The seen-set advances only on "sent".
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


#: the exit code of `claude-ops stall` for each result of `stall`
STALL_EXIT = {"finished": 0, "done": 0, "gone": 0, "stalled": 1, "error": 1, "timeout": 2}


def _content(d: dict) -> list:
    c = (d.get("message") or {}).get("content")
    return c if isinstance(c, list) else []


def turn_state(transcript: Path) -> str | None:
    """Where the session in this transcript stands, from its newest message record.

    "ended" (an assistant message with stop_reason end_turn), "error" (Claude Code's own record of
    an API error, such as a usage limit, after which the session waits), "tool call" (an assistant
    message that asked for a tool, with no result yet), "tool result" (a tool answered and the
    model has not), "answer" (assistant text with no end of turn recorded), "message" (a user
    message with no answer yet), or None when the transcript holds no message."""
    d = last_message(transcript)
    if d is None:
        return None
    kinds = {c.get("type") for c in _content(d) if isinstance(c, dict)}
    if d.get("type") == "assistant":
        if d.get("isApiErrorMessage"):
            return "error"
        if "tool_use" in kinds:
            return "tool call"
        stop = (d.get("message") or {}).get("stop_reason")
        return "ended" if stop == "end_turn" else "answer"
    if d.get("type") == "user" and "tool_result" in kinds:
        return "tool result"
    return "message"


def _error_text(transcript: Path) -> str:
    d = last_message(transcript) or {}
    text = " ".join(str(c.get("text", "")) for c in _content(d) if isinstance(c, dict)).strip()
    return " ".join(text.split())[:160]


def _transcripts(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.rglob("*.jsonl") if p.is_file())
    return [path] if path.suffix == ".jsonl" else []


def _newest_change(path: Path) -> float | None:
    """The newest modification time of the file, or of any file under the folder (the folder's own
    when it holds none), or None when it cannot be read."""
    try:
        if not path.is_dir():
            return path.stat().st_mtime
        times = []
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    times.append(p.stat().st_mtime)
            except OSError:
                continue
        return max(times) if times else path.stat().st_mtime
    except OSError:
        return None


_WAITING_ON = {"tool call": "a tool call with no result", "tool result": "a tool result with no answer",
               "answer": "an answer with no end of turn", "message": "a message with no answer"}


def stall_check(path: Path, minutes: float = 30, done_file: Path | None = None,
                now: float | None = None) -> dict | None:
    """One look at a transcript, or a folder of them: a result as `stall` gives it, or None while
    the session is still moving.

    In this order: the done file exists and is not empty ("done"), the path is gone ("gone"), the
    transcript ended its turn ("finished", and for a folder every transcript in it did), the
    transcript stopped on an API error ("error", and for a folder one did and none is still
    working), and nothing under the path changed for `minutes` ("stalled")."""
    path = Path(path)
    now = time.time() if now is None else now
    if done_file is not None:
        try:
            if Path(done_file).stat().st_size > 0:
                return {"result": "done", "path": str(path), "line": f"done: {done_file} is written"}
        except OSError:
            pass
    if not path.exists():
        return {"result": "gone", "path": str(path), "line": f"gone: {path} no longer exists"}
    states = {t: turn_state(t) for t in _transcripts(path)}
    states = {t: s for t, s in states.items() if s is not None}
    if states and all(s == "ended" for s in states.values()):
        line = (f"finished: all {len(states)} transcripts in {path} ended their turn" if path.is_dir()
                else f"finished: {path} ended its turn")
        return {"result": "finished", "path": str(path), "line": line}
    failed = [t for t, s in states.items() if s == "error"]
    if failed and all(s in ("ended", "error") for s in states.values()):
        where = failed[0] if not path.is_dir() else f"{failed[0].name} in {path}"
        return {"result": "error", "path": str(path),
                "line": f"stopped on an error: {where}: {_error_text(failed[0])}"}
    changed = _newest_change(path)
    if changed is None:
        return {"result": "gone", "path": str(path), "line": f"gone: {path} can no longer be read"}
    age = now - changed
    if age < minutes * 60:
        return None
    line = f"STALLED: {path} unchanged for {int(age // 60)} minutes"
    if not path.is_dir() and states.get(path) in _WAITING_ON:
        line += f", its last record is {_WAITING_ON[states[path]]}"
    elif path.is_dir() and states:
        working = sum(1 for s in states.values() if s not in ("ended", "error"))
        line += f", {working} of {len(states)} transcripts have not ended their turn"
    return {"result": "stalled", "path": str(path), "line": line}


def stall(path: Path, minutes: float = 30, max_hours: float = 12, done_file: Path | None = None,
          poll_s: float = 60, *, now: Callable[[], float] = time.time,
          sleep: Callable[[float], None] = time.sleep) -> dict:
    """Wait on a session's transcript until it finishes, stops, or stalls.

    `path` is a transcript (`.jsonl`), a folder of them (a session's `subagents` folder, say), or
    any file or folder a job keeps writing. It is checked every `poll_s` seconds with `stall_check`,
    and the answer is a dict with "result" (finished, done, gone, error, stalled or timeout), "path"
    and "line", one line of text that says what happened. "timeout" means it was still changing
    after `max_hours`. `done_file`, when given, is a file the job writes when it is done, which ends
    the wait as soon as it is not empty. `now` and `sleep` are the clock, replaceable in tests.
    `STALL_EXIT` maps each result to the command's exit code."""
    deadline = now() + max_hours * 3600
    while True:
        r = stall_check(path, minutes, done_file, now())
        if r is not None:
            return r
        if now() >= deadline:
            return {"result": "timeout", "path": str(path),
                    "line": f"still changing after {max_hours:g} hours: {path}"}
        sleep(poll_s)
