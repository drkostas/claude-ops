"""Bring back Claude Code chats that disappeared from ~/.claude/projects, from an archive that keeps them.

Claude Code removes old transcripts over time, and then `claude --resume` cannot find them. If
another tool keeps copies (the episodic-memory plugin archives every chat to
~/.config/superpowers/conversation-archive), this copies back only the ones that are MISSING.

- A live transcript is never overwritten. It is the newest version there is, and an archive copy
  can be older than a chat that kept going.
- Copies keep the archive's modification time, so a sync in the other direction does not treat
  every restored file as new and copy it straight back.
- More than `max_missing` missing chats at once is refused, because that means something else is
  wrong (a moved folder, a wrong path), and restoring them all would hide it.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ARCHIVE = Path(os.environ.get("CLAUDE_OPS_ARCHIVE", Path.home() / ".config" / "superpowers" / "conversation-archive"))
PROJECTS = Path(os.environ.get("CLAUDE_OPS_PROJECTS", Path.home() / ".claude" / "projects"))


def missing(archive: Path = ARCHIVE, projects: Path = PROJECTS) -> list[Path]:
    return [f for f in sorted(archive.glob("*/*.jsonl")) if not (projects / f.parent.name / f.name).exists()]


def restore(archive: Path = ARCHIVE, projects: Path = PROJECTS, max_missing: int = 200, dry_run: bool = False) -> dict:
    todo = missing(archive, projects)
    if len(todo) > max_missing:
        return {"result": "refused", "missing": len(todo), "max": max_missing, "first": str(todo[0])}
    restored = []
    for f in todo:
        dest = projects / f.parent.name / f.name
        if dry_run:
            restored.append(str(dest))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + f".restore.{os.getpid()}")
        shutil.copy2(f, tmp)  # keeps the modification time
        if dest.exists():  # a live chat appeared meanwhile, and it wins
            tmp.unlink()
            continue
        os.replace(tmp, dest)
        restored.append(str(dest))
    return {"result": "dry-run" if dry_run else "restored", "missing": len(todo), "restored": restored}
