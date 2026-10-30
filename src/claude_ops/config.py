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

import copy
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
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


#: what names the account Claude Code is signed in to; the rest of `oauthAccount` (the profile,
#: rate tiers, the time it was fetched) changes without a new login
#: (built from parts, because a pair of quoted names on one line reads as a login to secret scanners)
LOGIN_KEYS = tuple(f"{part}Uuid" for part in ("account", "organization"))


def login_digest(path: os.PathLike | str = CLAUDE_JSON) -> str | None:
    """A fingerprint of the account Claude Code is signed in to. A running chat keeps the login it
    started with, so a change after it started means it needs a restart. None when the file cannot
    be read or holds no login."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(account, dict) or not account.get("accountUuid"):
        return None
    return fingerprint(json.dumps([account.get(k) for k in LOGIN_KEYS]).encode())


def _canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def mcp_server_shapes(path: os.PathLike | str = CLAUDE_JSON, key: str = "mcpServers") -> dict[str, dict] | None:
    """What a change to each MCP server's definition can mean to a running session:
    {name: {"digest", "without_p", "module"}}. `digest` covers the whole definition, `without_p` the
    same definition with Python's -P flag left out, and `module` is the top package a
    `python -m <package.module>` server runs, or None. Only fingerprints and a package name are
    returned, never the arguments or the environment, which can hold tokens. None when the file
    cannot be read."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    servers = data.get(key, {}) if isinstance(data, dict) else {}
    out: dict[str, dict] = {}
    for name, spec in (servers if isinstance(servers, dict) else {}).items():
        if not isinstance(spec, dict):
            continue
        args = [str(a) for a in spec.get("args") or []]
        module = None
        if "-m" in args and args.index("-m") + 1 < len(args):
            module = args[args.index("-m") + 1].split(".")[0] or None
        bare = {**spec, "args": [a for a in args if a != "-P"]} if "args" in spec else spec
        out[name] = {"digest": fingerprint(_canonical(spec)), "without_p": fingerprint(_canonical(bare)),
                     "module": module}
    return out


def mcp_change_affects(then: dict | None, now: dict | None, folder: os.PathLike | str | None) -> str | None:
    """Why a change of one MCP server's definition, from `then` to `now` (two entries of
    mcp_server_shapes), changes something for a session working in `folder`, or None when it does
    not. A session keeps the servers it started with until it restarts.

    - A server that did not exist when the session started was never loaded by it: None.
    - A server removed since: the session still runs it, which is a change.
    - A change that only adds or removes -P matters only when the folder holds the server's top
      package, because -P only stops Python putting the working folder first on its import path, so
      a folder without that package imports the same code either way."""
    if not then:
        return None
    if not now:
        return "a server it runs was removed"
    if then.get("digest") == now.get("digest"):
        return None
    module = now.get("module") or then.get("module")
    if then.get("without_p") == now.get("without_p"):
        if module and folder and Path(folder, module).is_dir():
            return f"-P changed, and its folder holds {module}, which the server imports"
        return None
    return "a server it runs is defined differently"


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


def descendants(pid: int, procs: dict[int, dict]) -> set[int]:
    """Every process started under `pid`, at any depth (`procs` is `processes()`). Used to find what
    a chat made: a window belongs to a chat when the process that owns it descends from the chat's
    claude process (a Playwright browser, an app a command started)."""
    kids: dict[int, list[int]] = {}
    for p, v in procs.items():
        kids.setdefault(v.get("ppid"), []).append(p)
    out: set[int] = set()
    todo = list(kids.get(pid, []))
    while todo:
        p = todo.pop()
        if p in out:
            continue
        out.add(p)
        todo.extend(kids.get(p, []))
    return out


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
        "login_digest": login_digest(CLAUDE_JSON),
        "plugins_digest": digest_json(PLUGINS),
        "instructions_digest": digest_files(instruction_files(GLOBAL_MD)),
    }


# ---------------------------------------------------------------- writing the configuration

class ConfigChanged(RuntimeError):
    """The file kept changing under each try, and nothing was written."""


def update_json(path: os.PathLike | str, change: Callable[[dict], "dict | None"], *,
                backup: os.PathLike | str | None = None, tries: int = 3) -> dict:
    """Rewrite a JSON object file that other programs also rewrite, without losing their writes.
    `change` gets a copy of the parsed object and returns the object to write, or None to leave the
    file as it is. Returns {"changed", "backup", "mtime_ns"}.

    ⚠️ RUNNING CLAUDE CODE PROCESSES REWRITE `~/.claude.json` EVERY MINUTE OR TWO. So the file is read
    whole, `change` builds the new object from what was read, the result is written to a temporary
    file beside it, and only while the file still holds the bytes that were read is the temporary
    file renamed over it (one atomic step, keeping the original's mode). A file that changed in
    between is read again and `change` called again, `tries` times, then ConfigChanged is raised with
    nothing written. An object `change` leaves equal is not written at all.

    `backup`, when given, receives the exact bytes the write replaced, with the file's mode. A
    missing file, or one that is not a JSON object, raises ValueError."""
    path = Path(path)
    backed_up = None
    for _ in range(max(1, tries)):
        try:
            raw = path.read_bytes()
        except OSError as e:
            raise ValueError(f"{path} cannot be read: {e.strerror or type(e).__name__}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError(f"{path} is not JSON") from None
        if not isinstance(data, dict):
            raise ValueError(f"{path} does not hold a JSON object")
        new = change(copy.deepcopy(data))
        if new is None or new == data:
            return {"changed": False, "backup": backed_up, "mtime_ns": path.stat().st_mtime_ns}
        text = json.dumps(new, indent=2) + ("\n" if raw.endswith(b"\n") else "")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, path.stat().st_mode & 0o7777)
            if path.read_bytes() != raw:
                os.unlink(tmp)
                continue            # another program wrote meanwhile: start from what it wrote
            if backup is not None:  # the bytes this write replaces, exactly
                Path(backup).write_bytes(raw)
                os.chmod(backup, path.stat().st_mode & 0o7777)
                backed_up = str(backup)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return {"changed": True, "backup": backed_up, "mtime_ns": path.stat().st_mtime_ns}
    raise ConfigChanged(f"{path} changed under each of {tries} tries; nothing was written")
