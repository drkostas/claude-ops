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

import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Mapping
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


def pane_text(name: str, *, styled: bool = False, env: Mapping[str, str] | None = None) -> str:
    """The text on a tmux pane's screen. `styled` keeps the colour and attribute codes (`-e`)."""
    flags = ["-p", "-e"] if styled else ["-p"]
    r = subprocess.run([TMUX, "-u", "capture-pane", *flags, "-t", name], capture_output=True, text=True, env=env)
    return r.stdout if r.returncode == 0 else ""


#: how many lines up from the bottom a start-up question is looked for: Claude Code draws the
#: question last, and words above it are the conversation, which may quote the question itself
QUESTION_LINES = 12


def input_box(text: str) -> bool:
    """Claude Code's own input box is at the bottom of the screen: a line opening with ❯ between two
    rules of ─. A screen that ends with it is waiting for a message, not on a start-up question."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][-8:]
    return any(lines[i].startswith("❯") and lines[i - 1].startswith("─") and lines[i + 1].startswith("─")
               for i in range(1, len(lines) - 1))


def diagnose(text: str, name: str) -> tuple[str, str] | None:
    """What a start-up question on the screen is, and how to answer it; None when there is none.

    Only the bottom of the screen counts, and never while the input box is there: a conversation
    that mentions "trust this folder" read as a chat stuck on the trust prompt."""
    if input_box(text):
        return None
    low = "\n".join([ln for ln in text.splitlines() if ln.strip()][-QUESTION_LINES:]).lower()
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


def stable_pane(name: str, tries: int = 10, gap: float = 0.5, *, env: Mapping[str, str] | None = None) -> str:
    """The pane text once two captures in a row are equal. Keys sent while the prompt is still drawing are lost."""
    prev = pane_text(name, env=env)
    for _ in range(tries):
        time.sleep(gap)
        cur = pane_text(name, env=env)
        if cur == prev:
            return cur
        prev = cur
    return prev


def answer_trust(name: str, *, env: Mapping[str, str] | None = None) -> bool:
    """Move to "Yes, I trust this folder" and press Enter only after seeing the cursor there."""
    keys = trust_keys(stable_pane(name, env=env))
    if keys is None:
        return False
    for k in keys[:-1]:
        subprocess.run([TMUX, "-u", "send-keys", "-t", name, k], env=env)
        time.sleep(0.3)
    if not cursor_on_yes(stable_pane(name, env=env)):
        return False  # never press Enter on an option we did not see selected
    subprocess.run([TMUX, "-u", "send-keys", "-t", name, "Enter"], env=env)
    return True


def show_in_iterm(tmux_name: str, *, env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    cmd = f"{TMUX} -u -CC attach -t {tmux_name}"
    r = subprocess.run(["osascript", "-e", f'tell application "iTerm2" to create window with default profile command "{cmd}"'],
                       capture_output=True, text=True, timeout=20, env=env)
    if r.returncode != 0:
        return False, f"iTerm2 refused the window: {r.stderr.strip()[:200]}"
    time.sleep(2.0)
    subprocess.run([TMUX, "-u", "send-keys", "-t", tmux_name, "C-l"], capture_output=True, env=env)  # redraw
    return True, "shown in iTerm2"


def new(name: str, cwd: Path, *, claude_args: str = "", trust: bool = False, show: bool = True, wait: int = 30, log=print, env: Mapping[str, str] | None = None) -> dict:
    tmux_name = f"claude-{name}"
    if tmux_name in tmux_sessions(env=env):
        return {"result": "exists", "tmux": tmux_name}
    cwd = cwd.expanduser().resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    before = transcripts(cwd)
    subprocess.run([TMUX, "-u", "new-session", "-d", "-s", tmux_name, "-c", str(cwd), "-e", "ITERM_SESSION_ID="], check=True, env=env)
    shown = show_in_iterm(tmux_name, env=env) if show else (False, "not shown")
    command = f"claude -n {name} {claude_args}".strip()
    subprocess.run([TMUX, "-u", "send-keys", "-t", tmux_name, command, "Enter"], check=True, env=env)
    chat_id, trusted, blocked = None, False, None
    for _ in range(wait):
        time.sleep(1)
        new_ids = transcripts(cwd) - before
        if new_ids:
            chat_id = sorted(new_ids)[0]
            break
        pane = pane_text(tmux_name, env=env)
        if trust and not trusted and "yes, i trust" in pane.lower():
            trusted = answer_trust(tmux_name, env=env)
            log("answered the trust prompt (--trust)" if trusted else "the trust prompt did not respond as expected, nothing confirmed")
    if not chat_id:
        blocked = diagnose(pane_text(tmux_name, env=env), tmux_name)
    return {"result": "started", "tmux": tmux_name, "cwd": str(cwd), "shown": shown[0], "chat_id": chat_id,
            "trusted": trusted, "waiting_on": blocked[0] if blocked else None, "fix": blocked[1] if blocked else None}


def end(name: str, *, env: Mapping[str, str] | None = None) -> dict:
    """Stop a chat's tmux session. iTerm2 is detached first, because killing a session it is still
    attached to with -CC leaves an empty control window behind."""
    tmux_name = name if name.startswith("claude-") else f"claude-{name}"
    if tmux_name not in tmux_sessions(env=env):
        return {"result": "no-session", "tmux": tmux_name}
    subprocess.run([TMUX, "-u", "detach-client", "-s", tmux_name], capture_output=True, env=env)
    time.sleep(0.5)
    r = subprocess.run([TMUX, "-u", "kill-session", "-t", tmux_name], capture_output=True, text=True, env=env)
    return {"result": "ended" if r.returncode == 0 else "failed", "tmux": tmux_name, "detail": r.stderr.strip()}


def listing(*, env: Mapping[str, str] | None = None) -> list[dict]:
    return [{"tmux": n, **v} for n, v in sorted(tmux_sessions(env=env).items()) if n.startswith("claude-")]


def _mcp(mcp_config: str | None, strict: bool = True) -> list[str]:
    """The flags that give a chat the MCP servers in `mcp_config`: only those when `strict`, those
    beside the ones its configuration names when not; none without a file."""
    if not mcp_config:
        return []
    return (["--strict-mcp-config"] if strict else []) + ["--mcp-config", mcp_config]


def claude_command(name: str, mode: str | None, *, remote_control: bool = True,
                   settings: str | None = None, mcp_config: str | None = None,
                   mcp_strict: bool = True, model: str | None = None,
                   effort: str | None = None) -> str:
    """The command that starts a named chat: `claude -n <name>`, with Remote Control under the same
    name (it works inside tmux), and `--permission-mode <mode>` when a mode is given.

    `mode` has no default on purpose: which permission mode a chat starts in is the caller's
    choice (bypassPermissions, acceptEdits, plan, ...), and None means Claude Code's own default.

    `settings` is a settings file for this chat alone (`--settings <file>`), for example one that
    sends its requests to a local model. It is a flag rather than environment, so it shows in the
    chat's command line and a restart keeps it (`update.KEEP_FLAGS`).

    `mcp_config` is a file of MCP servers for this chat alone: the chat starts only those
    (`--strict-mcp-config --mcp-config <file>`), and a restart keeps both flags. With
    `mcp_strict=False` it starts them beside the servers its configuration names (`--mcp-config
    <file>` alone), so plugin servers and connectors still load; a server in the file replaces a
    configured one of the same name.

    `model` and `effort` start the chat on that model and effort level (`--model`, `--effort`) in
    place of the settings' default, as `resume_command` reopens one. With `settings`, the settings
    file decides the model, so neither is added. A restart keeps them, since `update.model_of` reads
    both back from the conversation."""
    parts = (["claude"] + (["--settings", settings] if settings else []) + _mcp(mcp_config, mcp_strict)
             + ["-n", name])
    if not settings:
        parts += (["--model", model] if model else []) + (["--effort", effort] if effort else [])
    if remote_control:
        parts += ["--remote-control", name]
    if mode:
        parts += ["--permission-mode", mode]
    return shlex.join(parts)


#: the settings that turn ultracode on, which Claude Code keeps for the session only. Compact, so
#: `ps` prints it as one word and `update.resume_line` can find it again.
ULTRACODE_SETTINGS = '{"ultracode":true}'


def resume_command(name: str, chat_id: str, mode: str | None, *, remote_control: bool = True,
                   settings: str | None = None, model: str | None = None,
                   effort: str | None = None, mcp_config: str | None = None,
                   mcp_strict: bool = True, ultracode: bool = False) -> str:
    """The command that reopens conversation `chat_id` under the same name, in the mode given
    (read a running chat's mode with `update.permission_mode` before ending it). `model` and
    `effort` reopen it on what it last ran on (`update.model_of`). With `settings`, the settings
    file decides the model, so neither is added. `mcp_config` and `mcp_strict` are as in
    `claude_command`. `ultracode` reopens it with ultracode on (`update.ultracode_of`), which a
    resume otherwise drops, since Claude Code keeps it for the session only; not with `settings`,
    where a second settings flag would replace the file."""
    parts = (["claude"] + (["--settings", settings] if settings else []) + _mcp(mcp_config, mcp_strict)
             + ["--resume", chat_id, "-n", name])
    if not settings:
        parts += (["--model", model] if model else []) + (["--effort", effort] if effort else [])
        parts += ["--settings", ULTRACODE_SETTINGS] if ultracode else []
    if remote_control:
        parts += ["--remote-control", name]
    if mode:
        parts += ["--permission-mode", mode]
    return shlex.join(parts)


def pane_owner(pid: int, *, env: Mapping[str, str] | None = None) -> str | None:
    """Which tmux session a process is in, by walking up its parents to a pane's pid.

    The process does not know it is in tmux and tmux does not know what it runs, so the process
    tree is the join, and the answer is derived rather than known."""
    r = subprocess.run([TMUX, "-u", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return None
    panes = {}
    for ln in r.stdout.splitlines():
        sname, _, ppid = ln.partition("\t")
        if ppid.isdigit():
            panes[int(ppid)] = sname
    seen, cur = 0, pid
    while cur and cur > 1 and seen < 30:
        if cur in panes:
            return panes[cur]
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(cur)], capture_output=True,
                             text=True, env=env).stdout.strip()
        cur = int(out) if out.isdigit() else 0
        seen += 1
    return None


def in_tmux(pid: int, procs: dict[int, dict]) -> bool | None:
    """Whether a process runs inside a tmux pane: one of its parents is a tmux server. None when
    the process is not in `procs` (`config.processes()`), which is unknown.

    The cheap form of `pane_owner`: no tmux or ps call per process, so a caller that already holds
    the process table can ask it of every chat. A tmux client (`tmux attach` in an iTerm tab) is
    not a parent of claude, so a chat shown through tmux is still found by its server."""
    if pid not in procs:
        return None
    seen, p = set(), procs[pid].get("ppid")
    while p and p not in seen and p in procs:
        seen.add(p)
        if Path(procs[p].get("comm") or "").name.lower() == "tmux":
            return True
        p = procs[p].get("ppid")
    return False


def plain_look(name: str) -> list[list[str]]:
    """The tmux commands that make plain tmux (shown with `tmux attach`, not iTerm's tmux mode)
    look and behave like the terminal tab it is shown in.

    - No status bar, so the tab looks as it did.
    - Mouse on, because iTerm cannot scroll a plain tmux screen and the wheel then scrolls tmux's.
    - The pane's title goes to the terminal. Claude Code titles its terminal with the chat's name
      ("✳ my-chat") by an escape code, and inside plain tmux that title stays on tmux's pane: every
      tab read "cd (tmux)" until set-titles handed it on (2026-10-11).
    - focus-events, which Claude Code asks for inside tmux. It is a server option."""
    return [[TMUX, "-u", "set-option", "-t", name, "status", "off"],
            [TMUX, "-u", "set-option", "-t", name, "mouse", "on"],
            [TMUX, "-u", "set-option", "-t", name, "set-titles", "on"],
            [TMUX, "-u", "set-option", "-t", name, "set-titles-string", "#{pane_title}"],
            [TMUX, "-u", "set-option", "-s", "focus-events", "on"]]


def attach_line(cwd: str, name: str, resume: str, cc: bool = False) -> str:
    """One shell line, typed into a terminal tab, that starts tmux session `name` in `cwd`, types
    `resume` into it and shows the session in that same tab.

    The session gets a shell first and the command is typed into it, as `new` does, so the session
    outlives Claude and a later restart can resume in the same pane. ITERM_SESSION_ID is cleared,
    because a pane must never claim the tab's iTerm session as its own. `cc` shows the session
    through iTerm's tmux mode instead, which opens it as a new tab or window."""
    q = shlex.quote
    look = "" if cc else "".join("&& " + shlex.join(c) + " " for c in plain_look(name))
    attach = f"{TMUX} -u {'-CC ' if cc else ''}attach -t {q(name)}"
    return (f"cd {q(cwd)} && {TMUX} -u new-session -d -s {q(name)} -c {q(cwd)} -e ITERM_SESSION_ID= "
            f"{look}&& {TMUX} -u send-keys -t {q(name)} -l {q(resume)} "
            f"&& {TMUX} -u send-keys -t {q(name)} Enter && {attach}")


def server_context(*, env: Mapping[str, str] | None = None) -> str:
    """Whose context the running tmux server has: "none" (no server; the next one starts wherever
    it is started from), "iterm" (it can script iTerm2), "outside" (it cannot), or "unknown".

    ⛔ EVERYTHING IN A TMUX SERVER RUNS IN THE CONTEXT OF WHATEVER STARTED THE SERVER. One started by
    a background job (launchd) cannot script iTerm2, and the macOS permissions its chats ask for
    are judged for that job. A chat moved from an iTerm2 tab into such a server would lose what it
    had in iTerm2, so a move asks this first. The question is asked from inside the server."""
    if subprocess.run([TMUX, "-u", "ls"], capture_output=True, text=True, env=env).returncode != 0:
        return "none"
    fd, path = tempfile.mkstemp(prefix="claude-ops-tmux-context-")
    os.close(fd)
    try:
        # run-shell runs in the server itself; with no client attached it shows nothing, so the
        # answer is written to a file
        subprocess.run([TMUX, "-u", "run-shell",
                        f"osascript -e 'tell application \"iTerm2\" to count windows' > {path} 2>&1"],
                       capture_output=True, text=True, env=env, timeout=30)
        with open(path) as f:
            out = f.read().strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return "iterm" if out.isdigit() else "outside"


def at_prompt(name: str, wait: float = 90.0, *, env: Mapping[str, str] | None = None) -> bool:
    """Claude is at its prompt in tmux session `name`: the input marker is on screen and no
    start-up question is."""
    end = time.time() + wait
    while time.time() < end:
        text = pane_text(name, env=env)
        if "❯" in text and diagnose(text, name) is None:
            return True
        time.sleep(2)
    return False


_REPO_NAMES: dict[str, str] = {}


def repo_name(path: str, *, env: Mapping[str, str] | None = None) -> str:
    """The name of the repository a folder is in (its main checkout's folder name), or the
    folder's own name when it is in none. Cached per path."""
    if path not in _REPO_NAMES:
        from .worktree import repo_root  # noqa: PLC0415
        root = repo_root(Path(path), env=env)
        _REPO_NAMES[path] = (root or Path(path)).name
    return _REPO_NAMES[path]


def parent_of(pid: int, *, env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """(parent pid, command) of one process, read with ps; None when it cannot be read."""
    try:
        out = subprocess.run(["/bin/ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=3, env=env).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not out:
        return None
    ppid, _, comm = out.partition(" ")
    return ppid, comm


def claude_pid(start: int | None = None, parent=None, *, env: Mapping[str, str] | None = None,
               depth: int = 8) -> int | None:
    """The Claude Code process a hook (or any child of a chat) runs under: walk up from `start`
    (default: this process's parent) to the first process whose command is `claude`. None when the
    walk reaches the top, `depth` steps, or a process it cannot read. `parent` reads one step of the
    tree and stands in for `ps` in a test."""
    parent = parent or (lambda p: parent_of(p, env=env))
    pid = os.getppid() if start is None else start
    for _ in range(depth):
        step = parent(pid)
        if step is None:
            return None
        ppid, comm = step
        if os.path.basename(comm.strip()) == "claude":
            return pid
        try:
            pid = int(ppid)
        except ValueError:
            return None
        if pid <= 1:
            return None
    return None
