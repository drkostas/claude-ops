"""Tell chats apart in a shared iTerm2 window by giving each tab its own color.

When several Claude Code chats run as tabs of one iTerm2 window, their titles change as they work
and look alike at a glance. A tab color that stays with each chat makes them easy to tell apart.

`assign` is the rule, with no iTerm2 in it: in every window that holds two or more chats, each tab
gets a color no other tab in that window has. A color the user set is kept, a chat takes back the
color it had before when that color is free in its window, and new colors come from PALETTE.
`chat_tabs` reads the windows and `set_tab_color` writes one tab, through iTerm2's Python API (the
`iterm2` package, installed with the `iterm` extra, and the API switched on in iTerm2's settings).

The rest finds and tidies iTerm2's windows through AppleScript: which window shows a session, a
terminal or a tmux pane, which windows hold an idle shell in a folder (where a new session for that
folder belongs), its frame, and the empty frames iTerm2 can leave behind. Every listing
reads each window inside its own `try`, because a window iTerm2 closed can stay in its scripting
model unreadable, and one such window failed every listing that did not. A listing that could not
be taken is None, never "no windows".
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence

Color = tuple[int, int, int]

#: distinct, readable tab colors, in the order they are handed out
PALETTE: tuple[Color, ...] = ((234, 116, 104), (237, 221, 104), (185, 144, 212), (235, 175, 90),
                              (126, 196, 123), (104, 166, 234), (106, 204, 204), (232, 140, 186),
                              (172, 172, 172), (196, 164, 122))


def _key(color) -> Color | None:
    return tuple(int(round(x)) for x in color) if color else None


def assign(windows: Sequence[Sequence[tuple[str, Sequence[float] | None]]],
           remembered: Mapping[str, Color] | None = None,
           palette: Sequence[Color] = PALETTE) -> dict[str, Color]:
    """Pure. {chat: color} for the tabs whose color should change, so that no two tabs in a window
    with two or more chats share a color. `windows` is, per window, (chat, its tab's color or None),
    in tab order. A tab keeps its color when no earlier tab in its window has it. Otherwise it takes
    the color it had before (`remembered`) if that is free in the window, else the first free color
    in `palette`. A window with more chats than colors leaves the rest as they are. iTerm2 reports
    colors as floats, and they are compared rounded."""
    remembered = remembered or {}
    out: dict[str, Color] = {}
    for tabs in windows:
        if len(tabs) < 2:
            continue
        used: set[Color] = set()
        for chat, color in tabs:
            now = _key(color)
            if now is not None and now not in used:
                used.add(now)
                continue
            mine = _key(remembered.get(chat))
            pick = mine if mine is not None and mine not in used else next(
                (c for c in palette if c not in used), None)
            if pick is None:
                continue
            used.add(pick)
            out[chat] = pick
    return out


def tmux_by_tty(*, tmux: str = "tmux", env: Mapping[str, str] | None = None) -> dict[str, str]:
    """{terminal device: tmux session} for every tmux client, {} when tmux does not answer."""
    try:
        r = subprocess.run([tmux, "list-clients", "-F", "#{client_tty} #{session_name}"],
                           capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split(" ", 1) for line in r.stdout.splitlines() if " " in line)


def chat_tabs(*, prefix: str = "claude-", tmux: str = "tmux",
              env: Mapping[str, str] | None = None) -> list[list[tuple[str, Color | None, str]]] | None:
    """Per iTerm2 window, (tmux session, tab color or None, iTerm2 session id) for each tab that
    shows a tmux session whose name starts with `prefix`. None when iTerm2's API does not answer or
    the `iterm2` package is missing."""
    try:
        import iterm2   # noqa: PLC0415
    except ImportError:
        return None
    names = tmux_by_tty(tmux=tmux, env=env)
    out: list = []

    async def main(connection):
        app = await iterm2.async_get_app(connection)
        for w in app.terminal_windows:
            row = []
            for t in w.tabs:
                s = t.current_session
                name = names.get(await s.async_get_variable("tty") or "")
                if not name or not name.startswith(prefix):
                    continue
                p = await s.async_get_profile()
                c = p.tab_color if p.use_tab_color else None
                row.append((name, _key((c.red, c.green, c.blue)) if c is not None else None, s.session_id))
            out.append(row)

    if not _run(iterm2, main):
        return None
    return out


def _run(iterm2, main) -> bool:
    """Run `main` in a connection of its own; False when iTerm2's API did not answer. The `iterm2`
    package ends the program with SystemExit(1) when it cannot connect ("There was a problem
    connecting to iTerm2"), and SystemExit is not an Exception, so a plain except let every caller's
    whole program end there instead of reading None."""
    try:
        iterm2.run_until_complete(main, retry=False)
    except (Exception, SystemExit):  # noqa: BLE001 - iTerm2 not answering: nothing is known
        return False
    return True


def set_tab_color(session_id: str, color: Color) -> bool:
    """Color the tab of one iTerm2 session, found by its id in a connection of its own (a session
    object read in an earlier connection cannot be written once that connection has closed)."""
    try:
        import iterm2   # noqa: PLC0415
    except ImportError:
        return False

    async def main(connection):
        app = await iterm2.async_get_app(connection)
        session = app.get_session_by_id(session_id)
        if session is None:
            raise LookupError(session_id)
        p = iterm2.LocalWriteOnlyProfile()
        p.set_tab_color(iterm2.Color(*color))
        p.set_use_tab_color(True)
        await session.async_set_profile_properties(p)

    return _run(iterm2, main)


def color_chat_tabs(*, prefix: str = "claude-", remembered: Mapping[str, Color] | None = None,
                    tmux: str = "tmux", env: Mapping[str, str] | None = None) -> dict[str, Color] | None:
    """Read the windows, color the tabs `assign` names, and return {chat: color} for each tab
    colored. None when iTerm2 could not be read."""
    windows = chat_tabs(prefix=prefix, tmux=tmux, env=env)
    if windows is None:
        return None
    change = assign([[(n, c) for n, c, _ in row] for row in windows], remembered)
    ids = {n: sid for row in windows for n, _c, sid in row}
    return {n: c for n, c in change.items() if set_tab_color(ids[n], c)}


# ----------------------------------------------------------------------------- windows

OSASCRIPT = "/usr/bin/osascript"
SEP = "\t"


def _osa(script: str, timeout: float = 5, *, env: Mapping[str, str] | None = None) -> str | None:
    try:
        r = subprocess.run([OSASCRIPT, "-e", script], capture_output=True, text=True, timeout=timeout,
                           env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


_WINDOWS = '''set sep to (ASCII character 9)
set out to ""
tell application "iTerm2"
repeat with w in (get windows)
try
set b to bounds of w
set out to out & (id of w) & sep & (count of tabs of w) & sep & (item 1 of b) & "," & (item 2 of b) & "," & (item 3 of b) & "," & (item 4 of b) & sep & (name of w) & linefeed
end try
end repeat
end tell
return out'''


def parse_windows(raw: str | None) -> list[dict] | None:
    """Pure. iTerm2's window listing: {"id", "tabs", "frame" (yabai's coordinates), "title"}."""
    if raw is None:
        return None
    out = []
    for line in raw.splitlines():
        parts = line.split(SEP, 3)
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        try:
            left, top, right, bottom = (float(v) for v in parts[2].split(","))
        except ValueError:
            continue
        out.append({"id": int(parts[0]), "tabs": int(parts[1]) if parts[1].isdigit() else None,
                    "frame": {"x": left, "y": top, "w": right - left, "h": bottom - top},
                    "title": parts[3]})
    return out


def windows(*, env: Mapping[str, str] | None = None) -> list[dict] | None:
    """Every window iTerm2 has, as iTerm2 says; None when it does not answer (a launch agent that
    was never allowed to script iTerm2 gets error -1712). iTerm2's window id is also yabai's."""
    return parse_windows(_osa(_WINDOWS, 5, env=env))


_DEAD = '''set out to ""
tell application "iTerm2"
repeat with w in (get windows)
try
set wid to (id of w) as text
on error
set wid to ""
end try
try
set n to count of (get tabs of w)
on error
if wid is not "" then set out to out & wid & linefeed
end try
end repeat
end tell
return out'''


def dead_windows(raw: str | None = None, *, env: Mapping[str, str] | None = None) -> set[int] | None:
    """The ids of windows iTerm2 still lists but whose tabs cannot be read: frames iTerm2 closed
    that stayed on screen, blank. Only the window's own close button removes one (through yabai,
    claude_human.desktops.close_window). None when iTerm2 did not answer."""
    out = _osa(_DEAD, 8, env=env) if raw is None else raw
    if out is None:
        return None
    return {int(x) for x in out.split() if x.strip().isdigit()}


def set_bounds(window_id: int, target: dict, *, wait: float = 2.0,
               env: Mapping[str, str] | None = None) -> dict | None:
    """Set an iTerm2 window's frame through iTerm2's own bounds and return the frame it then reads
    back, or None when iTerm2 does not know the window. iTerm2 applies bounds a moment later, so it
    is read again for up to `wait` seconds. Fits claude_human.desktops.frame_window(fallback=...),
    for a window yabai has lost."""
    import time   # noqa: PLC0415
    x, y = int(target["x"]), int(target["y"])
    bounds = f"{{{x}, {y}, {x + int(target['w'])}, {y + int(target['h'])}}}"
    _osa(f'tell application "iTerm2" to set bounds of window id {int(window_id)} to {bounds}', 5, env=env)
    end = time.time() + wait
    while True:
        w = next((v for v in (windows(env=env) or []) if v["id"] == int(window_id)), None)
        if w is None:
            return None
        fr = w["frame"]
        if all(abs(fr[k] - target[k]) <= 2 for k in ("x", "y", "w", "h")) or time.time() >= end:
            return fr
        time.sleep(0.5)


def close_empty_window(window_id: int, *, wait: float = 6.0,
                       env: Mapping[str, str] | None = None) -> str:
    """Close an iTerm2 window that holds no session, and nothing else: "closed" (seen gone),
    "asked" (closed, still there after `wait` seconds), "kept" (it holds a session) or "not found".

    A tmux control-mode window can stay on screen empty after tmux attaches; yabai cannot close it
    but iTerm2's `close window id N` does, and the tmux client stays attached. The close lands a few
    seconds later, so it is read back. A window still holding a session is never closed: that
    would detach whatever runs there."""
    import time   # noqa: PLC0415
    ask = f'tell application "iTerm2" to count of tabs of window id {int(window_id)}'
    out = _osa(ask, 5, env=env)
    if out is None:
        return "not found"
    if out.strip() != "0":
        return "kept"
    if _osa(f'tell application "iTerm2" to close window id {int(window_id)}', 5, env=env) is None:
        return "kept"
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.5)
        if _osa(ask, 5, env=env) is None:
            return "closed"
    return "asked"


def session_uuid(session: str | None) -> str | None:
    """Pure. The UUID part of an ITERM_SESSION_ID-shaped value ("w0t1p0:<UUID>" or "tmux:<UUID>")."""
    if not session or ":" not in session:
        return None
    return session.split(":", 1)[1] or None


def _safe(u: str | None) -> bool:
    return bool(u) and all(ch.isalnum() or ch == "-" for ch in u)


def window_of_session(session: str, *, env: Mapping[str, str] | None = None) -> int | None:
    """The iTerm2 window holding this session, or None. Three seconds at most: iTerm2 sometimes
    stops answering scripts altogether, and a caller should ask again later rather than wait."""
    u = session_uuid(session)
    if not _safe(u):
        return None
    out = _osa(f'''tell application "iTerm2" to repeat with w in (get windows)
try
repeat with t in (get tabs of w)
repeat with s in (get sessions of t)
if unique id of s is "{u}" then return (id of w as text)
end repeat
end repeat
end try
end repeat''', 3, env=env)
    return int(out.strip()) if out and out.strip().isdigit() else None


def session_of_tmux_pane(pane: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """The iTerm2 session showing this tmux pane (tmux -CC), as "tmux:<UUID>", or None when no
    window shows it. A process started inside tmux has no ITERM_SESSION_ID of its own, but iTerm2
    records which pane each tmux-mode session shows (its `tmuxWindowPane` variable)."""
    num = (pane or "").lstrip("%")
    if not num.isdigit():
        return None
    out = _osa(f'''tell application "iTerm2" to repeat with w in (get windows)
try
repeat with t in (get tabs of w)
repeat with s in (get sessions of t)
try
if ((variable named "tmuxWindowPane") of s) is "{num}" then return (unique id of s)
end try
end repeat
end repeat
end try
end repeat''', 3, env=env)
    u = (out or "").strip()
    return f"tmux:{u}" if _safe(u) else None


_TTYS = '''set sep to (ASCII character 9)
set out to ""
tell application "iTerm2"
repeat with w in (get windows)
try
repeat with t in (get tabs of w)
repeat with s in (get sessions of t)
try
set out to out & (id of w) & sep & (tty of s) & linefeed
end try
end repeat
end repeat
end try
end repeat
end tell
return out'''


def parse_ttys(raw: str | None) -> list[tuple[int, str]]:
    """Pure. (window id, session terminal) pairs from the terminals listing."""
    out = []
    for line in (raw or "").splitlines():
        w, _, tty = line.partition(SEP)
        if w.strip().isdigit() and tty.strip().startswith("/dev/"):
            out.append((int(w.strip()), tty.strip()))
    return out


def tty_listing(*, env: Mapping[str, str] | None = None) -> str | None:
    """Every iTerm2 session's terminal with its window, read in one AppleScript call, or None when
    iTerm2 did not answer within 8 seconds. Pass it as `raw` to window_of_ttys and windows_of_ttys
    to answer for many terminals from one reading.

    Each of those calls without `raw` walks every window, tab and session again. Asked once per
    tmux session, that is close to a second each while iTerm2 answers, and the whole 8 seconds
    each while it does not (a consent prompt can hold its Apple Events for hours), so a loop over
    eight sessions waited more than a minute before giving up."""
    return _osa(_TTYS, 8, env=env)


def window_of_ttys(ttys: set[str], raw: str | None = None, *,
                   env: Mapping[str, str] | None = None) -> int | None:
    """The one iTerm2 window holding a session on one of these terminals, or None (none, or several:
    never a guess). A terminal is an identity; a window's title names only its front tab."""
    if not ttys:
        return None
    pairs = parse_ttys(tty_listing(env=env) if raw is None else raw)
    hits = {w for w, tty in pairs if tty in ttys}
    return hits.pop() if len(hits) == 1 else None


