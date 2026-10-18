"""owners: which owner's pattern matches a subject most specifically.

Several agents sharing work need one answer to "whose is this?". Each owner lists patterns, and the
pattern that matches with the most literal characters wins, so an exact address beats a wide one.
"""
from __future__ import annotations

import fnmatch
import re


def _like(pattern: str) -> re.Pattern:
    return re.compile("".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
                              for ch in pattern) + r"\Z")


def _literal(pattern: str, kind: str) -> int:
    wild = "%_" if kind == "like" else "*?"
    return sum(1 for ch in pattern if ch not in wild)


def most_specific(subject: str, owners: dict[str, list[str]], *, kind: str = "like") -> str | None:
    """The owner whose pattern matches `subject` with the most literal characters, or None.

    `kind` is "like" (SQL LIKE, `%` any run and `_` one character) or "glob" (shell globs, `*` any
    run including folders and `?` one character). A tie goes to the owner first in alphabetical
    order, so the answer never depends on the order the owners were given in."""
    if kind not in ("like", "glob"):
        raise ValueError(f"kind is like or glob, not {kind!r}")
    hits = []
    for owner, patterns in owners.items():
        for p in patterns:
            ok = _like(p).match(subject) if kind == "like" else fnmatch.fnmatchcase(subject, p)
            if ok:
                hits.append((-_literal(p, kind), owner))
    return sorted(hits)[0][1] if hits else None
