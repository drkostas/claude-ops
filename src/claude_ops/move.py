"""Move a Claude Code conversation to another project folder, so `claude --resume` finds it there.

Claude Code keeps a conversation under the project folder of the directory it ran in
(~/.claude/projects/<path-as-name>/<id>.jsonl, plus a folder <id>/ for its subagents and tool
output). To carry on the same conversation from a different directory, those files have to be in
that directory's project folder.

- Nothing leaves before everything has arrived. Every file is copied first, a copied file must
  have the same sha256 and a copied folder the same number of files, and only then are the
  originals moved, so the conversation is never in neither place.
- Originals are held, not deleted. They go to `hold`, which the caller names.
- An archive moves with it. If the caller keeps a copy of every transcript elsewhere (for example
  episodic-memory's conversation archive), pass its root as `archive`, and its copies move in the
  same step, so a restore job does not bring the old one back.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .worktree import home_repo  # noqa: F401 - part of this module's interface


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _count(d: Path) -> int:
    return sum(1 for x in d.rglob("*") if x.is_file())


def move_files(chat_id: str, src_dir: Path, dst_dir: Path, archive: Path, hold: Path) -> dict:
    """Copy the conversation (transcript, its folder, and the archive's copies) from the project
    folder `src_dir` into `dst_dir`, check every copy, then move the originals to `hold`.

    Raises before any original is moved if a copy does not match. Returns what was moved and where
    the originals are."""
    if src_dir.resolve() == dst_dir.resolve():
        return {"moved": [], "held": None, "note": "already in that folder"}
    src = src_dir / f"{chat_id}.jsonl"
    if not src.is_file():
        raise FileNotFoundError(f"no transcript {src}")
    asrc, adst = archive / src_dir.name, archive / dst_dir.name
    pairs = [(src, dst_dir / src.name)]
    if (src_dir / chat_id).is_dir():
        pairs.append((src_dir / chat_id, dst_dir / chat_id))
    for f in (f"{chat_id}.jsonl", f"{chat_id}-summary.txt", chat_id):
        if (asrc / f).exists():
            pairs.append((asrc / f, adst / f))
    for a, b in pairs:
        b.parent.mkdir(parents=True, exist_ok=True)
        if a.is_dir():
            if b.exists():
                shutil.rmtree(b)
            shutil.copytree(a, b, copy_function=shutil.copy2)
            if _count(a) != _count(b):
                raise OSError(f"file count differs after copying {a}: {_count(a)} vs {_count(b)}")
        else:
            shutil.copy2(a, b)
            if _sha(a) != _sha(b):
                raise OSError(f"checksum differs after copying {a}")
    # only now do the originals leave, all of them
    for a, _b in pairs:
        where = hold / ("archive" if archive in a.parents else "projects")
        where.mkdir(parents=True, exist_ok=True)
        shutil.move(str(a), str(where / a.name))
    return {"moved": [str(b) for _a, b in pairs], "held": str(hold)}
