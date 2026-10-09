"""Give a chat its own git worktree that still knows what the main checkout knows.

Several chats in one checkout switch branches under each other (one chat committed onto another's
branch and renamed it). So a chat working in a git repo gets `~/worktrees/<repo>-<name>`, made
from the remote's default branch on a branch `chat/<name>`.

- Two things do not follow a worktree. A file the main checkout keeps out of git through
  `.git/info/exclude` (a local CLAUDE.md, say) is not in it, and the chat's memory lives in a
  project folder named after the worktree's path, not the repo's. Both are linked here.
- `claude --worktree` is not used: it puts the worktree inside the repo, which inside a synced
  folder (Insync, Dropbox) would sync every worktree.
- Claude Code's folder name for a path changed. Older versions turned only "/" into "-", newer ones
  turn "." into "-" too. Memory is looked for under both, and worktree paths have no dots.
- The name is used as given. A disk that ignores case would let `Fix` and `fix` share a folder, so
  a folder is reused only when its branch is `chat/<name>`, and refused otherwise.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

WORKTREES = Path(os.path.expanduser("~/worktrees"))
PROJECTS = Path(os.path.expanduser("~/.claude/projects"))
GIT = "/usr/bin/git"
#: a chat name as it is typed, where Claude and tmux allow it: letters of either case, digits,
#: dashes and underscores, never a dot or a colon (tmux refuses both) and never a space
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

Env = Optional[Mapping[str, str]]


def _git(cwd: Path, *args: str, check: bool = True, env: Env = None) -> str:
    r = subprocess.run([GIT, "-C", str(cwd), *args], capture_output=True, text=True, timeout=120,
                       env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def repo_root(path: Path, *, env: Env = None) -> Path | None:
    """The top of the git checkout `path` is in, or None when it is in none."""
    try:
        top = _git(path, "rev-parse", "--show-toplevel", env=env)
    except (RuntimeError, OSError):
        return None
    return Path(top) if top else None


def home_repo(path: Path, *, env: Env = None) -> Path:
    """The main checkout a folder belongs to (a worktree's too), or the folder when it is no repo."""
    try:
        common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env)
    except (RuntimeError, OSError):
        return path.expanduser().resolve()
    return Path(common).resolve().parent


def project_dirs(path: Path, projects: Path = PROJECTS) -> list[Path]:
    """Claude Code's project folder for `path`, under both spellings it has used, newest first."""
    s = str(path)
    new, old = projects / s.replace("/", "-").replace(".", "-"), projects / s.replace("/", "-")
    return [new] if new == old else [new, old]


def excluded_files(root: Path) -> list[Path]:
    """Plain paths `.git/info/exclude` keeps out of git that exist in the main checkout. Globs and
    negations are skipped: linking a pattern would be a guess."""
    exclude = root / ".git" / "info" / "exclude"
    out: list[Path] = []
    try:
        lines = exclude.read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        p = ln.split("#", 1)[0].strip().strip("/")
        if not p or p.startswith("!") or any(ch in p for ch in "*?["):
            continue
        if (root / p).exists() and (root / p) not in out:
            out.append(root / p)
    return out


def default_ref(root: Path, *, env: Env = None) -> str:
    """The remote's default branch, e.g. origin/master; HEAD when there is no remote."""
    try:
        _git(root, "fetch", "-q", "origin", env=env)
        ref = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False, env=env)
        if ref:
            return ref
        for b in ("origin/master", "origin/main"):
            if _git(root, "rev-parse", "--verify", "-q", b, check=False, env=env):
                return b
    except (RuntimeError, OSError):
        pass
    return "HEAD"


def owner_branch(wt: Path, *, env: Env = None) -> str | None:
    """The branch a worktree has checked out, or None when it is not a worktree."""
    try:
        return _git(wt, "branch", "--show-current", env=env) or None
    except (RuntimeError, OSError):
        return None


