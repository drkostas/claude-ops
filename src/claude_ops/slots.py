"""Share several Playwright MCP servers between Claude chats without lock collisions.

Run N Playwright MCP servers, each with its own Chrome profile folder (playwright-1 ... playwright-N).
A chat claims the first free one. Chrome itself marks a profile in use with a `SingletonLock`
symlink whose target is "<hostname>-<pid>", so the lock is the claim: free when there is no lock,
in use when the pid is alive, stale when the pid is dead (a crashed browser), and a stale lock can be
removed.

The lock is a symlink to a name that is not a real path, so `os.path.exists` follows it and says
False even for a held lock. Always use `os.path.islink`.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_OPS_PLAYWRIGHT_ROOT", Path.home() / ".claude" / "playwright-profiles"))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # another user's process exists
    except OSError:
        return False
    return True


def read(profile: Path) -> dict:
    lock = profile / "SingletonLock"
    if not os.path.islink(lock) and not lock.exists():
        return {"slot": profile.name, "state": "free", "pid": None}
    try:
        tail = os.readlink(lock).rsplit("-", 1)[-1]
    except OSError:
        tail = ""
    if not tail.isdigit():
        return {"slot": profile.name, "state": "stale", "pid": None}
    pid = int(tail)
    return {"slot": profile.name, "state": "in-use" if alive(pid) else "stale", "pid": pid}


def status(root: Path = ROOT) -> list[dict]:
    profiles = sorted((p for p in root.glob("*") if p.is_dir()), key=lambda p: (len(p.name), p.name))
    return [read(p) for p in profiles]


def first_free(root: Path = ROOT) -> str | None:
    return next((s["slot"] for s in status(root) if s["state"] == "free"), None)


def release_stale(root: Path = ROOT) -> list[str]:
    """Remove locks whose browser is gone. Chrome writes a new one the next time it opens the profile."""
    done = []
    for s in status(root):
        if s["state"] == "stale":
            (root / s["slot"] / "SingletonLock").unlink(missing_ok=True)
            done.append(s["slot"])
    return done
