"""Bring an open Claude Code chat up to date: is it safe to touch, and how to restart it in place.

An open chat keeps the Claude Code version, settings and instructions it started with. Bringing it
up to date means typing into it or restarting it, and both are only safe when it is idle and
nobody is typing into it. These are the checks and the mechanics, learned on a machine with a
dozen chats open at once.

- Never while the chat is working. A restart in the middle of a turn throws the turn away, and a
  background command the chat started dies with its process. `idle()` decides from what a chat
  already produces: the transcript, the process's children (a command or background task is a
  shell under `claude`), and, when the caller has one, the time its last turn ended.
- Never over a half-written message. A chat at its prompt reads as idle while a person types into
  it, and a paste would join their draft and send both. `may_be_typing` and `empty_prompt` check.
- Never /exit. A pasted /exit can open Claude Code's own exit dialog and leave a choice on screen
  that belongs to the person. `restart_iterm` sends SIGTERM; the conversation is on disk already.
- The permission mode does not survive a resume. It is read from the footer before the restart
  and passed back with --permission-mode.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from . import chat as _chat
from . import inject as _inject

SHELLS = {"zsh", "bash", "sh", "fish"}
#: after its last turn ended, a chat must have been quiet this long before it is touched
QUIET_S = 30
#: a chat with no recorded end of turn must be quiet this long
QUIET_UNHOOKED_S = 600
#: someone may be typing when the terminal is in front and they touched the Mac this recently
TYPING_WINDOW_S = 120
PROJECTS = Path.home() / ".claude" / "projects"


def shell_children(pid: int, procs: dict[int, dict]) -> list[int]:
    """The shells a claude process has started: a command or a background task it is running."""
    return [p for p, v in procs.items()
            if v["ppid"] == pid and Path(v["comm"]).name.lstrip("-") in SHELLS]


def idle(pid: int, procs: dict[int, dict], transcript_at: dt.datetime | None,
         turn_ended_at: dt.datetime | None, now: dt.datetime,
         prompt_on_screen: bool | None = None, *, quiet_s: float = QUIET_S,
         quiet_unhooked_s: float = QUIET_UNHOOKED_S) -> tuple[bool, str]:
    """(is the chat idle, why). Every "no" says what it is waiting for.

    `procs` is `config.processes()`, `transcript_at` is `last_activity(...)`, and `turn_ended_at`
    is when a Stop hook last fired for it (None when the caller has no such hook)."""
    if shell_children(pid, procs):
        return False, "it is running a command"
    if transcript_at is None:
        return False, "its transcript was not found, so whether it is working is unknown"
    quiet = (now - transcript_at).total_seconds()
    if turn_ended_at is None:
        # no end of turn to go by: only a long silence, and the prompt when it is visible
        if quiet < quiet_unhooked_s:
            return False, f"it has no finished turn on record and wrote {int(quiet)}s ago"
        if prompt_on_screen is False:
            return False, "Claude's prompt is not on its screen"
        return True, f"quiet for {int(quiet_unhooked_s // 60)} minutes with no command running"
    if turn_ended_at < transcript_at - dt.timedelta(seconds=5):
        return False, "it has written since its last turn ended, so it is working"
    if quiet < quiet_s:
        return False, f"its turn ended {int(quiet)}s ago"
    return True, "its turn ended and nothing is running"


def may_be_typing(seconds_since_input: float | None, front_app: str | None, *,
                  terminal: str = "iTerm2", window_s: float = TYPING_WINDOW_S) -> bool:
    """Someone may be typing into a chat: the terminal is in front and the Mac was touched in the
    last two minutes. Unknown means they may be. A phone (Remote Control) keeps its draft on the
    phone, so this is about the Mac's own keyboard."""
    if seconds_since_input is None or front_app is None:
        return True
    return front_app == terminal and seconds_since_input < window_s


#: the footer Claude Code shows for each permission mode, and the --permission-mode that brings it back
MODES = (("bypass permissions on", "bypassPermissions"), ("accept edits on", "acceptEdits"),
         ("plan mode on", "plan"), ("auto mode on", "auto"), ("don't ask on", "dontAsk"))


def permission_mode(screen: str | None) -> str | None:
    """The permission mode the chat is in, from its footer; None when it shows none (the default).
    A resume does not keep a mode switched inside the session, so read it before restarting."""
    low = (screen or "").lower()
    for footer, mode in MODES:
        if footer in low:
            return mode
    return None


#: the flags a restart carries over, and whether each takes a value. Nothing else is kept.
#: Not the rest of the command line: `ps` prints arguments without their quoting, so a chat
#: started with a first prompt reads back as loose words, and the first of them would reach the
#: resumed chat as a new message.
KEEP_FLAGS = {"--allow-dangerously-skip-permissions": False, "--dangerously-skip-permissions": False,
              "-n": True, "--name": True, "--remote-control": True, "--model": True,
              "--add-dir": True, "--effort": True}


def resume_line(args: str | None, chat_id: str, mode: str | None = None) -> str:
    """The command that reopens this conversation with the flags it was started with.

    `args` is the process's command line (`ps -o args=`), and `mode` the permission mode it was
    in (from `permission_mode`)."""
    argv = (args or "").split()
    out = ["claude"]
    if argv and Path(argv[0]).name == "claude":
        i = 1
        while i < len(argv):
            a = argv[i]
            if a in KEEP_FLAGS:
                if KEEP_FLAGS[a]:
                    if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                        out += [a, argv[i + 1]]
                        i += 2
                        continue
                elif a not in out:
                    out.append(a)
            i += 1
    tail = ["--permission-mode", mode] if mode else []
    return shlex.join(out + tail + ["--resume", chat_id])