def windows_of_ttys(ttys: set[str], raw: str | None = None, *,
                    env: Mapping[str, str] | None = None) -> set[int] | None:
    """Every iTerm2 window holding a session on one of these terminals: an empty set when none does,
    and None when iTerm2 could not be read. Ask this to know whether something is shown at all.
    window_of_ttys answers only for exactly one window, so a tmux session attached from two windows
    reads there as shown nowhere, and a caller that then opens a tab for it opens one more each time."""
    if not ttys:
        return set()
    raw = tty_listing(env=env) if raw is None else raw
    if raw is None:
        return None
    return {w for w, tty in parse_ttys(raw) if tty in ttys}


#: the programs a terminal runs when nothing runs but its shell
SHELLS = frozenset({"zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh"})


def shell_pid(ps_text: str) -> int | None:
    """Pure. The shell's pid when it is all that runs in a terminal's foreground, from
    `ps -t <tty> -o pid=,stat=,comm=` (a foreground process has `+` in its state), else None. A
    login shell is listed as `-zsh`; a terminal running a program, or a pipeline, has another
    foreground process and answers None."""
    fore = [parts for parts in (line.split(None, 2) for line in (ps_text or "").splitlines())
            if len(parts) == 3 and "+" in parts[1]]
    if len(fore) != 1 or not fore[0][0].isdigit():
        return None
    name = fore[0][2].strip().rsplit("/", 1)[-1].lstrip("-")
    return int(fore[0][0]) if name in SHELLS else None


