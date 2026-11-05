"""dependency_bump.py — read a dependency update pull request before merging it on its own.

A bot such as Dependabot opens one pull request per update, and a maintainer who merges them by hand
reads the same things each time: who opened it, whether every check passed, whether it changes only
lockfiles and dependency manifests, and how far each version moves. `review` answers those from the
pull request as gh prints it (title, body, author, files), and `claude-ops merge --dependency-bump
minor` merges only when the answer is yes, with the checks read as always.

    claude-ops merge 123 --repo owner/name --dependency-bump patch   # a bot's patch bump, nothing else
    claude-ops merge 123 --repo owner/name --dependency-bump minor   # patch or minor

What counts. The author is one of the named updaters (UPDATERS: Dependabot and Renovate by the logins
gh prints for them; a caller may name more), never any bot: every GitHub App with pull-request write
is a bot too. Every changed file is a lockfile or a dependency manifest (LOCKFILES, MANIFESTS,
`requirements*.txt`). Every bump the title or body names ("bump X from A to B", "Updates `X` from
A to B") moves at most by the level asked, and, when the diff is given, every package a manifest or a
lockfile moves is one the text names, moves to the version the text names and within the level, and
keeps the same registry. A package the diff adds or removes, moves without the text naming it (a
transitive dependency across a major), lands on a version the text does not name, is pointed at
another registry host, or is newly marked to run an install script, is refused.
The formats read are npm's (package.json, package-lock.json, npm-shrinkwrap.json), yarn.lock, Python's
(pyproject.toml, requirements*.txt, uv.lock, poetry.lock, pdm.lock), Cargo's, go.mod and composer's;
a lockfile in another format contributes nothing, so for it the text is the only check. A version is
read
as a semantic version: a change of the first number is major, of the second minor, of anything after
patch; a 0.x version counts its second number as major, since that is where such a release puts its
breaking changes. A bump that cannot be read (no "from A to B", a version that is not numbers, a move
backwards) is refused, because the reader does not know what it would be merging. A workflow file, a
Dockerfile or any source file in the change is refused too: those run code, a manifest only names it.
"""
from __future__ import annotations

import posixpath
import re
from typing import NamedTuple, Sequence

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


#: the updaters whose pull requests a program may merge on its own, by the logins gh prints for them:
#: a GitHub App's login is `app/<slug>` on a pull request, the bot account's is `<slug>[bot]`. Named,
#: never "any bot": every App installed with pull-request write is a bot too, and a login starting
#: `app/` passed before, so such an App could name a harmless bump and point a lockfile elsewhere.
UPDATERS: tuple[str, ...] = ("app/dependabot", "dependabot[bot]", "app/renovate", "renovate[bot]")


def is_bot(author, bots: Sequence[str] = UPDATERS) -> bool:
    """Whether the author gh printed ({"login": ..., "is_bot": ...}) or a bare login is one of the
    named updaters. gh's own `is_bot` flag is not read: it is true for every GitHub App."""
    if isinstance(author, dict):
        author = author.get("login") or ""
    login = str(author or "")
    return login in bots


class Move(NamedTuple):
    """One package a manifest or lockfile changes in the diff: `old` is None when it is added, `new`
    when it is removed."""
    file: str
    name: str
    old: str | None
    new: str | None


