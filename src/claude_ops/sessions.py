"""Find any Claude Code chat, in any project folder.

Claude Code keeps every chat as ~/.claude/projects/<folder with / as ->/<session id>.jsonl, and
`claude --resume <name>` only finds a renamed chat from inside the folder it ran in. This builds a
small index of all of them (title, first message, folder, last activity) and searches it, or the
full text of every chat with ripgrep.

The index is incremental: a transcript is read again only when its size or modification time
changed, and then only from where the last read stopped. Last activity comes from the transcript's
own last timestamp, not the file's modification time, because tools that copy transcripts (backups,
restore jobs) give every file a fresh time.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from .paths import PROJECTS as ROOT  # noqa: E402


def default_cache(projects: Path = ROOT) -> Path:
    """Where the session index of `projects` is kept: CLAUDE_OPS_CACHE when set, else the shared cache
    for Claude Code's own folder, else a file beside the folder. The index is keyed by the transcripts'
    paths, so one cache file serving two folders is rewritten by each in turn: a test that points
    CLAUDE_OPS_PROJECTS at fixtures replaced the real index with theirs, and the next read of the
    real folder read every transcript again (about 50 seconds for 15,000 of them)."""
    named = os.environ.get("CLAUDE_OPS_CACHE")
    if named:
        return Path(named)
    if projects == Path.home() / ".claude" / "projects":
        return Path.home() / ".cache" / "claude-ops" / "sessions.json"
    return projects.parent / "claude-ops-sessions.json"


CACHE = default_cache()
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


#: how many bytes before the resume point must be unchanged for a read to continue from there
SEAM = 256


def _seam(fh, end: int) -> str:
    """A short hash of the bytes just before `end`."""
    start = max(0, end - SEAM)
    fh.seek(start)
    return hashlib.blake2b(fh.read(end - start), digest_size=8).hexdigest()


#: the folder a record was written in; a moved conversation carries the new one from then on
_CWD = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _record(raw: bytes) -> dict | None:
    try:
        d = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def read_meta(path: Path, prev: dict | None = None) -> dict:
    """Identity from the first records (folder, branch, first message), and the rename from anywhere in the file.

    A transcript is only ever appended to, so with `prev`, this file's entry from the last read, a
    file that grew is read from where that read stopped (`read_to`, after its last whole line).
    The chats in use are the largest files, and reading one whole on each change took a second for
    a 100 MB transcript. The bytes just before that point must still hash to `seam`, so a file
    replaced by another one is read from the start again.
    """
    st = path.stat()
    out = {"path": str(path), "id": path.stem, "cwd": None, "branch": None, "title": None,
           "first": None, "mtime": st.st_mtime, "bytes": st.st_size}
    try:
        with path.open("rb") as fh:
            read_to, i = 0, 0
            if (prev and isinstance(prev.get("read_to"), int) and prev["read_to"] <= st.st_size
                    and _seam(fh, prev["read_to"]) == prev.get("seam")):
                read_to, i = prev["read_to"], prev.get("lines", 0)
                out.update({k: prev.get(k) for k in ("cwd", "branch", "title", "first")})
            fh.seek(read_to)
            for raw in fh:
                # the first records give the identity; after that only a later rename is looked for
                identity = i <= 40 or not (out["cwd"] and out["first"])
                d = _record(raw) if identity or b'"customTitle"' in raw else None
                if d is not None:
                    out["title"] = d.get("customTitle") or out["title"]
                    if identity:
                        out["cwd"] = out["cwd"] or d.get("cwd")
                        out["branch"] = out["branch"] or d.get("gitBranch")
                        if out["first"] is None and d.get("type") == "user":
                            txt = _text_of((d.get("message") or {}).get("content"))
                            if txt and not txt.startswith("<"):
                                out["first"] = txt[:300]
                # a conversation moved to another folder: every record names the folder it ran in,
                # so the newest one wins, read without parsing the whole record
                m = _CWD.search(raw)
                if m:
                    out["cwd"] = m.group(1).decode("utf-8", "replace")
                if raw.endswith(b"\n"):  # a line still being written is read again next time
                    read_to += len(raw)
                    i += 1
            out.update(read_to=read_to, lines=i, seam=_seam(fh, read_to))
    except OSError:
        return out
    out["active_at"] = last_activity(path, st.st_mtime)
    return out


#: what max_age remembers: key -> (time.monotonic() when it was read, the answer)
_RECENT: dict = {}


def _remembered(key, max_age: float, make):
    """`make()`, or the answer it gave less than `max_age` seconds ago for the same key (0 asks every time)."""
    if max_age > 0:
        hit = _RECENT.get(key)
        if hit is not None and time.monotonic() - hit[0] < max_age:
            return hit[1]
    value = make()
    if max_age > 0:
        _RECENT[key] = (time.monotonic(), value)
    return value


def build_index(root: Path = ROOT, cache: Path | None = None, force: bool = False, *,
                max_age: float = 0.0) -> list[dict]:
    """The index of every transcript under `root`, read from the cache for the files that did not change.

    Listing and checking thousands of files takes a moment even then, so a caller that asks many times in a
    row (a server building a page per request) can pass `max_age`, the number of seconds a previous answer for
    the same folder and cache may be reused. The default asks every time, and `force` always does. Each
    caller gets its own copy of the entries, so marking them (see `annotate`) never reaches another caller."""
    if max_age > 0 and not force:
        got = _remembered(("index", str(root), str(cache)), max_age, lambda: build_index(root, cache))
        return [dict(e) for e in got]
    if cache is None:
        cache = default_cache(root)  # the index of another folder never replaces this folder's
    old = {}
    if cache.exists() and not force:
        try:
            old = {e["path"]: e for e in json.loads(cache.read_text())}
        except (ValueError, KeyError):
            old = {}
    idx, changed = [], force
    # sorted by the strings: sorting 15,000 Path objects took ten times as long
    for path in sorted(root.glob("*/*.jsonl"), key=str):
        try:
            st = path.stat()
        except OSError:
            continue  # removed since the listing
        prev = old.pop(str(path), None)
        if prev and abs(prev.get("mtime", 0) - st.st_mtime) < 1 and prev.get("bytes") == st.st_size:
            idx.append(prev)
        else:
            idx.append(read_meta(path, prev))
            changed = True
    if changed or old or not cache.exists():  # `old` now holds only the transcripts that are gone
        cache.parent.mkdir(parents=True, exist_ok=True)
        # a temp file of this writer's own: with one shared name, two processes indexing at once
        # renamed each other's file away and the second failed with "No such file or directory"
        fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=cache.stem + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(idx))
            os.replace(tmp, cache)  # a run killed mid-write must not leave half a cache
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return sorted(idx, key=lambda e: e.get("active_at") or e["mtime"], reverse=True)


def live_folders(*, env: Mapping[str, str] | None = None, max_age: float = 0.0) -> set[str]:
    """Folders a claude process is running in now. A fact about processes, not about which chat each one is.
    `max_age` is the number of seconds a previous answer may be reused (0 asks every time)."""
    if max_age > 0:
        key = ("live", repr(sorted(env.items())) if env is not None else None)
        return set(_remembered(key, max_age, lambda: live_folders(env=env)))
    out: set[str] = set()
    try:
        pids = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True, env=env).stdout.split()
    except OSError:
        return out
    if not pids:
        return out
    # one lsof for all of them: one each took ten times as long with a dozen chats open. A process
    # that ended since pgrep is left out of the answer, and the others are still listed.
    r = subprocess.run(["lsof", "-a", "-p", ",".join(pids), "-d", "cwd", "-Fn"], capture_output=True, text=True, env=env)
    out.update(ln[1:] for ln in r.stdout.splitlines() if ln.startswith("n"))
    return out


def annotate(idx: list[dict], live: set[str] | None = None, now: float | None = None, *, env: Mapping[str, str] | None = None) -> list[dict]:
    live = live_folders(env=env) if live is None else live
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


def deep_search(text: str, root: Path = ROOT, *, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Session ids whose full transcript contains `text` (case-insensitive). Subagent files count for their parent chat."""
    rg = ripgrep()
    cmd = [rg, "-l", "--no-messages", "-i", "-F", text, str(root)] if rg else ["grep", "-rliF", text, str(root)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
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


#: how far a transcript's records may be out of time order when it is read backwards: subagents and
#: parallel tool calls append slightly out of order, so reading stops only this far before the window
CALLS_SLACK_S = 300.0


def _records_back(path: Path, chunk: int = 1 << 16):
    """Each JSON record of a transcript, newest first, read backwards in chunks of `chunk` bytes, so a
    long transcript is never read whole. A line that is not JSON is skipped."""
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        fh.seek(0, os.SEEK_END)
        pos, tail = fh.tell(), b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            block = fh.read(step) + tail
            lines = block.split(b"\n")
            tail = lines[0] if pos > 0 else b""
            for raw in reversed(lines if pos == 0 else lines[1:]):
                rec = _record(raw)
                if rec is not None:
                    yield rec


def _when(rec: dict) -> float | None:
    """A record's timestamp as seconds since the epoch, or None."""
    ts = rec.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def calls_near(at: float, words=(), root: Path = ROOT, *, before: float = 10.0, after: float = 1.0,
               chunk: int = 1 << 16) -> list[dict]:
    """The tool calls made from `before` seconds before `at` to `after` seconds after it, in every
    transcript under `root` (subagents' included), whose input holds every one of `words` in any case.
    Newest first, each {"session", "folder", "time", "tool", "input"}.

    A macOS consent prompt names only the process responsible for asking, often the terminal or the
    multiplexer every session runs in, and the program that asked has usually exited. Its request time
    is exact, though, so the session that raised it is the one whose tool call ran just before. Only
    transcripts changed since the window began are read, and each one backwards from its end until its
    records are older than the window (with CALLS_SLACK_S for records out of order)."""
    lo, hi = at - before, at + after
    want = [w.lower() for w in words if w]
    found = []
    paths = list(root.glob("*/*.jsonl")) + list(root.glob("*/*/subagents/*.jsonl"))
    for path in paths:
        try:
            if path.stat().st_mtime < lo:
                continue
        except OSError:
            continue
        session = path.parent.parent.name if path.parent.name == "subagents" else path.stem
        for rec in _records_back(path, chunk):
            t = _when(rec)
            if t is None:
                continue
            if t < lo - CALLS_SLACK_S:
                break
            if not lo <= t <= hi or rec.get("type") != "assistant":
                continue
            content = (rec.get("message") or {}).get("content")
            for item in content if isinstance(content, list) else []:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                text = json.dumps(item.get("input"), ensure_ascii=False).lower()
                if all(w in text for w in want):
                    found.append({"session": session, "folder": rec.get("cwd"), "time": t,
                                  "tool": item.get("name"), "input": item.get("input")})
    return sorted(found, key=lambda c: c["time"], reverse=True)


def resume_command(e: dict) -> str:
    cwd = (e.get("cwd") or "~").replace(str(Path.home()), "~")
    return f"cd {cwd} && claude --resume {e['id']}"


#: path -> (modification time, read_to, seam, url), as read_meta keeps them
_URL_CACHE: dict[str, tuple[float, int, str, str | None]] = {}


def rc_url(path: str) -> str | None:
    """A chat's own Remote Control link: the url of the last `bridge_status` record in its
    transcript, or None. Cached on the file's modification time, and a file that grew is read from
    where the last read stopped, as in read_meta. A caller waiting for a new link reads it every
    second or two while the chat writes."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _URL_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[3]
    url, read_to = None, 0
    try:
        with open(path, "rb") as fh:
            if hit and hit[1] <= os.fstat(fh.fileno()).st_size and _seam(fh, hit[1]) == hit[2]:
                read_to, url = hit[1], hit[3]
            fh.seek(read_to)
            for raw in fh:
                if b'"bridge_status"' in raw:
                    d = _record(raw)
                    if d is not None and d.get("subtype") == "bridge_status" and d.get("url"):
                        url = d["url"]
                if raw.endswith(b"\n"):
                    read_to += len(raw)
            seam = _seam(fh, read_to)
    except OSError:
        return None
    _URL_CACHE[path] = (mtime, read_to, seam, url)
    return url


def pick(idx: list[dict]) -> dict[str, dict]:
    """One entry per chat name (`title`): the conversation that carries it and was active most
    recently. Subagent transcripts never count."""
    out: dict[str, dict] = {}
    for e in idx:
        name = e.get("title")
        if not name or Path(e["path"]).name.startswith("agent-") or "/subagents/" in e["path"]:
            continue
        at = e.get("active_at") or e.get("mtime") or 0
        if name not in out or at > (out[name].get("active_at") or out[name].get("mtime") or 0):
            out[name] = e
    return out


def project_for(cwd: str | None, projects: list[dict]) -> str | None:
    """The project (`{"project": name, "path": folder}`) whose folder holds `cwd` most closely, by
    path; None when none does."""
    if not cwd:
        return None
    best = None
    for p in projects:
        path = p["path"].rstrip("/")
        if cwd == path or cwd.startswith(path + "/"):
            if best is None or len(path) > len(best["path"]):
                best = p
    return best["project"] if best else None
