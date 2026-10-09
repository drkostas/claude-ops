"""Send a message into a Claude Code chat that is already running, in iTerm2 or tmux.

A chat is found by its iTerm2 session id, by part of its tab name, or by its tmux session name. The
iTerm2 session id is the stable handle: Claude Code rewrites the tab name, so a long-lived watcher
should hold the id. Multi-line text is sent as one bracketed paste, because typed newlines are Enter
and would submit each line as its own message.

Before sending, the target must look like it is running Claude Code. Sending into a plain shell
would run the text as a command. The check fails closed: when unsure, nothing is sent.

Every attempt is written to an append-only log BEFORE sending, and the delivery result after, as a
second line with the same correlation id. A crash in between leaves an attempt with no result,
which is honest. This is a record, not a lock: anything running as your user can type into a
terminal without going through here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

NO_SESSION = 3
NOT_CLAUDE = 4
LOG = Path(os.environ.get("CLAUDE_OPS_INJECT_LOG", Path.home() / ".local" / "state" / "claude-ops" / "inject.jsonl"))
TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
OSASCRIPT = "/usr/bin/osascript"

# The text is an argument, never part of the script, so quotes and backslashes need no escaping.
ITERM_SEND = """on run argv
  set wanted to item 1 of argv
  set payload to item 2 of argv
  set wantEnter to item 3 of argv
  with timeout of 30 seconds
    tell application "iTerm2"
      repeat with w in windows
        repeat with t in tabs of w
          repeat with s in sessions of t
            if (id of s) is wanted then
              tell s to write text payload newline NO
              if wantEnter is "1" then
                delay 0.5
                tell s to write text "" newline YES
              end if
              return "sent"
            end if
          end repeat
        end repeat
      end repeat
      return "no-session"
    end tell
  end timeout
end run
"""

ITERM_LIST = """with timeout of 20 seconds
 tell application "iTerm2"
  set out to ""
  repeat with w in windows
   repeat with t in tabs of w
    repeat with s in sessions of t
     set out to out & (id of s) & (ASCII character 9) & (name of s) & linefeed
    end repeat
   end repeat
  end repeat
  return out
 end tell
end timeout"""


def iterm_running(*, env: Mapping[str, str] | None = None) -> bool:
    # Asking iTerm2 itself would launch it when it is closed, so ask System Events.
    r = subprocess.run([OSASCRIPT, "-e", 'tell application "System Events" to return (name of processes) contains "iTerm2"'],
                       capture_output=True, text=True, env=env)
    return r.returncode == 0 and r.stdout.strip() == "true"


def iterm_sessions(*, env: Mapping[str, str] | None = None) -> dict[str, dict]:
    """{session id: {"name": tab name}}. Empty when iTerm2 is not running, which is not an error."""
    if not iterm_running(env=env):
        return {}
    r = subprocess.run([OSASCRIPT, "-e", ITERM_LIST], capture_output=True, text=True, env=env)
    out = {}
    for line in r.stdout.splitlines():
        if "\t" in line:
            sid, name = line.split("\t", 1)
            if sid.strip():
                out[sid.strip()] = {"name": name.strip()}
    return out


def tmux_sessions(*, env: Mapping[str, str] | None = None) -> dict[str, dict]:
    """{session name: {...}}. Empty when no tmux server runs, which is normal."""
    r = subprocess.run([TMUX, "list-sessions", "-F", "#{session_name}\t#{pane_current_command}\t#{session_attached}"],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.strip().splitlines():
        name, cmd, attached = (line.split("\t") + ["", ""])[:3]
        out[name] = {"name": name, "command": cmd, "attached": attached != "0"}
    return out


def looks_like_claude(transport: str, session: dict) -> bool:
    """Whether the session's foreground program is Claude Code (a node process). Fails closed."""
    if transport == "tmux":
        return _is_claude_command(str(session.get("command") or ""))
    name = str(session.get("name") or "")
    if "(" not in name:
        return False  # iTerm2 shows the foreground job in brackets, e.g. "my-chat (node)"
    return _is_claude_command(name.rsplit("(", 1)[-1].rstrip(")").strip().lstrip("-"))


def _is_claude_command(cmd: str) -> bool:
    # Claude Code names its process after its version, so tmux reports e.g. "2.1.286"
    return cmd in ("node", "claude") or bool(re.fullmatch(r"\d+\.\d+\.\d+", cmd))


def resolve(target: str, live: dict, transport: str) -> str:
    if target in live:
        return target
    if transport == "iterm":
        return next((k for k in sorted(live) if target in live[k].get("name", "")), "")
    return ""


def bracketed(text: str) -> str:
    return f"\x1b[200~{text}\x1b[201~" if "\n" in text else text


def send_iterm(session_id: str, text: str, enter: bool, *, env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    r = subprocess.run([OSASCRIPT, "-", session_id, bracketed(text), "1" if enter else "0"],
                       input=ITERM_SEND, capture_output=True, text=True, env=env)
    ok = r.returncode == 0 and r.stdout.strip() == "sent"
    return ok, (r.stderr or r.stdout).strip()[:300]


def send_tmux(name: str, text: str, enter: bool, *, env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    buf = f"claude-ops-{uuid.uuid4().hex[:10]}"
    r = subprocess.run([TMUX, "set-buffer", "-b", buf, "--", text], capture_output=True, text=True, env=env)
    if r.returncode == 0:
        # -p pastes with the bracketed-paste markers when the program asked for them, which Claude Code does
        r = subprocess.run([TMUX, "paste-buffer", "-p", "-d", "-b", buf, "-t", name], capture_output=True, text=True, env=env)
    if r.returncode == 0 and enter:
        time.sleep(0.3)  # the paste must be processed before Enter, or Enter lands inside it
        r = subprocess.run([TMUX, "send-keys", "-t", name, "Enter"], capture_output=True, text=True, env=env)
    return r.returncode == 0, r.stderr.strip()[:300]


def _log(entry: dict, log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def inject(target: str, text: str, *, transport: str = "iterm", enter: bool = True, force: bool = False,
           keep_text: bool = False, log: Path = LOG, sessions=None, send=None, env: Mapping[str, str] | None = None) -> dict:
    """Send `text` to a running chat. Returns {"result": "sent" | "no-session" | "not-claude" | "failed", ...}."""
    live = sessions() if sessions else (iterm_sessions(env=env) if transport == "iterm" else tmux_sessions(env=env))
    matched = resolve(target, live, transport)
    if not matched:
        return {"result": "no-session", "target": target, "open": sorted(v.get("name", k) for k, v in live.items())}
    if not force and not looks_like_claude(transport, live[matched]):
        return {"result": "not-claude", "target": target, "session": live[matched]}
    corr = uuid.uuid4().hex
    entry = {"at": datetime.now(timezone.utc).isoformat(), "correlation": corr, "event": "attempt",
             "transport": transport, "target": target, "session": matched, "name": live[matched].get("name"),
             "bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest(), "enter": enter,
             "caller_pid": os.getpid(), "caller_ppid": os.getppid()}
    if keep_text:
        entry["text"] = text
    _log(entry, log)
    if send is None:
        base = send_iterm if transport == "iterm" else send_tmux
        send = lambda m, t, e: base(m, t, e, env=env)
    ok, detail = send(matched, text, enter)
    _log({"at": datetime.now(timezone.utc).isoformat(), "correlation": corr, "event": "delivered" if ok else "failed",
          "detail": detail}, log)
    return {"result": "sent" if ok else "failed", "session": matched, "name": live[matched].get("name"),
            "correlation": corr, "detail": detail}