_DIFF_FILE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)")
_SKIP = ("---", "+++", "@@", "index ", "new file", "deleted file", "similarity", "rename ", "old mode", "new mode")
_JSON_PAIR = re.compile(r'^[-+]\s*"(?P<name>[^"]+)":\s*"(?P<ver>[^"]*)",?\s*$')
_JSON_OBJ = re.compile(r'^[ +-]\s*"(?P<key>[^"]*)":\s*\{')
_JSON_NAME = re.compile(r'^[ +-]\s*"name":\s*"(?P<name>[^"]+)"')
_JSON_VERSION = re.compile(r'^[-+]\s*"version":\s*"(?P<ver>[^"]+)",?\s*$')
_TOML_INLINE = re.compile(r'^[-+]\s*"?(?P<name>[A-Za-z0-9_.\-]+)"?\s*=\s*\{[^}]*\bversion\s*=\s*"(?P<ver>[^"]+)"')
_TOML_PAIR = re.compile(r'^[-+]\s*"?(?P<name>[A-Za-z0-9_.\-]+)"?\s*=\s*"(?P<ver>[^"]*)"\s*(?:#.*)?$')
_TOML_NAME = re.compile(r'^[ +-]\s*name\s*=\s*"(?P<name>[^"]+)"')
_TOML_VERSION = re.compile(r'^[-+]\s*version\s*=\s*"(?P<ver>[^"]+)"')
_PEP508 = re.compile(r'^[-+]\s*"?(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(?:==|>=|~=|<=|!=|>|<)=?\s*(?P<ver>[^",;\s]+)')
_YARN_HEADER = re.compile(r'^[ +-]?"?(?P<name>@?[^@"\s]+)@')
_YARN_VERSION = re.compile(r'^[-+]\s+version\s+"(?P<ver>[^"]+)"')
_GOMOD = re.compile(r'^[-+]\s*(?P<name>[a-z0-9.\-]+\.[a-z]+/\S+)\s+(?P<ver>v\d\S*)')
_SPEC = re.compile(r"^\s*[\^~>=<!*\s]*v?\d")
#: JSON keys that open an object or name a value but are no package
_STRUCTURAL = frozenset({"", "dependencies", "devDependencies", "peerDependencies", "optionalDependencies",
                         "packages", "requires", "engines", "bin", "funding", "scripts", "overrides",
                         "resolutions", "require", "require-dev", "platform", "name", "version",
                         "description", "main", "module", "types", "license", "type", "private", "author",
                         "homepage", "repository", "exports", "files", "lockfileVersion"})
_TOML_STRUCTURAL = frozenset({"name", "version", "description", "readme", "license", "edition", "python",
                              "requires-python", "build-backend", "authors", "source", "checksum",
                              "resolution-markers", "requires-dist", "url", "hash", "size", "upload-time"})
#: the line that names where a package is fetched from, in an npm lockfile and in yarn.lock
_JSON_RESOLVED = re.compile(r'^(?P<sign>[-+])\s*"resolved":\s*"(?P<url>[^"]+)"')
_YARN_RESOLVED = re.compile(r'^(?P<sign>[-+])\s+resolved\s+"(?P<url>[^"]+)"')
#: a lifecycle script in a manifest, and the mark npm writes for a package that runs one
_INSTALL_KEY = re.compile(r'^\+\s*"(?P<key>preinstall|install|postinstall|prepare|prepublish'
                          r'|prepublishOnly|prepack|postpack|preuninstall|postuninstall)"\s*:')
_HAS_INSTALL = re.compile(r'^(?P<sign>[-+])\s*"hasInstallScript":\s*(?P<val>true|false)')


def _host(url: str | None) -> str | None:
    """The host of a URL ("https://registry.npmjs.org/x/-/x-1.0.0.tgz" is "registry.npmjs.org"),
    or None when there is no scheme and host to read."""
    m = re.match(r"[A-Za-z][A-Za-z0-9+.\-]*://([^/]+)", url or "")
    return m.group(1).lower() if m else None


def _canon(version: str) -> tuple[int, ...] | str:
    """A version in a form two spellings of the same release share. "v1.4.0", "1.4.0" and "1.4"
    all become (1, 4). A version that is not numbers keeps its cleaned string, so it only ever
    equals the same string."""
    n = numbers(version)
    if n is None:
        return clean_version(version)
    parts = list(n)
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _kind(path: str) -> str | None:
    """How a changed file's diff is read, or None for a format this does not read."""
    name = posixpath.basename(path)
    if name in ("package.json", "composer.json"):
        return "json-manifest"
    if name in ("package-lock.json", "npm-shrinkwrap.json", "composer.lock", "packages.lock.json"):
        return "json-lock"
    if name in ("pyproject.toml", "Cargo.toml", "Pipfile", "libs.versions.toml"):
        return "toml-manifest"
    if name in ("uv.lock", "poetry.lock", "Cargo.lock", "pdm.lock"):
        return "toml-lock"
    if REQUIREMENTS.match(name):
        return "requirements"
    if name == "yarn.lock":
        return "yarn"
    if name == "go.mod":
        return "gomod"
    return None


def clean_version(spec: str) -> str:
    """A version out of a range or pin: "^1.2.3" and ">= 1.2.3, <2" are 1.2.3."""
    s = re.sub(r"^[\^~>=<!=\s]*", "", (spec or "").strip())
    return re.split(r"[\s,]", s, maxsplit=1)[0] if s else s


