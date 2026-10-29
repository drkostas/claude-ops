"""sqlite_health — whether an SQLite database inside a container is intact.

A web app that keeps its data in SQLite in a Docker volume can go on answering its health URL
while the database underneath is damaged. When the virtual machine or the host under the
container goes down in the middle of a write, a page of the file can be left half written, and
SQLite then fails only the queries that touch it ("database disk image is malformed"). The app
logs a warning for each failed write, serves everything else, and its health check stays green
for as long as nobody reads that page.

Two readings catch it:

- `quick_check_in_container(container, path)` runs `PRAGMA quick_check` on the file, inside the
  container, read-only. It uses the image's `sqlite3` when it has one and its `python3` otherwise.
  It answers `{"intact": True}` for "ok", `{"intact": False, "detail": <SQLite's first lines>}`
  for damage, and `{"intact": None, "detail": <why>}` when nothing could be asked (the container
  is not running, the engine does not answer, the image has neither program). None is no
  observation, never a pass.
- `log_error_lines(container, since="10m")` counts the lines in the container's log that carry
  SQLite's damage messages (`ERROR_PATTERN`). It finds damage a quick check misses (a file that
  is fine now but failed writes a minute ago) and costs nothing to read. None when the log could
  not be read.

    claude-ops sqlite-check <container> <path>              # intact, damaged, or not asked
    claude-ops sqlite-check <container> <path> --since 10m  # and the damage lines in the log
    claude-ops sqlite-check <container> <path> --json

`engine` is the container command (`docker` by default, `podman` works the same).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable

Runner = Callable[..., subprocess.CompletedProcess]

#: the messages SQLite (and the drivers over it) print for a damaged or unreadable database file
ERROR_PATTERN = re.compile(r"database disk image is malformed|disk I/O error|file is not a database",
                           re.IGNORECASE)
#: how long one check may take, in seconds (a quick check of a few hundred MB takes seconds)
TIMEOUT_S = 60

#: run inside the container when it has python3 and no sqlite3; the path is its first argument
_PY_CHECK = ("import sqlite3,sys\n"
             "c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)\n"
             "print('\\n'.join(r[0] for r in c.execute('PRAGMA quick_check(20)')))\n")

#: what the engine prints when the container cannot be asked at all
_NOT_ASKED = re.compile(r"no such container|is not running|cannot connect to the docker daemon|"
                        r"error during connect|is paused", re.IGNORECASE)


def _run(runner: Runner, cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return runner(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def read_quick_check(text: str) -> dict:
    """SQLite's quick_check output -> {"intact": bool, "detail": str}. "ok" alone is intact."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines == ["ok"]:
        return {"intact": True, "detail": "ok"}
    if not lines:
        return {"intact": None, "detail": "the check printed nothing"}
    return {"intact": False, "detail": "; ".join(lines[:5])[:400]}


def quick_check_in_container(container: str, path: str, *, engine: str = "docker",
                             runner: Runner = subprocess.run, timeout: float = TIMEOUT_S) -> dict:
    """Run PRAGMA quick_check on `path` inside `container`, read-only. See the module docstring."""
    tries = (["sqlite3", "-readonly", path, "PRAGMA quick_check(20);"],
             ["python3", "-c", _PY_CHECK, path])
    why = "the image has neither sqlite3 nor python3"
    for inner in tries:
        r = _run(runner, [engine, "exec", container, *inner], timeout)
        if r is None:
            return {"intact": None, "detail": f"{engine} could not be run or did not finish"}
        err = (r.stderr or "").strip()
        if _NOT_ASKED.search(err):
            return {"intact": None, "detail": err.splitlines()[0][:300]}
        if r.returncode == 0:
            return read_quick_check(r.stdout or "")
        if r.returncode in (126, 127) or "executable file not found" in err or "not found" in err.lower():
            continue  # this program is not in the image, try the next
        # the program ran and failed: SQLite refused the file (a damaged header, not a database)
        text = (r.stdout or "") + "\n" + err
        if ERROR_PATTERN.search(text) or "malformed" in text.lower():
            return {"intact": False, "detail": " ".join(text.split())[:400]}
        why = " ".join(err.split())[:300] or f"exit {r.returncode}"
        return {"intact": None, "detail": why}
    return {"intact": None, "detail": why}


def count_error_lines(text: str, pattern: re.Pattern = ERROR_PATTERN) -> int:
    """How many lines of a log carry one of SQLite's damage messages."""
    return sum(1 for ln in text.splitlines() if pattern.search(ln))


def log_error_lines(container: str, since: str = "10m", *, engine: str = "docker",
                    runner: Runner = subprocess.run, timeout: float = TIMEOUT_S,
                    pattern: re.Pattern = ERROR_PATTERN) -> int | None:
    """The damage lines in the container's log over `since` (both of its streams), or None when the
    log could not be read."""
    r = _run(runner, [engine, "logs", "--since", since, container], timeout)
    if r is None or r.returncode != 0:
        return None
    return count_error_lines((r.stdout or "") + "\n" + (r.stderr or ""), pattern)


def main(argv: list[str] | None = None, runner: Runner = subprocess.run) -> int:
    p = argparse.ArgumentParser(prog="claude-ops sqlite-check",
                                description="whether an SQLite database inside a container is intact")
    p.add_argument("container")
    p.add_argument("path", help="the database file's path inside the container")
    p.add_argument("--since", metavar="DURATION", help="also count the damage lines in the log over this time")
    p.add_argument("--engine", default="docker")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    got = quick_check_in_container(a.container, a.path, engine=a.engine, runner=runner)
    if a.since:
        got["error_lines"] = log_error_lines(a.container, a.since, engine=a.engine, runner=runner)
    if a.json:
        print(json.dumps(got))
    else:
        state = {True: "intact", False: "DAMAGED", None: "not asked"}[got["intact"]]
        print(f"{a.container}:{a.path} {state} ({got['detail']})")
        if a.since:
            n = got["error_lines"]
            print(f"damage lines in the log over {a.since}: {'unreadable' if n is None else n}")
    damaged = got["intact"] is False or bool(got.get("error_lines"))
    return 1 if damaged else (2 if got["intact"] is None else 0)


if __name__ == "__main__":
    raise SystemExit(main())
