"""Find any Claude Code chat, in any project folder.

Claude Code keeps every chat as ~/.claude/projects/<folder with / as ->/<session id>.jsonl, and
`claude --resume <name>` only finds a renamed chat from inside the folder it ran in. This builds a
small index of all of them (title, first message, folder, last activity) and searches it, or the
full text of every chat with ripgrep.

The index is incremental: a transcript is read again only when its size or modification time
changed. Last activity comes from the transcript's own last timestamp, not the file's modification
time, because tools that copy transcripts (backups, restore jobs) give every file a fresh time.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_OPS_PROJECTS", Path.home() / ".claude" / "projects"))
CACHE = Path(os.environ.get("CLAUDE_OPS_CACHE", Path.home() / ".cache" / "claude-ops" / "sessions.json"))
LIVE_WINDOW = 15 * 60


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(x.get("text", "") for x in (content or []) if isinstance(x, dict))


def last_activity(path: Path, fallback: float) -> float:
    """The newest `timestamp` in the last 8 KB of the transcript."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            if '"timestamp"' not in line:
                continue
            try:
                ts = json.loads(line).get("timestamp")
            except ValueError:
                continue
            if ts:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except OSError:
        pass
    return fallback


def read_meta(path: Path) -> dict:
    """Identity from the first records (folder, branch, first message), and the rename from anywhere in the file."""
    st = path.stat()
    out = {"path": str(path), "id": path.stem, "cwd": None, "branch": None, "title": None,
           "first": None, "mtime": st.st_mtime, "bytes": st.st_size}
    try:
        with path.open(errors="replace") as fh:
            for i, line in enumerate(fh):
                if '"customTitle"' in line:
                    try:
                        out["title"] = json.loads(line).get("customTitle") or out["title"]
                    except ValueError:
                        pass
                if i > 40 and out["cwd"] and out["first"]:
                    continue  # keep scanning only for a later rename
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                out["cwd"] = out["cwd"] or d.get("cwd")
                out["branch"] = out["branch"] or d.get("gitBranch")
                if out["first"] is None and d.get("type") == "user":
                    txt = _text_of((d.get("message") or {}).get("content"))
                    if txt and not txt.startswith("<"):
                        out["first"] = txt[:300]
    except OSError:
        return out
    out["active_at"] = last_activity(path, st.st_mtime)
    return out


def build_index(root: Path = ROOT, cache: Path = CACHE, force: bool = False) -> list[dict]:
    old = {}
    if cache.exists() and not force:
        try:
            old = {e["path"]: e for e in json.loads(cache.read_text())}
        except (ValueError, KeyError):
            old = {}
    idx = []
    for path in sorted(root.glob("*/*.jsonl")):
        st = path.stat()
        prev = old.get(str(path))
        if prev and abs(prev.get("mtime", 0) - st.st_mtime) < 1 and prev.get("bytes") == st.st_size:
            idx.append(prev)
        else:
            idx.append(read_meta(path))
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx))
    os.replace(tmp, cache)  # a run killed mid-write must not leave half a cache
    return sorted(idx, key=lambda e: e.get("active_at") or e["mtime"], reverse=True)


def live_folders() -> set[str]:
    """Folders a claude process is running in now. A fact about processes, not about which chat each one is."""
    out: set[str] = set()
    try:
        pids = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True).stdout.split()
    except OSError:
        return out
    for pid in pids:
        r = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True)
        out.update(ln[1:] for ln in r.stdout.splitlines() if ln.startswith("n"))
    return out


def annotate(idx: list[dict], live: set[str] | None = None, now: float | None = None) -> list[dict]:
    live = live_folders() if live is None else live
    now = time.time() if now is None else now
    for e in idx:
        recent = now - (e.get("active_at") or e["mtime"]) < LIVE_WINDOW
        e["maybe_live"] = bool(recent and e.get("cwd") in live)  # two facts joined, so only "maybe"
    return idx


def label(e: dict) -> str:
    return e.get("title") or (e.get("first") or "")[:60] or "(untitled)"


def find(idx: list[dict], text: str) -> list[dict]:
    t = text.lower()
    return [e for e in idx if t in " ".join(str(e.get(k) or "") for k in ("title", "first", "cwd", "branch")).lower()]


def ripgrep() -> str | None:
    """A real rg binary. `shutil.which` misses the one Claude Code ships, and `rg` in a shell is often a function."""
    cand = [shutil.which("rg"), "/opt/homebrew/bin/rg", "/usr/local/bin/rg", "/usr/bin/rg"]
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x64"
    plat = f"{arch}-{'darwin' if sys.platform == 'darwin' else 'linux'}"
    for base in (Path.home() / ".claude" / "plugins" / "cache", Path("/opt/homebrew/lib/node_modules"), Path("/usr/local/lib/node_modules")):
        cand += [str(p) for p in base.glob(f"**/vendor/ripgrep/{plat}/rg")][:2] if base.exists() else []
    return next((c for c in cand if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


def deep_search(text: str, root: Path = ROOT) -> dict[str, str]:
    """Session ids whose full transcript contains `text` (case-insensitive). Subagent files count for their parent chat."""
    rg = ripgrep()
    cmd = [rg, "-l", "--no-messages", "-i", "-F", text, str(root)] if rg else ["grep", "-rliF", text, str(root)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    hits: dict[str, str] = {}
    for line in r.stdout.splitlines():
        try:
            parts = Path(line).relative_to(root).parts
        except ValueError:
            continue
        if len(parts) >= 2:
            sid = parts[1][:-6] if parts[1].endswith(".jsonl") else parts[1]
            hits.setdefault(sid, line)
    return hits


def resume_command(e: dict) -> str:
    cwd = (e.get("cwd") or "~").replace(str(Path.home()), "~")
    return f"cd {cwd} && claude --resume {e['id']}"
