"""dependency_bump.py — read a dependency update pull request before merging it on its own.

A bot such as Dependabot opens one pull request per update, and a maintainer who merges them by hand
reads the same things each time: who opened it, whether every check passed, whether it changes only
lockfiles and dependency manifests, and how far each version moves. `review` answers those from the
pull request as gh prints it (title, body, author, files), and `claude-ops merge --dependency-bump
minor` merges only when the answer is yes, with the checks read as always.

    claude-ops merge 123 --repo owner/name --dependency-bump patch   # a bot's patch bump, nothing else
    claude-ops merge 123 --repo owner/name --dependency-bump minor   # patch or minor

What counts. The author is a bot (gh's `is_bot`, or a login ending in `[bot]`). Every changed file is
a lockfile or a dependency manifest (LOCKFILES, MANIFESTS, `requirements*.txt`). Every bump the title
or body names ("bump X from A to B", "Updates `X` from A to B") moves at most by the level asked, read
as a semantic version: a change of the first number is major, of the second minor, of anything after
patch; a 0.x version counts its second number as major, since that is where such a release puts its
breaking changes. A bump that cannot be read (no "from A to B", a version that is not numbers, a move
backwards) is refused, because the reader does not know what it would be merging. A workflow file, a
Dockerfile or any source file in the change is refused too: those run code, a manifest only names it.
"""
from __future__ import annotations

import posixpath
import re
from typing import NamedTuple

LEVELS = ("patch", "minor", "major")

#: files a package manager writes: a change here is a pinned version moving
LOCKFILES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    "poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock", "Cargo.lock", "go.sum", "Gemfile.lock",
    "composer.lock", "mix.lock", "pubspec.lock", "packages.lock.json", "gradle.lockfile", "flake.lock",
})
#: files a person writes to name dependencies: a bump edits a version in them
MANIFESTS = frozenset({
    "package.json", "pyproject.toml", "Pipfile", "Cargo.toml", "go.mod", "Gemfile", "composer.json",
    "mix.exs", "pubspec.yaml", "setup.cfg", "build.gradle", "build.gradle.kts", "libs.versions.toml",
})
REQUIREMENTS = re.compile(r"^requirements[\w.-]*\.txt$")

_VERSION = r"v?(\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?)"
#: "bump X from A to B" (a title), "Bumps [X](url) from A to B." and "Updates `X` from A to B" (a body)
BUMP = re.compile(r"\b(?:bumps?|updates?)\s+\[?`?(?P<name>[^\s`\[\]()]+)`?\]?(?:\([^)]*\))?"
                  r"\s+from\s+`?" + _VERSION.replace("(", "(?P<old>", 1) + r"`?\s+to\s+`?"
                  + _VERSION.replace("(", "(?P<new>", 1) + "`?", re.IGNORECASE)


class Bump(NamedTuple):
    name: str
    old: str
    new: str


class Verdict(NamedTuple):
    ok: bool
    reason: str
    level: str | None       # the largest move among the bumps, or None when one could not be read
    bumps: tuple[Bump, ...]


def numbers(version: str) -> tuple[int, ...] | None:
    """The numeric parts of a version ("v4.0.3" is (4, 0, 3)), or None for anything else: a
    pre-release, a hash, a word."""
    v = version.strip().lstrip("vV")
    parts = v.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def level(old: str, new: str) -> str | None:
    """How far a version moved: "major", "minor" or "patch", or None when the move cannot be read or
    goes backwards. A 0.x version's second number counts as major."""
    a, b = numbers(old), numbers(new)
    if a is None or b is None or b <= a:
        return None
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    if a[0] != b[0]:
        return "major"
    if width > 1 and a[1] != b[1]:
        return "major" if a[0] == 0 else "minor"
    return "patch"


def bumps(title: str | None, body: str | None) -> tuple[Bump, ...]:
    """Every "X from A to B" the title and body name, each once, in order of appearance."""
    seen: dict[Bump, None] = {}
    for text in (title or "", body or ""):
        for m in BUMP.finditer(text):
            seen.setdefault(Bump(m.group("name"), m.group("old"), m.group("new")), None)
    return tuple(seen)


def is_dependency_file(path: str) -> bool:
    name = posixpath.basename(path)
    return name in LOCKFILES or name in MANIFESTS or bool(REQUIREMENTS.match(name))


def other_files(paths) -> list[str]:
    """The changed files that are neither a lockfile nor a manifest."""
    return [p for p in paths if not is_dependency_file(p)]


def is_bot(author) -> bool:
    """Whether the author gh printed ({"login": ..., "is_bot": ...}) or a bare login is a bot."""
    if isinstance(author, dict):
        if author.get("is_bot") is True:
            return True
        author = author.get("login") or ""
    login = str(author or "")
    return login.endswith("[bot]") or login.startswith("app/")


def review(title: str | None, body: str | None, files, author=None, *, up_to: str = "minor",
           bots_only: bool = True) -> Verdict:
    """Pure. Whether this pull request is a dependency update a program may merge on its own, and
    why not when it is not. `files` are the changed paths; `up_to` is the largest move allowed."""
    if up_to not in LEVELS:
        return Verdict(False, f"the level is one of {', '.join(LEVELS)}, not {up_to!r}", None, ())
    found = bumps(title, body)
    if bots_only and not is_bot(author):
        who = author.get("login") if isinstance(author, dict) else author
        return Verdict(False, f"opened by {who or 'an unknown author'}, not by a bot", None, found)
    others = other_files(files)
    if others:
        return Verdict(False, "changes files that are not lockfiles or manifests: " + ", ".join(others[:5])
                       + (f" and {len(others) - 5} more" if len(others) > 5 else ""), None, found)
    if not found:
        return Verdict(False, "no version bump could be read from the title or body", None, ())
    worst = None
    for b in found:
        lv = level(b.old, b.new)
        if lv is None:
            return Verdict(False, f"the move of {b.name} from {b.old} to {b.new} cannot be read", None, found)
        if worst is None or LEVELS.index(lv) > LEVELS.index(worst):
            worst = lv
    if LEVELS.index(worst) > LEVELS.index(up_to):
        over = [b for b in found if level(b.old, b.new) == worst]
        return Verdict(False, f"{worst} bump: " + ", ".join(f"{b.name} {b.old} to {b.new}" for b in over)
                       + f" (allowed up to {up_to})", worst, found)
    return Verdict(True, f"a bot's {worst} bump of " + ", ".join(b.name for b in found)
                   + ", touching only lockfiles and manifests", worst, found)


def review_pr(pr: dict, *, up_to: str = "minor", bots_only: bool = True) -> Verdict:
    """`review` over a pull request as `gh pr view --json title,body,author,files` prints it."""
    files = [f.get("path", "") for f in (pr.get("files") or []) if isinstance(f, dict)]
    return review(pr.get("title"), pr.get("body"), files, pr.get("author"), up_to=up_to,
                  bots_only=bots_only)
