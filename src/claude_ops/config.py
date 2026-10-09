"""What Claude Code is installed with, and what a running chat is running.

A running chat keeps the binary, the settings, the MCP servers and the instructions it started
with. When Claude Code updates itself, or a CLAUDE.md changes, the open chats do not notice. These
functions read both sides: the installed version and a fingerprint of each thing a chat loads at
start, and for a process, the version it runs and when it started.

- A fingerprint, never a file time. `~/.claude.json` is rewritten by Claude Code itself many times
  an hour, so its modification time says nothing. Each source is a hash of the part that matters.
- What cannot be read is None, never a guess. A process whose binary lsof cannot see has no
  version, and a caller should leave it alone rather than call it out of date.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

HOME = Path.home()
CLAUDE_BIN = HOME / ".local/bin/claude"
SETTINGS = HOME / ".claude/settings.json"
CLAUDE_JSON = HOME / ".claude.json"
PLUGINS = HOME / ".claude/plugins/installed_plugins.json"
GLOBAL_MD = HOME / ".claude/CLAUDE.md"
VERSION = re.compile(r"/claude/versions/([^/\s]+)$")
IMPORT = re.compile(r"^@(\S+)\s*$", re.M)


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def digest_json(path: os.PathLike | str, key: str | None = None) -> str | None:
    """A fingerprint of a JSON file (or of one key in it) that ignores key order and spacing.
    None when the file is missing or unreadable; the empty object when the key is absent."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if key is not None:
        data = data.get(key, {}) if isinstance(data, dict) else {}
    return fingerprint(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())


def digest_files(paths: list[Path]) -> str | None:
    """One fingerprint over the files that exist, names included; None when none exists."""
    h = hashlib.sha256()
    seen = False
    for p in paths:
        try:
            data = Path(p).read_bytes()
        except OSError:
            continue
        seen = True
        h.update(str(p).encode() + b"\0" + data + b"\0")
    return h.hexdigest()[:16] if seen else None


def instruction_files(path: os.PathLike | str = GLOBAL_MD) -> list[Path]:
    """A CLAUDE.md and the files it includes with a line `@file`, one level deep, relative to the
    CLAUDE.md's own folder (`@tools.md` in ~/.claude/CLAUDE.md is ~/.claude/tools.md)."""
    path = Path(path)
    try:
        text = path.read_text()
    except OSError:
        return [path]
    out = [path]
    for name in IMPORT.findall(text):
        p = Path(os.path.expanduser(name))
        out.append(p if p.is_absolute() else path.parent / p)
    return out


def project_claude_mds(cwd: os.PathLike | str, global_md: os.PathLike | str = GLOBAL_MD) -> list[Path]:
    """Every CLAUDE.md a chat in `cwd` loads: in its folder and in each folder above it (the global
    one is not counted here)."""
    out = []
    p = Path(cwd)
    for d in (p, *p.parents):
        f = d / "CLAUDE.md"
        if f != Path(global_md) and f.is_file():
            out.append(f)
    return out


def version_of_binary(path: str | None) -> str | None:
    """`2.1.294` from `~/.local/share/claude/versions/2.1.294`, or None."""
    m = VERSION.search(path or "")
    return m.group(1) if m else None


def parse_ps(text: str) -> dict[int, dict]:
    """`ps -A -o pid=,ppid=,lstart=,comm=` into {pid: {ppid, started, comm}}. lstart is five words
    (`Thu Oct  9 12:00:00 2026`), in local time."""
    out = {}
    for line in text.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            started = dt.datetime.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y").astimezone()
        except ValueError:
            continue
        out[pid] = {"ppid": ppid, "started": started, "comm": parts[7].strip()}
    return out


def _run(argv: list[str], env: Mapping[str, str] | None) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def processes(*, env: Mapping[str, str] | None = None) -> dict[int, dict]:
    """Every process, from ps: {pid: {ppid, started, comm}}."""
    return parse_ps(_run(["/bin/ps", "-A", "-o", "pid=,ppid=,lstart=,comm="], env) or "")


def is_claude(proc: dict | None) -> bool:
    return bool(proc) and Path(proc["comm"]).name == "claude"


def running_version(pid: int, *, env: Mapping[str, str] | None = None) -> str | None:
    """The version a claude process runs: the binary it was started from (lsof's txt entry)."""
    out = _run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "txt", "-Fn"], env) or ""
    for line in out.splitlines():
        v = version_of_binary(line[1:] if line.startswith("n") else line)
        if v:
            return v
    return None


def installed_version(binary: os.PathLike | str = CLAUDE_BIN) -> str | None:
    """The version `claude` starts now: where the launcher symlink points."""
    try:
        return version_of_binary(os.path.realpath(binary))
    except OSError:
        return None


def snapshot() -> dict[str, str | None]:
    """The installed version and a fingerprint of each thing a new chat loads at start."""
    return {
        "claude_version": installed_version(),
        "settings_digest": digest_json(SETTINGS),
        "mcp_servers_digest": digest_json(CLAUDE_JSON, "mcpServers"),
        "plugins_digest": digest_json(PLUGINS),
        "instructions_digest": digest_files(instruction_files(GLOBAL_MD)),
    }
