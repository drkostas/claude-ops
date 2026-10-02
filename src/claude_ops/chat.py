"""Start a Claude Code chat inside a named tmux session, so it can be reached by name from birth.

The chat runs in tmux (it survives its window closing) and can be shown as an iTerm2 window through
iTerm2's tmux integration (`tmux -CC attach`). Lessons kept from real use:

- The transcript is written on the first message, not at launch, so a chat with no transcript yet
  is normal, not a failed start.
- A first run in a folder stops on prompts (trust this folder, permission mode, login). The pane
  text says which, so it is reported instead of a timeout.
- The trust prompt lists "No, exit" first with the cursor on it, so answering by position would
  close the chat. With --trust the answer is found by its words.
- A tmux window reattached in iTerm2 is blank until the program redraws, so Ctrl+L is sent after
  attaching.
- A tmux server keeps the environment of the terminal that started it, so ITERM_SESSION_ID is
  cleared for new sessions, or every chat in tmux would claim the same iTerm2 tab.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .inject import TMUX, tmux_sessions

PROJECTS = Path.home() / ".claude" / "projects"

BLOCKED_ON = [
    ("trust this folder", "Claude Code is asking whether to trust this folder.",
     "run again with --trust, or answer it: tmux attach -t {name}"),
    ("auto mode as my default permission mode", "Claude Code is asking for the default permission mode. --trust does not answer this.",
     "tmux attach -t {name} and choose"),
    ("select login method", "Claude Code is not signed in.", "tmux attach -t {name} and sign in"),
    ("browser didn't open", "Claude Code is waiting for a sign-in in the browser.", "tmux attach -t {name} and finish it"),
]


def project_dir(cwd: Path) -> Path:
    return PROJECTS / str(cwd.resolve()).replace("/", "-")


def transcripts(cwd: Path) -> set[str]:
    d = project_dir(cwd)
    return {p.stem for p in d.glob("*.jsonl")} if d.exists() else set()


def pane_text(name: str) -> str:
    r = subprocess.run([TMUX, "capture-pane", "-p", "-t", name], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def diagnose(text: str, name: str) -> tuple[str, str] | None:
    low = text.lower()
    for needle, what, fix in BLOCKED_ON:
        if needle in low:
            return what, fix.format(name=name)
    return None


def trust_keys(pane: str) -> list[str] | None:
    """Keys that move the cursor to "Yes, I trust this folder" and confirm, or None if that prompt is not showing.

    Only the last prompt on screen counts. An earlier one can still be visible above it.
    """
    lines = [ln for ln in pane.splitlines() if ln.strip()]
    yes_at = [i for i, ln in enumerate(lines) if "Yes, I trust" in ln]
    if not yes_at:
        return None
    lo, hi = max(0, yes_at[-1] - 3), yes_at[-1] + 3
    opts = [i for i in range(lo, min(hi + 1, len(lines)))
            if lines[i].strip().startswith("❯") or "Yes, I trust" in lines[i] or lines[i].strip().startswith(("No, exit", "No,"))]
    cur = next((i for i in opts if lines[i].strip().startswith("❯")), None)
    if cur is None:
        return None
    step = opts.index(yes_at[-1]) - opts.index(cur)
    return (["Down"] * step if step > 0 else ["Up"] * -step) + ["Enter"]


def cursor_on_yes(pane: str) -> bool:
    keys = trust_keys(pane)
    return keys == ["Enter"]


def stable_pane(name: str, tries: int = 10, gap: float = 0.5) -> str:
    """The pane text once two captures in a row are equal. Keys sent while the prompt is still drawing are lost."""
    prev = pane_text(name)
    for _ in range(tries):
        time.sleep(gap)
        cur = pane_text(name)
        if cur == prev:
            return cur
        prev = cur
    return prev


def answer_trust(name: str) -> bool:
    """Move to "Yes, I trust this folder" and press Enter only after seeing the cursor there."""
    keys = trust_keys(stable_pane(name))
    if keys is None:
        return False
    for k in keys[:-1]:
        subprocess.run([TMUX, "send-keys", "-t", name, k])
        time.sleep(0.3)
    if not cursor_on_yes(stable_pane(name)):
        return False  # never press Enter on an option we did not see selected
    subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])
    return True


def show_in_iterm(tmux_name: str) -> tuple[bool, str]:
    cmd = f"{TMUX} -CC attach -t {tmux_name}"
    r = subprocess.run(["osascript", "-e", f'tell application "iTerm2" to create window with default profile command "{cmd}"'],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return False, f"iTerm2 refused the window: {r.stderr.strip()[:200]}"
    time.sleep(2.0)
    subprocess.run([TMUX, "send-keys", "-t", tmux_name, "C-l"], capture_output=True)  # redraw
    return True, "shown in iTerm2"


def new(name: str, cwd: Path, *, claude_args: str = "", trust: bool = False, show: bool = True, wait: int = 30, log=print) -> dict:
    tmux_name = f"claude-{name}"
    if tmux_name in tmux_sessions():
        return {"result": "exists", "tmux": tmux_name}
    cwd = cwd.expanduser().resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    before = transcripts(cwd)
    subprocess.run([TMUX, "new-session", "-d", "-s", tmux_name, "-c", str(cwd), "-e", "ITERM_SESSION_ID="], check=True)
    shown = show_in_iterm(tmux_name) if show else (False, "not shown")
    command = f"claude -n {name} {claude_args}".strip()
    subprocess.run([TMUX, "send-keys", "-t", tmux_name, command, "Enter"], check=True)
    chat_id, trusted, blocked = None, False, None
    for _ in range(wait):
        time.sleep(1)
        new_ids = transcripts(cwd) - before
        if new_ids:
            chat_id = sorted(new_ids)[0]
            break
        pane = pane_text(tmux_name)
        if trust and not trusted and "yes, i trust" in pane.lower():
            trusted = answer_trust(tmux_name)
            log("answered the trust prompt (--trust)" if trusted else "the trust prompt did not respond as expected, nothing confirmed")
    if not chat_id:
        blocked = diagnose(pane_text(tmux_name), tmux_name)
    return {"result": "started", "tmux": tmux_name, "cwd": str(cwd), "shown": shown[0], "chat_id": chat_id,
            "trusted": trusted, "waiting_on": blocked[0] if blocked else None, "fix": blocked[1] if blocked else None}


def listing() -> list[dict]:
    return [{"tmux": n, **v} for n, v in sorted(tmux_sessions().items()) if n.startswith("claude-")]
