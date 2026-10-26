"""What changed in the instruction files a chat loaded, since it loaded them.

Claude Code reads a CLAUDE.md, the files it includes and the CLAUDE.md files above the working
folder once, when a conversation starts. When one of them changes later, a supervisor can tell from
a fingerprint (`claude_ops.config`) that it changed, but not what changed, so the chat either reads
every file again (often more than a thousand lines, for an edit of one) or carries on without
reading. These files are seldom in a repository, so there is no history to ask.

This module keeps that history. `record(paths)` stores one copy per distinct content (named by its
hash) and notes, for each file, which content it had from when. The time is the file's own
modification time, not the time it was recorded, so a reader that recorded late still knows which
content a chat that started in between had read. `changes_since(paths, when)` compares each file
now with the content it had at `when`, and `summary(changes)` writes that as a few lines: the file,
the section headings the change is under, how many lines were added and removed, and the lines
themselves when there are few, or a command that prints the full diff when there are many.

- A copy is kept per content, never per session, so any number of chats that started at different
  times share them. Call `record` often (each pass of a scheduler, and before each `changes_since`),
  since a content that came and went between two calls is never seen.
- What has no copy from before `when` is reported as such (`Change.old` is None), never guessed.
- `prune(keep_since)` drops what no reader at or after `keep_since` can ask for. Pass the oldest
  time any live chat may still ask about.

The store is `~/.local/state/claude-ops/instructions`, or the folder named in
`CLAUDE_OPS_INSTRUCTIONS_STORE`.

    python3 -m claude_ops.instruction_history diff <file> <copy>   # the full diff from a copy
    claude-ops instructions diff <file> <copy>                      # the same
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

STORE = Path(os.environ.get("CLAUDE_OPS_INSTRUCTIONS_STORE")
             or Path.home() / ".local" / "state" / "claude-ops" / "instructions")
#: changed lines shown in full across one summary; above this only the headings and a command
MAX_LINES = 20
#: characters of changed lines shown in full across one summary (a markdown paragraph is one line)
MAX_CHARS = 6000
#: the characters of a copy's hash a summary prints and `diff` accepts
SHORT = 12
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass
class Change:
    """One file that differs from the content it had at a time. `old` is the copy's hash then, or
    None when no copy is that old; `new` is None when the file is gone."""
    path: Path
    old: str | None
    new: str | None
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------- the store

def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob(store: Path, digest: str) -> Path:
    return store / "copies" / digest


def _history_file(store: Path) -> Path:
    return store / "history.json"


@contextlib.contextmanager
def _locked(store: Path):
    store.mkdir(parents=True, exist_ok=True)
    with open(store / "lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load(store: Path) -> dict[str, list[list]]:
    try:
        data = json.loads(_history_file(store).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _save(store: Path, history: dict) -> None:
    _write_atomic(_history_file(store), json.dumps(history, indent=1, sort_keys=True).encode())


def _key(path: os.PathLike | str) -> str:
    return str(Path(path).expanduser().absolute())


def record(paths, store: Path | None = None) -> list[Path]:
    """Keep a copy of each file whose content differs from the last one noted for it, noted from
    its modification time. Returns the files that got a new entry. A file that cannot be read is
    skipped."""
    store = Path(store or STORE)
    out = []
    with _locked(store):
        history = _load(store)
        for p in paths:
            p = Path(p)
            try:
                data = p.read_bytes()
                mtime = p.stat().st_mtime
            except OSError:
                continue
            digest = _hash(data)
            entries = history.setdefault(_key(p), [])
            if entries and entries[-1][1] == digest:
                continue
            blob = _blob(store, digest)
            if not blob.exists():
                _write_atomic(blob, data)
            # a file put back with an older time still came after what was noted before it
            at = max(mtime, entries[-1][0]) if entries else mtime
            entries.append([at, digest])
            out.append(p)
        if out:
            _save(store, history)
    return out


def version_at(path, when: float, store: Path | None = None) -> str | None:
    """The hash of the content `path` had at `when` (epoch seconds), or None when no copy is
    that old."""
    entries = _load(Path(store or STORE)).get(_key(path), [])
    best = None
    for at, digest in entries:
        if at <= when:
            best = digest
    return best


def copy_text(digest: str, store: Path | None = None) -> str | None:
    """A copy's text, by its full hash or a unique prefix of at least 8 characters; None when there
    is no such copy, or more than one."""
    folder = Path(store or STORE) / "copies"
    if len(digest) < 8 or not re.fullmatch(r"[0-9a-f]+", digest):
        return None
    try:
        found = [p for p in folder.iterdir() if p.name.startswith(digest)]
    except OSError:
        return None
    if len(found) != 1:
        return None
    return found[0].read_text(errors="replace")


def prune(keep_since: float, store: Path | None = None) -> int:
    """Drop the entries no reader at or after `keep_since` can ask for (for each file, every entry
    before the last one at or before it), and every copy no entry names. Returns the copies
    removed."""
    store = Path(store or STORE)
    with _locked(store):
        history = _load(store)
        dropped = False
        for key, entries in history.items():
            older = [i for i, (at, _) in enumerate(entries) if at <= keep_since]
            if len(older) > 1:
                history[key] = entries[older[-1]:]
                dropped = True
        if dropped:
            _save(store, history)
        named = {d for entries in history.values() for _, d in entries}
        removed = 0
        with contextlib.suppress(OSError):
            for blob in (store / "copies").iterdir():
                if blob.name not in named and not blob.name.startswith(".tmp-"):
                    blob.unlink()
                    removed += 1
    return removed


# ------------------------------------------------------------------------------------- the diff

def _heading_at(lines: list[str], index: int) -> str | None:
    """The nearest markdown heading at or above line `index`, outside fenced code."""
    fenced = False
    found = None
    for i, line in enumerate(lines[: index + 1]):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            m = HEADING.match(line)
            if m:
                found = line.strip()
        if i >= index:
            break
    return found


def compare(old_text: str, new_text: str) -> tuple[list[str], list[str], list[str]]:
    """Pure. (lines added, lines removed, the headings the changes are under, in file order)."""
    old, new = old_text.splitlines(), new_text.splitlines()
    added, removed, sections = [], [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        removed += old[i1:i2]
        added += new[j1:j2]
        # a change is under the heading above it in the new text; a pure removal, in the old one
        # and a heading the change itself adds or removes is a section of its own
        h = _heading_at(new, j1) if j2 > j1 else _heading_at(old, i1)
        inside = [x.strip() for x in old[i1:i2] + new[j1:j2] if HEADING.match(x)]
        for s in ([h] if h else []) + inside:
            if s not in sections:
                sections.append(s)
    return added, removed, sections


def changes_since(paths, when: float, store: Path | None = None) -> list[Change]:
    """Each file of `paths` whose content now differs from what it had at `when`."""
    store = Path(store or STORE)
    out = []
    for p in paths:
        p = Path(p)
        old = version_at(p, when, store)
        try:
            data = p.read_bytes()
        except OSError:
            data = None
        new = _hash(data) if data is not None else None
        if old == new:
            continue
        if old is None and new is not None:
            out.append(Change(p, None, new))
            continue
        old_text = copy_text(old, store) if old else ""
        if old_text is None:
            out.append(Change(p, None, new))
            continue
        added, removed, sections = compare(old_text, data.decode(errors="replace") if data else "")
        out.append(Change(p, old, new, added, removed, sections))
    return out


def _home(path: Path) -> str:
    s, home = str(path), str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def _count(n: int, word: str) -> str:
    return f"{n} line{'s' if n != 1 else ''} {word}"


def summary(changes: list[Change], max_lines: int = MAX_LINES, max_chars: int = MAX_CHARS,
            command: str = "claude-ops instructions diff") -> list[str]:
    """Pure. The lines that tell a reader what changed, file by file. The changed lines are shown
    while the whole summary stays within `max_lines` lines and `max_chars` characters of them; a
    file past that gets its headings and the command that prints its diff."""
    out = []
    lines_left, chars_left = max_lines, max_chars
    for c in changes:
        name = _home(c.path)
        if c.new is None:
            out.append(f"{name} was removed.")
            continue
        if c.old is None:
            out.append(f"{name} changed, and no copy from before is kept, so read it again.")
            continue
        parts = [x for x in (_count(len(c.added), "added") if c.added else "",
                             _count(len(c.removed), "removed") if c.removed else "") if x]
        where = (" under " + ", ".join(f'"{s}"' for s in c.sections)) if c.sections else ""
        shown = [f"  - {x}" for x in c.removed] + [f"  + {x}" for x in c.added]
        size = sum(len(x) for x in shown)
        if not shown:
            out.append(f"{name} changed only in line endings or spacing at the end of the file.")
        elif len(shown) <= lines_left and size <= chars_left:
            out.append(f"{name}: {' and '.join(parts)}{where}:")
            out += shown
            lines_left -= len(shown)
            chars_left -= size
        else:
            out.append(f"{name}: {' and '.join(parts)}{where}. "
                       f"See them with: {command} {name} {c.old[:SHORT]}")
    return out


def diff_text(path, digest: str, store: Path | None = None) -> str | None:
    """A unified diff from a kept copy to the file now, or None when the copy is not kept."""
    old = copy_text(digest, store)
    if old is None:
        return None
    try:
        new = Path(path).expanduser().read_text(errors="replace")
    except OSError:
        new = ""
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile=f"{path} (copy {digest[:SHORT]})", tofile=str(path)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops instructions",
                                description="what changed in the instruction files a chat loaded")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diff", help="the full diff from a kept copy to the file now")
    d.add_argument("file")
    d.add_argument("copy", help="the copy's hash, or the first characters of it a summary printed")
    r = sub.add_parser("record", help="keep a copy of each file whose content changed")
    r.add_argument("files", nargs="+")
    a = p.parse_args(argv)
    if a.cmd == "record":
        for f in record(a.files):
            print(f"recorded {f}")
        return 0
    text = diff_text(a.file, a.copy)
    if text is None:
        print(f"no kept copy {a.copy} (it may have been pruned)", file=sys.stderr)
        return 2
    sys.stdout.write(text or "no difference\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
