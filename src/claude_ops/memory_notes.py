"""Memory notes that have grown large and are still being written.

Claude Code keeps a project's memory as markdown files in `~/.claude/projects/<folder>/memory/`.
A note that a session appends to all day grows without limit, and anything that republishes or
syncs the whole file on each write pays for its size every time. These functions find such notes
from their size and modification time alone. The notes themselves are never opened.

A folder may keep an index note whose lines link each session's current note, for example
`- [build state, 15 Nov](build-state-2026-11-15.md)`. When `index_name` is given, a large note is
returned with the link text of the line that links it, so a caller can tell whose it is.
`index_name` may be one name or several. Each folder's index is the first of them that the folder
holds, so folders that keep their index under different names are read in one pass.

A note the index does not link but still names was rolled away: its history was kept and a new note
took its place (the note linked on the line that names it). So was a note the index links only after
another link on the same line: a line's first link is the current note, and later links on it point
at its history. So was an unlinked note whose name
without its date (`build-state` for `build-state-2026-11-01.md`) is a linked note's. A rolled note is
left out only while it has not been written since its successor was created. One written after that
is a session still appending to its old note, and it is returned with its successor's label. Where
the file system gives no creation time (Linux), a rolled note is always left out.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PROJECTS = Path.home() / ".claude/projects"
#: a note above this many bytes counts as large
MIN_BYTES = 60_000
#: a note written within this many seconds is still being written
WITHIN_S = 86_400

LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+\.md)\)")
DATED = re.compile(r"-\d{4}-\d{2}-\d{2}.*$")


@dataclass(frozen=True)
class LargeNote:
    folder: Path          # the memory folder
    path: Path
    size: int             # bytes
    written_at: float     # modification time, seconds since the epoch
    label: str | None     # the link text of the index line that links it, None when none does


def stem(name: str) -> str:
    """A note's name without `.md` and without a date suffix: `build-state-2026-11-01-night.md`
    gives `build-state`."""
    base = name[:-3] if name.endswith(".md") else name
    return DATED.sub("", base)


@dataclass(frozen=True)
class Index:
    links: dict[str, str]                       # linked file name to its link text
    lines: list[tuple[str, list[str]]]          # each line, and the file names it links


def read_index(path: Path) -> Index:
    """The index's links and lines. A missing or unreadable index gives neither."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return Index({}, [])
    links: dict[str, str] = {}
    lines: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        found = LINK.findall(line)
        for label, target in found:
            links.setdefault(os.path.basename(target), label.strip())
        lines.append((line, [os.path.basename(t) for _l, t in found]))
    return Index(links, lines)


def index_names(index_name: str | Sequence[str] | None) -> tuple[str, ...]:
    """The index names asked for, in order: one name, several, or none. Empty names are dropped."""
    if index_name is None:
        return ()
    found = (index_name,) if isinstance(index_name, str) else tuple(index_name)
    return tuple(n for n in found if n)


def folder_index(folder: Path, wanted: Sequence[str]) -> Index:
    """The index of `folder`: the first of `wanted` that is a file there, read. None of them gives an
    empty index."""
    for name in wanted:
        if (folder / name).is_file():
            return read_index(folder / name)
    return Index({}, [])


def names(text: str, name: str) -> bool:
    """True when `text` names the file `name` as a whole name, so `a.md` is not found in `data.md`."""
    return re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(name) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])", text) is not None


def successor(name: str, index: Index) -> str | None:
    """The note that replaced `name`: the first link of a line that links it later on the line
    (`- [build state](build-state-2026-11-16.md), older history in [part c](build-state-2026-11-15.md)`),
    else the first link of a line that names it, else a line's first link with the same name without its
    date. A line's first link is the current note and later links on it are its history. None for a
    note that is the first link of a line, and for one never replaced."""
    current = [linked[0] for _line, linked in index.lines if linked]
    if name in current:
        return None
    for line, linked in index.lines:
        if name in linked[1:] or (linked and names(line, name)):
            return linked[0]
    own = stem(name)
    return next((n for n in current if stem(n) == own), None)


def birth_time(path: Path) -> float | None:
    """When the file was created, where the file system keeps it (macOS, BSD), else None."""
    try:
        return getattr(os.stat(path), "st_birthtime", None)
    except OSError:
        return None


def memory_folders(projects: Path = PROJECTS) -> list[Path]:
    """Each project's memory folder once by its real path, sorted. Several project folders can link to
    one shared memory folder, and without this every note in it would count once per link. Of the
    folders that reach one real folder, the one that is not reached through a link is kept, else the
    first by name. Raises OSError when `projects` cannot be listed."""
    kept: dict[str, Path] = {}
    for p in sorted(Path(projects).iterdir()):
        folder = p / "memory"
        if not folder.is_dir():
            continue
        real = os.path.realpath(folder)
        if real not in kept or (_through_link(kept[real]) and not _through_link(folder)):
            kept[real] = folder
    return sorted(kept.values())


def _through_link(folder: Path) -> bool:
    """Whether a memory folder, or the project folder holding it, is a symbolic link."""
    return folder.is_symlink() or folder.parent.is_symlink()


def large_notes(projects: Path = PROJECTS, *, min_bytes: int = MIN_BYTES, within_s: float = WITHIN_S,
                index_name: str | Sequence[str] | None = None, now: float | None = None,
                birth: Callable[[Path], float | None] = birth_time) -> list[LargeNote]:
    """Every memory note over `min_bytes` written within `within_s` seconds, largest first.

    Only sizes and modification times are read, the creation time of a rolled note's successor, and
    the index note when `index_name` names one. `index_name` is a name or a sequence of names, and each
    folder reads the first of them it holds. A note with one of those names is an index and is never
    taken for a rolled note. A folder that cannot be listed is skipped, and a note that disappears
    while it is read too. `birth` gives a file's creation time (None when unknown)."""
    now = time.time() if now is None else now
    wanted = index_names(index_name)
    found: list[LargeNote] = []
    try:
        folders = memory_folders(projects)
    except OSError:
        return []
    for folder in folders:
        index = folder_index(folder, wanted)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".md") or not entry.is_file(follow_symlinks=False):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_size <= min_bytes or now - st.st_mtime > within_s:
                continue
            label = index.links.get(entry.name)
            nxt = successor(entry.name, index) if wanted and entry.name not in wanted else None
            if nxt is not None:
                born = birth(folder / nxt)
                if born is None or st.st_mtime < born:
                    continue                    # rolled, and not written since
                label = index.links.get(nxt)    # still written after the roll: its successor's
            found.append(LargeNote(folder, Path(entry.path), st.st_size, st.st_mtime, label))
    return sorted(found, key=lambda n: -n.size)
