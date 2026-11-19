"""What a running process really runs, and whether that runtime still gets fixes.

Two facts about a service are easy to get wrong when they are read from what is installed.

- The runtime a process runs is the program it was started from, not the newest one on the disk.
  A service started last month under one version of a language runtime keeps that version until the
  process is replaced, whatever was installed since.
- A library upgraded on disk is not a library in use. A process maps the files it loaded when it
  started. After an upgrade the old files are gone from the disk, and every process that started
  before it still maps them. A reading of the installed package's version says the fix is in place
  while those processes run without it.

So this reads the process. `lsof -p <pid> -a -d txt` lists the program and every library the process
has mapped, and a mapped path that no longer exists on the disk (or that the system marks as deleted)
is a mapping of a version that has been replaced. The program's own `--version` names the runtime,
and a table of support dates, which the caller supplies, says whether that release line still gets
fixes.

    from claude_ops import runtime_check
    reading = runtime_check.read(pid)
    print(reading.name, reading.version, reading.stale)
    table = {"node": {"22": "2027-04-30", "24": "2028-04-30"}}
    print(runtime_check.support(table, reading.name, reading.version, date.today()).state)

Nothing is signalled, restarted or changed. The program is only asked for its version, which every
runtime answers without starting anything.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

SUPPORTED, ENDED, UNKNOWN = "supported", "ended", "unknown"

#: how a runtime's release line is named from its version, by the program's name. Most runtimes
#: are supported by major version (node, bun), some by major and minor (python, php, ruby)
MAJOR_AND_MINOR = ("python", "php", "ruby", "perl")

_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class ProcessRuntime:
    pid: int
    executable: str | None            # the program the process was started from, or None when not readable
    name: str | None                  # its runtime name (node, python, php, bun, ...), from its file name
    version: str | None               # what the program says with --version, or None
    stale: tuple[str, ...] = ()       # mapped files that are no longer on the disk
    mapped: int = 0                   # how many files the process maps
    detail: str = ""


@dataclass(frozen=True)
class Support:
    state: str                        # supported, ended or unknown
    line: str | None                  # the release line the version belongs to (22, 3.14, ...)
    ends: date | None                 # the date that line stops getting fixes, when the table has it


def mapped_files(pid: int, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> list[str] | None:
    """The program and the libraries a process maps, in the order lsof lists them (the program
    first), or None when lsof could not read the process (it ended, or is another user's)."""
    try:
        r = run(["lsof", "-p", str(pid), "-a", "-d", "txt", "-Fn"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 and not (r.stdout or "").strip():
        return None
    return [ln[1:] for ln in (r.stdout or "").splitlines() if ln.startswith("n") and len(ln) > 1]


def stale_mappings(paths: Iterable[str], exists: Callable[[str], bool] = os.path.exists) -> list[str]:
    """The mapped paths whose file is gone. lsof on Linux marks a replaced file with a trailing
    ` (deleted)`. On macOS the path simply stops existing when an upgrade removes its folder."""
    out = []
    for p in paths:
        if p.endswith(" (deleted)") or (p.startswith("/") and not exists(p)):
            out.append(p)
    return out


def runtime_name(executable: str | None) -> str | None:
    """`node` from /usr/local/bin/node, `python` from python3.14, `php` from php-fpm8.3, else None."""
    if not executable:
        return None
    base = os.path.basename(executable).lower()
    for name in ("node", "python", "php", "bun", "deno", "ruby", "perl", "java"):
        if base.startswith(name):
            return name
    return None


def parse_version(text: str) -> str | None:
    """The first `major.minor[.patch]` in a program's version text (`v25.8.1`, `Python 3.14.0`,
    `PHP 8.3.1 (cli)`), or a bare `major` for a runtime that prints only that."""
    m = _VERSION.search(text or "")
    if m:
        return ".".join(g for g in m.groups() if g is not None)
    m = re.search(r"\b(\d+)\b", text or "")
    return m.group(1) if m else None


def version_of(executable: str, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> str | None:
    """What the program says it is. Only `--version` is asked, which starts no service."""
    try:
        r = run([executable, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version((r.stdout or "") + " " + (r.stderr or "")) if r.returncode == 0 else None


def read(pid: int, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
         exists: Callable[[str], bool] = os.path.exists) -> ProcessRuntime | None:
    """The runtime one process runs, or None when the process cannot be read."""
    paths = mapped_files(pid, run)
    if not paths:
        return None
    exe = paths[0]
    return ProcessRuntime(pid=pid, executable=exe, name=runtime_name(exe), version=version_of(exe, run),
                          stale=tuple(stale_mappings(paths[1:], exists)), mapped=len(paths) - 1)


def release_line(name: str, version: str) -> str | None:
    """The release line a version belongs to, the unit a support date is kept for."""
    parts = version.split(".")
    if name in MAJOR_AND_MINOR:
        return ".".join(parts[:2]) if len(parts) >= 2 else None
    return parts[0] if parts and parts[0] else None


def support(table: dict[str, dict[str, str]], name: str | None, version: str | None, today: date) -> Support:
    """Whether a release line still gets fixes, from a table of `{runtime: {line: "YYYY-MM-DD"}}`
    (the date its fixes end). A runtime or a line the table does not hold is `unknown`, never
    `supported`: a reading that cannot say is not a reading that says it is fine."""
    if not name or not version:
        return Support(UNKNOWN, None, None)
    line = release_line(name, version)
    raw = (table.get(name) or {}).get(line or "")
    if not raw:
        return Support(UNKNOWN, line, None)
    try:
        ends = date.fromisoformat(raw)
    except ValueError:
        return Support(UNKNOWN, line, None)
    return Support(ENDED if today > ends else SUPPORTED, line, ends)