def prepare(folder: Path, name: str, *, worktrees: Path = WORKTREES,
            projects: Path = PROJECTS, env: Env = None) -> dict:
    """Where a chat named `name` asked to work in `folder` should start, and what was linked.

    Not a repo: the folder itself, nothing made. A repo: its own worktree (reused if it already
    exists and is this chat's), every excluded file linked in, and its memory folder linked to the
    main checkout's. Raises ValueError for a name NAME refuses, or a folder that is another chat's."""
    if not NAME.match(name):
        raise ValueError(f"chat name {name!r}: letters, digits, dashes and underscores, up to 40")
    folder = folder.expanduser().resolve()
    root = repo_root(folder, env=env)
    if root is None:
        return {"cwd": str(folder), "worktree": None, "linked": [], "memory": None}
    repo_part = re.sub(r"[^a-z0-9-]", "-", root.name.lower())
    wt = worktrees / f"{repo_part}-{name}"
    # a folder made by an older version, which lowercased the name: reused only by its own branch
    legacy = worktrees / f"{repo_part}-{re.sub(r'[^a-z0-9-]', '-', name.lower())}"
    if (not wt.exists() and legacy != wt and legacy.exists()
            and owner_branch(legacy, env=env) == f"chat/{name}"):
        wt = legacy
    if wt.exists():
        # the filesystem may ignore case, so `Fix` finds `fix`'s folder: only its own is reused
        if owner_branch(wt, env=env) != f"chat/{name}":
            raise ValueError(f"{wt} is another chat's worktree ({owner_branch(wt, env=env) or 'no branch'})")
    else:
        worktrees.mkdir(parents=True, exist_ok=True)
        _git(root, "worktree", "add", "-q", "-b", f"chat/{name}", str(wt), default_ref(root, env=env),
             env=env)
    linked = []
    for src in excluded_files(root):
        dst = wt / src.relative_to(root)
        if not dst.exists() and not dst.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
            linked.append(str(src.relative_to(root)))
    main_memory = next((d / "memory" for d in project_dirs(root, projects)
                        if (d / "memory").is_dir()), project_dirs(root, projects)[0] / "memory")
    main_memory.mkdir(parents=True, exist_ok=True)
    # Claude Code names the folder after the RESOLVED path (/tmp is /private/tmp)
    wt_dir = project_dirs(wt.resolve(), projects)[0]
    wt_dir.mkdir(parents=True, exist_ok=True)
    mem = wt_dir / "memory"
    if not mem.exists() and not mem.is_symlink():
        mem.symlink_to(main_memory)
    # start where it was asked: a subfolder is the same place inside the chat's own worktree
    sub = folder.relative_to(root.resolve()) if folder != root.resolve() else Path()
    cwd = wt / sub
    if not cwd.is_dir():
        cwd = wt
    return {"cwd": str(cwd), "worktree": str(wt), "repo": str(root), "linked": linked,
            "memory": str(main_memory), "subfolder": str(sub) if str(sub) != "." else ""}


def unsafe(wt: Path, *, env: Env = None) -> str | None:
    """Why removing this worktree would lose work, or None: uncommitted changes, or commits on its
    branch that no remote branch holds."""
    if not wt.exists():
        return None
    dirty = _git(wt, "status", "--porcelain", "--untracked-files=normal", check=False, env=env)
    if dirty.strip():
        return "it has uncommitted changes"
    ahead = _git(wt, "rev-list", "--count", "HEAD", "--not", "--remotes", check=False, env=env)
    if ahead.strip() not in ("", "0"):
        return f"its branch has {ahead.strip()} commit(s) no remote holds"
    return None


def remove(wt: Path, *, projects: Path = PROJECTS, env: Env = None) -> None:
    """Remove a worktree whose work is safe (check `unsafe` first), its chat/ branch, and its
    memory link. The main checkout's memory folder itself is never touched."""
    if not wt.exists():
        return
    root = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env)).parent
    branch = _git(wt, "branch", "--show-current", check=False, env=env)
    resolved = wt.resolve()
    _git(root, "worktree", "remove", str(wt), env=env)
    if branch.startswith("chat/"):
        _git(root, "branch", "-D", branch, check=False, env=env)
    for d in project_dirs(resolved, projects):
        m = d / "memory"
        if m.is_symlink():
            m.unlink()