def moves(diff: str) -> tuple[Move, ...]:
    """Every package a manifest or lockfile moves, adds or removes in a unified diff (`gh pr diff`),
    for the formats _kind reads. Pure; a file in another format contributes nothing."""
    out: dict[tuple[str, str], list] = {}

    def note(file: str, name: str, line: str, ver: str) -> None:
        slot = out.setdefault((file, name), [None, None])
        slot[0 if line[0] == "-" else 1] = ver

    file = kind = current = None
    for line in diff.splitlines():
        m = _DIFF_FILE.match(line)
        if m:
            file, kind, current = m.group("b"), _kind(m.group("b")), None
            continue
        if not file or kind is None or line.startswith(_SKIP) or not line:
            continue
        if kind == "json-manifest":
            m = _JSON_PAIR.match(line)
            if m and m.group("name") not in _STRUCTURAL and _SPEC.match(m.group("ver")):
                note(file, m.group("name"), line, m.group("ver"))
        elif kind == "json-lock":
            m = _JSON_OBJ.match(line)
            if m:
                key = m.group("key")
                if key not in _STRUCTURAL:
                    current = key.rsplit("node_modules/", 1)[-1]
                continue
            m = _JSON_NAME.match(line)
            if m:
                current = m.group("name")
                continue
            m = _JSON_VERSION.match(line)
            if m and current:
                note(file, current, line, m.group("ver"))
        elif kind == "toml-manifest":
            m = _TOML_INLINE.match(line) or _TOML_PAIR.match(line) or _PEP508.match(line)
            if m and m.group("name") not in _TOML_STRUCTURAL and _SPEC.match(m.group("ver")):
                note(file, m.group("name"), line, m.group("ver"))
        elif kind == "toml-lock":
            m = _TOML_NAME.match(line)
            if m:
                current = m.group("name")
                continue
            m = _TOML_VERSION.match(line)
            if m and current:
                note(file, current, line, m.group("ver"))
        elif kind == "requirements":
            m = _PEP508.match(line)
            if m:
                note(file, m.group("name"), line, m.group("ver"))
        elif kind == "yarn":
            if line[1:2] not in (" ", "\t") and line.rstrip().endswith(":"):
                m = _YARN_HEADER.match(line)
                current = m.group("name") if m else None
                continue
            m = _YARN_VERSION.match(line)
            if m and current:
                note(file, current, line, m.group("ver"))
        elif kind == "gomod":
            m = _GOMOD.match(line)
            if m:
                note(file, m.group("name"), line, m.group("ver"))
    return tuple(Move(f, n, o, w) for (f, n), (o, w) in out.items())


def _registry_hazard(diff: str) -> str | None:
    """A package the diff re-points to a different registry host, which a bump to a new version from
    the same registry never does. Reads the `resolved` line of an npm lockfile and of yarn.lock. A
    change of only the version in the URL keeps the host, so this fires only on a host that moved."""
    host: dict[tuple[str, str], list] = {}
    file = kind = current = None
    for line in diff.splitlines():
        m = _DIFF_FILE.match(line)
        if m:
            file, kind, current = m.group("b"), _kind(m.group("b")), None
            continue
        if not file or kind is None or line.startswith(_SKIP) or not line:
            continue
        if kind == "json-lock":
            m = _JSON_OBJ.match(line)
            if m:
                key = m.group("key")
                current = key.rsplit("node_modules/", 1)[-1] if key not in _STRUCTURAL else None
                continue
            m = _JSON_NAME.match(line)
            if m:
                current = m.group("name")
                continue
            m = _JSON_RESOLVED.match(line)
        elif kind == "yarn":
            if line[1:2] not in (" ", "\t") and line.rstrip().endswith(":"):
                mh = _YARN_HEADER.match(line)
                current = mh.group("name") if mh else None
                continue
            m = _YARN_RESOLVED.match(line)
        else:
            continue
        if m and current:
            slot = host.setdefault((file, current), [None, None])
            slot[0 if m.group("sign") == "-" else 1] = _host(m.group("url"))
    for (file, name), (old, new) in host.items():
        if old and new and old != new:
            return f"the diff points {name} at {new} instead of {old} in {file}"
    return None