def cwd_of(pid: int, *, run=subprocess.run, env: Mapping[str, str] | None = None) -> str | None:
    """The working folder of a process, read with lsof, or None when it cannot be read."""
    try:
        r = run(["/usr/sbin/lsof", "-a", "-d", "cwd", "-p", str(int(pid)), "-Fn"],
                capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return next((line[1:] for line in (r.stdout or "").splitlines() if line.startswith("n")), None)


def shell_windows(folder: str, raw: str | None = None, *, run=subprocess.run,
                  env: Mapping[str, str] | None = None) -> dict[int, list[str]] | None:
    """Every iTerm2 window holding a session whose foreground is only its shell, working in `folder`
    or a folder inside it: {window id: [terminal, ...]}, in the listing's order. Such a session is
    what a chat or a program leaves behind in a project's window when it ends, and that window is
    where a new session for the same folder belongs. None when iTerm2 could not be read; pass the
    listing as `raw` (tty_listing) to answer from one reading."""
    raw = tty_listing(env=env) if raw is None else raw
    if raw is None:
        return None
    want = os.path.realpath(folder)
    out: dict[int, list[str]] = {}
    for window, tty in parse_ttys(raw):
        try:
            ps = run(["ps", "-t", tty.removeprefix("/dev/"), "-o", "pid=,stat=,comm="],
                     capture_output=True, text=True, timeout=5, env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        pid = shell_pid(ps.stdout or "")
        cwd = cwd_of(pid, run=run, env=env) if pid is not None else None
        if cwd and (os.path.realpath(cwd) == want or os.path.realpath(cwd).startswith(want + os.sep)):
            out.setdefault(window, []).append(tty)
    return out


def tmux_client_ttys(session: str, *, tmux: str = "tmux", env: Mapping[str, str] | None = None) -> set[str]:
    """The terminals of the plain clients attached to a tmux session (a control-mode client has
    none of its own that iTerm2 shows)."""
    try:
        r = subprocess.run([tmux, "list-clients", "-t", session, "-F",
                            "#{client_tty} #{client_control_mode}"],
                           capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if r.returncode != 0:
        return set()
    return {p[0] for p in (line.split() for line in r.stdout.splitlines())
            if len(p) == 2 and p[1] == "0" and p[0].startswith("/dev/")}


# ------------------------------------------------------------------------------------ restarting

#: iTerm2's session server, which owns the terminals when session restoration runs through it
SERVER_MARK = "/iTermServer-"


def _pids(argv: Sequence[str], env: Mapping[str, str] | None) -> list[int]:
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in r.stdout.split() if x.isdigit()]


def _children(pid: int, env: Mapping[str, str] | None) -> int:
    try:
        r = subprocess.run(["/bin/ps", "-Ao", "ppid="], capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return -1
    return sum(1 for x in r.stdout.split() if x.isdigit() and int(x) == pid)


class Processes:
    """What restart_app touches outside iTerm2's scripting, replaced by a fake in tests."""

    def __init__(self, env: Mapping[str, str] | None = None):
        self.env = env

    def app(self) -> list[int]:
        return _pids(["/usr/bin/pgrep", "-x", "iTerm2"], self.env)

    def server(self) -> list[int]:
        return _pids(["/usr/bin/pgrep", "-f", SERVER_MARK], self.env)

    def children(self, pid: int) -> int:
        return _children(pid, self.env)

    def kill(self, pid: int) -> None:
        import os, signal   # noqa: E401, PLC0415
        os.kill(pid, signal.SIGKILL)

    def alive(self, pid: int) -> bool:
        import os   # noqa: PLC0415
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def open(self) -> bool:
        try:
            return subprocess.run(["/usr/bin/open", "-a", "iTerm"], capture_output=True, timeout=30,
                                  env=self.env).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def answers(self) -> bool:
        return windows(env=self.env) is not None

    def sleep(self, s: float) -> None:
        import time   # noqa: PLC0415
        time.sleep(s)


def restart_app(*, answer_wait: float = 90.0, procs: Processes | None = None,
                env: Mapping[str, str] | None = None) -> tuple[str, dict]:
    """Restart iTerm2's app while its terminals stay, for an iTerm2 that stopped answering.

    When iTerm2 restores sessions through its session server (iTermServer), every terminal belongs
    to the server, not to the app: the app can be stopped, and opened again it reconnects to them,
    so the programs in them (a tmux client, a shell, a long job) never notice. The app is stopped
    with SIGKILL, because an app that stopped answering usually ignores a polite quit too.

    Refused, with nothing stopped, unless exactly one iTerm2 and one iTermServer run, the server
    holds at least one terminal and the app itself holds none (stopping it would end those).
    Returns (outcome, detail): "success" when iTerm2 answers again and the server still holds as
    many terminals as before, "partial" when it holds fewer, "failure" otherwise; detail carries
    "message", "terminals_before" and "terminals_after"."""
    p = procs or Processes(env)
    apps, servers = p.app(), p.server()
    if len(apps) != 1:
        return "failure", {"message": f"expected one iTerm2 process, found {len(apps)}; nothing was stopped"}
    if len(servers) != 1:
        return "failure", {"message": "iTerm2's session server is not running (or more than one is), so "
                                      "stopping the app would end its terminals; nothing was stopped"}
    app, server = apps[0], servers[0]
    held, own = p.children(server), p.children(app)
    if held <= 0:
        return "failure", {"message": "iTerm2's session server holds no terminal, so they belong to the "
                                      "app and would end with it; nothing was stopped"}
    if own != 0:
        return "failure", {"message": f"iTerm2 itself holds {own} process(es), which stopping it would end; "
                                      "nothing was stopped"}
    p.kill(app)
    for _ in range(40):
        if not p.alive(app):
            break
        p.sleep(0.25)
    else:
        return "failure", {"message": "iTerm2 did not stop"}
    if not p.open():
        return "failure", {"message": "iTerm2 stopped, and could not be opened again"}
    waited = 0.0
    while not p.answers():
        if waited >= answer_wait:
            return "failure", {"message": f"iTerm2 opened again but did not answer within {answer_wait:.0f} s",
                               "terminals_before": held}
        p.sleep(3)
        waited += 3
    kept = p.children(server)
    return ("success" if kept >= held else "partial"), {
        "message": f"iTerm2 restarted; its session server holds {kept} of {held} terminals",
        "terminals_before": held, "terminals_after": kept}
