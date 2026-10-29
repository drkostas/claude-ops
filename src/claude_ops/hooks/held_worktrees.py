"""Refuse a git command that rewrites a worktree a running tool holds (a PreToolUse hook).

Some tools work on a branch in the background for a long time: a ship queue that waits its turn,
rebases the branch onto the newest main branch, tests it, merges it, and puts the branch back as it
recorded it when a batch fails. A session that rebases, resets, amends, pushes or switches branches
in that worktree meanwhile has its change undone by the tool's put-back, or mixed into the tool's
work. This hook refuses such a command (exit 2, with the reason sent back to the model) while the
process that holds the worktree is alive:

- `git rebase`, `reset`, `commit --amend`, `push`, `pull`, `merge`, `cherry-pick`, `revert`, `am`,
  `checkout` or `switch` of another branch, `stash` (not `stash list`/`show`), in a held worktree,
  reached by `cd`, `git -C` or `--work-tree`, as protected_checkouts reads them.

A plain `git commit` stays allowed: it only adds a commit on top, which a tool that restores a
branch "as its worktree has it" keeps. Reading (log, show, status, diff, fetch) stays allowed too.

The holders are a folder of files, each named after a process id, each listing the worktree folders
that process holds, one per line. A file whose process is gone holds nothing, so a tool that died
leaves no lock behind. The tool writes its file when it starts holding and removes it when it ends.

A process id is used again once its process ends, so a holder that was killed can leave a file that a
new, unrelated process seems to keep alive. A tool may write `<pid>.started` beside the file, holding
the process's start time as `ps -o lstart= -p <pid>` prints it. Then the file holds only while that pid
still runs with that start time. A file with no `.started` beside it is judged by its pid alone, as
before. The start time is read only for a command that would rewrite a held worktree, so the hook
costs nothing more for every other call.
The folder and an extra message come from `--holders` and `--message`, else from a JSON file:

    {
      "holders": "/tmp/ship.lock.worktrees",
      "message": "Stop the ship (kill -TERM <pid>), make the change, push, and ship again."
    }

The file is `--config`, else `CLAUDE_OPS_HELD_WORKTREES_CONFIG`, else
`~/.config/claude-ops/held-worktrees.json`. With no holders folder nothing is refused. On anything
unexpected the call goes through: a broken hook must never stop work.

    python3 -P -m claude_ops.hooks.held_worktrees [--holders DIR] [--message TEXT] [--config FILE]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from . import commandline

DEFAULT_CONFIG = "~/.config/claude-ops/held-worktrees.json"
#: git subcommands that rewrite or move a branch, or send it
REWRITES = {"rebase", "reset", "push", "pull", "merge", "cherry-pick", "revert", "am", "stash",
            "checkout", "switch"}
STASH_READS = {"list", "show"}


def config(argv: list[str]) -> tuple[str | None, str]:
    """(the holders folder, the extra message) from the command line or the file."""
    holders, message, path, i = None, "", None, 0
    while i < len(argv):
        if argv[i] in ("--holders", "--message", "--config") and i + 1 < len(argv):
            if argv[i] == "--holders":
                holders = argv[i + 1]
            elif argv[i] == "--message":
                message = argv[i + 1]
            else:
                path = argv[i + 1]
            i += 2
        else:
            i += 1
    if not holders:
        path = path or os.environ.get("CLAUDE_OPS_HELD_WORKTREES_CONFIG") or DEFAULT_CONFIG
        try:
            with open(os.path.expanduser(path)) as f:
                data = json.load(f)
            holders, message = data.get("holders"), message or str(data.get("message") or "")
        except (OSError, ValueError):
            return None, ""
    return (os.path.expanduser(holders) if holders else None), message


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _started(pid: int) -> str | None:
    """The start time of process `pid` as `ps -o lstart=` prints it, or None when it cannot be read."""
    try:
        r = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout or "").strip() or None if r.returncode == 0 else None


def same_process(holders: str | None, pid: int, started=_started) -> bool:
    """Whether `pid` is still the process that wrote its holder file: True when no `<pid>.started` is
    beside the file (a holder written before start times were kept), or when it matches what `ps`
    prints now. A start time that cannot be read now counts as the same process, so the hook never
    lets a rewrite through on a reading it could not make."""
    if not holders:
        return True
    try:
        with open(os.path.join(holders, f"{pid}.started")) as f:
            want = f.read().strip()
    except OSError:
        return True
    if not want:
        return True
    now = started(pid)
    return now is None or " ".join(now.split()) == " ".join(want.split())


def holding(holders: str | None) -> dict[str, list[int]]:
    """{worktree folder (resolved): the pids of every live process whose file lists it}, in pid order."""
    out: dict[str, list[int]] = {}
    if not holders or not os.path.isdir(holders):
        return out
    for name in sorted(os.listdir(holders), key=lambda n: (len(n), n)):
        if not name.isdigit() or not _alive(int(name)):
            continue
        try:
            with open(os.path.join(holders, name)) as f:
                for line in f:
                    if line.strip():
                        out.setdefault(os.path.realpath(os.path.expanduser(line.strip())), []).append(int(name))
        except OSError:
            continue
    return out


def held(holders: str | None) -> dict[str, int]:
    """{worktree folder (resolved): the pid of a live process that holds it}."""
    return {folder: pids[0] for folder, pids in holding(holders).items()}


def _holder(where: str, folders: dict[str, int]) -> tuple[str, int] | None:
    full = os.path.realpath(where)
    for folder, pid in folders.items():
        if full == folder or full.startswith(folder.rstrip("/") + "/"):
            return folder, pid
    return None


def _rewrites(sub: str, args: list[str]) -> bool:
    if sub == "commit":
        return "--amend" in args
    if sub == "stash":
        return not (args[:1] and args[0] in STASH_READS)
    if sub in ("checkout", "switch"):
        # a file restored from the index (`checkout -- f`, `checkout f`) moves no branch; a branch,
        # a commit or -b/-B does. Only `--` followed by paths is let through.
        return not (args[:1] == ["--"])
    return sub in REWRITES


def check(payload: dict, folders: dict[str, int], holders: str | None = None,
          started=_started) -> str | None:
    """Why the call is refused, or None. With `holders`, a hit's holder must still be the process that
    wrote its file (same_process), else its folders hold nothing."""
    if not folders or payload.get("tool_name") != "Bash":
        return None
    cwd = payload.get("cwd") or os.getcwd()
    for inv in commandline.git_invocations(str((payload.get("tool_input") or {}).get("command", "")), cwd):
        where = inv["dir"]
        g = inv["globals"]
        for k, v in zip(g, g[1:]):
            if k == "--work-tree":
                where = os.path.normpath(os.path.join(inv["dir"], v))
        if not _rewrites(inv["sub"], inv["args"]):
            continue
        hit = _holder(where, folders)
        if hit and holders:
            # every file that lists the folder counts, and a pid another process took holds nothing
            live = [p for p in holding(holders).get(hit[0], [hit[1]]) if same_process(holders, p, started)]
            hit = (hit[0], live[0]) if live else None
        if hit:
            return (f"`git {inv['sub']}{' --amend' if inv['sub'] == 'commit' else ''}` in {hit[0]}, "
                    f"which process {hit[1]} holds")
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        holders, message = config(list(sys.argv[1:] if argv is None else argv))
        payload = json.load(sys.stdin)
        why = check(payload, held(holders), holders)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}. A tool works on this branch in the background and puts it back as it "
              "recorded it, so this change would be undone or mixed into its work. A plain commit on "
              "top is kept." + (f" {message}" if message else ""), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
