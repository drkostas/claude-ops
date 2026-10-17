"""Tell chats apart in a shared iTerm2 window by giving each tab its own color.

When several Claude Code chats run as tabs of one iTerm2 window, their titles change as they work
and look alike at a glance. A tab color that stays with each chat makes them easy to tell apart.

`assign` is the rule, with no iTerm2 in it: in every window that holds two or more chats, each tab
gets a color no other tab in that window has. A color the user set is kept, a chat takes back the
color it had before when that color is free in its window, and new colors come from PALETTE.
`chat_tabs` reads the windows and `set_tab_color` writes one tab, through iTerm2's Python API (the
`iterm2` package, installed with the `iterm` extra, and the API switched on in iTerm2's settings).
"""
from __future__ import annotations

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

    try:
        iterm2.run_until_complete(main, retry=False)
    except Exception:  # noqa: BLE001 - iTerm2 not answering: nothing is known
        return None
    return out


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

    try:
        iterm2.run_until_complete(main, retry=False)
        return True
    except Exception:  # noqa: BLE001
        return False


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