#: words on screen that mean Claude is asking something, so a paste and Enter would answer it
DIALOG = ("enter to confirm", "esc to cancel", "esc to discard", "do you want", "enter to review",
          "trust this folder", "press enter", "(y/n)")


def empty_prompt(screen: str | None) -> tuple[bool, str]:
    """Is Claude showing its empty input line, with nothing asked on screen?

    A paste and Enter answer whatever is on screen: a permission question, an exit dialog or the
    trust prompt takes the Enter as a choice, and a half-typed message would be sent with the
    paste joined to it."""
    if not screen:
        return False, "its screen could not be read"
    lines = [ln.rstrip() for ln in screen.splitlines() if ln.strip()]
    tail = "\n".join(lines[-15:]).lower()
    if any(d in tail for d in DIALOG):
        return False, "Claude is asking something on its screen"
    prompts = [ln.strip() for ln in lines[-8:] if ln.strip().startswith("❯")]
    if not prompts:
        return False, "Claude's input line is not on its screen"
    if prompts[-1] != "❯":
        return False, "something is typed in its input line"
    return True, "its input line is empty"


ITERM_SCREEN = """on run argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (id of s as text) is (item 1 of argv) then return contents of s
        end repeat
      end repeat
    end repeat
  end tell
  return ""
end run"""


def screen_of(kind: str, handle: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """The text on a chat's screen: a tmux session by name, or an iTerm2 session by id."""
    if kind == "tmux":
        return _chat.pane_text(handle, env=env) or None
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", ITERM_SCREEN, handle], capture_output=True,
                           text=True, timeout=20, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def transcript_of(chat_id: str, projects: Path = PROJECTS) -> Path | None:
    """The transcript of a conversation, in whichever project folder holds it (newest if several)."""
    hits = list(projects.glob(f"*/{chat_id}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


#: transcript records that mean the conversation moved: a message, or one typed and queued
ACTIVITY = {"user", "assistant", "queue-operation"}


def last_activity(path: Path | None, first: int = 262144, cap: int = 64 << 20) -> dt.datetime | None:
    """When the conversation last moved: the newest message record in the transcript.

    Not the file's modification time: Claude Code appends bookkeeping after a turn has ended, so
    an idle chat's file keeps being touched. And not only the last few hundred kilobytes: a large
    transcript can end in megabytes of bookkeeping, so the tail read grows until a message is
    found or `cap` is reached."""
    if path is None:
        return None
    size = first
    try:
        total = path.stat().st_size
    except OSError:
        return None
    while True:
        try:
            with open(path, "rb") as f:
                f.seek(max(0, total - size))
                lines = f.read(min(size, total)).splitlines()
        except OSError:
            return None
        if size < total:
            lines = lines[1:]          # the first line of a window may be cut
        for raw in reversed(lines):
            if b'"timestamp"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            # a compaction writes a user record ("This session is being continued ...") with no turn
            # around it, so counting it made an idle chat look busy for ever
            if d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
                continue
            if d.get("type") in ACTIVITY and d.get("timestamp"):
                try:
                    return dt.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
                except ValueError:
                    continue
        if size >= total or size >= cap:
            return None
        size *= 4


def seconds_since_input(*, env: Mapping[str, str] | None = None) -> float | None:
    """The Mac's own count since the last keyboard, mouse or trackpad event (IOHIDSystem)."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"], capture_output=True,
                             text=True, timeout=10, env=env).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    vals = [int(m) for m in re.findall(r'"HIDIdleTime" = (\d+)', out)]
    return min(vals) / 1e9 if vals else None


def front_app(*, env: Mapping[str, str] | None = None) -> str | None:
    """The name of the application in front."""
    try:
        asn = subprocess.run(["/usr/bin/lsappinfo", "front"], capture_output=True, text=True,
                             timeout=5, env=env).stdout.strip()
        out = subprocess.run(["/usr/bin/lsappinfo", "info", "-only", "name", asn],
                             capture_output=True, text=True, timeout=5, env=env).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'"LSDisplayName"="([^"]*)"', out)
    return m.group(1) if m else None


def process_args(pid: int, *, env: Mapping[str, str] | None = None) -> str:
    """A process's command line, as ps prints it (without quoting)."""
    try:
        return subprocess.run(["/bin/ps", "-o", "args=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5, env=env).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _wait(pred: Callable[[], bool], seconds: float, step: float = 2.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def restart_iterm(pid: int, session_id: str, cwd: str, chat_id: str, *,
                  env: Mapping[str, str] | None = None, stop_s: float = 30,
                  back_s: float = 90) -> tuple[bool, str]:
    """End the chat in an iTerm2 session and resume the same conversation there.

    The permission mode is read from the footer and the start flags from the process, the process
    gets SIGTERM, and once the session is back at a shell the resume line is typed into it. The
    answer is (back at Claude's prompt, what happened)."""
    mode = permission_mode(screen_of("iterm", session_id, env=env))
    args = process_args(pid, env=env)
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, TypeError, ValueError) as e:
        return False, f"could not end the Claude process: {e}"

    def session():
        return _inject.iterm_sessions(env=env).get(session_id)

    def at_shell():
        s = session()
        return s is not None and not _inject.looks_like_claude("iterm", s)

    if not _wait(at_shell, stop_s):
        return False, f"Claude did not stop within {int(stop_s)} seconds of being asked to"
    cmd = resume_line(args, chat_id, mode)
    ok, detail = _inject.send_iterm(session_id, f"cd {shlex.quote(cwd)} && {cmd}", True, env=env)
    if not ok:
        return False, f"could not type the resume command ({detail})"

    def back():
        s = session()
        return s is not None and _inject.looks_like_claude("iterm", s)

    return _wait(back, back_s), f"iTerm session {session_id} resumed"