def _install_hazard(diff: str) -> str | None:
    """An install script the diff adds. In a manifest that is a lifecycle key under scripts
    (postinstall and the rest), which runs a command on install. In an npm lockfile it is a package
    newly marked hasInstallScript. A package that already ran one shows the mark on both sides of the
    diff, so only a mark the diff adds counts."""
    had: set[tuple[str, str]] = set()
    gained: list[tuple[str, str]] = []
    file = kind = current = None
    for line in diff.splitlines():
        m = _DIFF_FILE.match(line)
        if m:
            file, kind, current = m.group("b"), _kind(m.group("b")), None
            continue
        if not file or kind is None or line.startswith(_SKIP) or not line:
            continue
        if kind == "json-manifest":
            m = _INSTALL_KEY.match(line)
            if m:
                return f"the diff adds the install script {m.group('key')} in {file}"
        elif kind == "json-lock":
            m = _JSON_OBJ.match(line)
            if m:
                key = m.group("key")
                current = key.rsplit("node_modules/", 1)[-1] if key not in _STRUCTURAL else None
                continue
            m = _JSON_NAME.match(line)
            if m:
                current = m.group("name")
                continue
            m = _HAS_INSTALL.match(line)
            if m and current and m.group("val") == "true":
                (had.add if m.group("sign") == "-" else gained.append)((file, current))
    for file, name in gained:
        if (file, name) not in had:
            return f"the diff marks {name} in {file} as running an install script"
    return None


def _diff_verdict(found: tuple[Bump, ...], diff: str, up_to: str) -> str | None:
    """Why the diff refuses the update, or None when every package it moves is named, moves to the
    version the text names and within the level, from the same registry, and the diff adds no install
    script."""
    named = {b.name.lower() for b in found}
    named_new: dict[str, set] = {}
    for b in found:
        named_new.setdefault(b.name.lower(), set()).add(_canon(b.new))
    for mv in moves(diff):
        old = clean_version(mv.old) if mv.old is not None else None
        new = clean_version(mv.new) if mv.new is not None else None
        if old is not None and new is not None and old == new:
            continue
        if mv.old is None:
            return f"the diff adds {mv.name} {new} in {mv.file}"
        if mv.new is None:
            return f"the diff removes {mv.name} from {mv.file}"
        if mv.name.lower() not in named:
            return (f"the diff moves {mv.name} from {old} to {new} in {mv.file}, which the title and body "
                    f"do not name")
        lv = level(old, new)
        if lv is None:
            return f"the move of {mv.name} from {old} to {new} in {mv.file} cannot be read"
        if LEVELS.index(lv) > LEVELS.index(up_to):
            return f"{lv} move in {mv.file}: {mv.name} {old} to {new} (allowed up to {up_to})"
        if _canon(new) not in named_new.get(mv.name.lower(), set()):
            want = " or ".join(dict.fromkeys(b.new for b in found if b.name.lower() == mv.name.lower()))
            return (f"the diff moves {mv.name} to {new} in {mv.file}, but the title and body name {want}")
    return _registry_hazard(diff) or _install_hazard(diff)


def review(title: str | None, body: str | None, files, author=None, *, up_to: str = "minor",
           bots_only: bool = True, diff: str | None = None, bots: Sequence[str] = UPDATERS) -> Verdict:
    """Pure. Whether this pull request is a dependency update a program may merge on its own, and
    why not when it is not. `files` are the changed paths; `up_to` is the largest move allowed;
    `diff` is the pull request's unified diff, read when given (moves); `bots` the updaters."""
    if up_to not in LEVELS:
        return Verdict(False, f"the level is one of {', '.join(LEVELS)}, not {up_to!r}", None, ())
    found = bumps(title, body)
    if bots_only and not is_bot(author, bots):
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
    if diff is not None:
        why = _diff_verdict(found, diff, up_to)
        if why:
            return Verdict(False, why, worst, found)
    return Verdict(True, f"a bot's {worst} bump of " + ", ".join(b.name for b in found)
                   + ", touching only lockfiles and manifests"
                   + (", and the diff moves nothing else" if diff is not None else ""), worst, found)


def review_pr(pr: dict, *, up_to: str = "minor", bots_only: bool = True, diff: str | None = None,
              bots: Sequence[str] = UPDATERS) -> Verdict:
    """`review` over a pull request as `gh pr view --json title,body,author,files` prints it, with
    `diff` as `gh pr diff` prints it."""
    files = [f.get("path", "") for f in (pr.get("files") or []) if isinstance(f, dict)]
    return review(pr.get("title"), pr.get("body"), files, pr.get("author"), up_to=up_to,
                  bots_only=bots_only, diff=diff, bots=bots)
