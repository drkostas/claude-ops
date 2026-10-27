"""Keep merge conflict markers out of commits (a PreToolUse hook).

A rebase, merge or cherry-pick that stops on a conflict writes marker lines into the file: a line
starting `<<<<<<< `, one of exactly seven `=` signs, one starting `>>>>>>> `, and with the diff3
style one starting `||||||| `. git refuses to continue while the file is unmerged, but a `git add` of
the file as it is marks it merged, and the next `git rebase --continue` or `git commit` records the
markers as content. That happens to a session that settles some conflicted files and stages all of
them, or that stages what a settling tool refused, and a file without its own check reaches the
branch with the markers in it.

This hook reads the tool call Claude Code is about to make and refuses it (exit 2, with the reason
sent back to the model) when a Bash command would record such a line:

- `git add`: the lines that add would stage, read from the working tree (the changes to the tracked
  files it names, and the untracked files it names);
- `git commit`: what the commit would record, read as the no-session-links hook reads it (the staged
  changes, the tracked changes too for `-a`, and what a `git add` on the same line would stage);
- `git rebase --continue`, `git merge --continue`, `git cherry-pick --continue`, `git revert
  --continue` and `git am --continue`: what is staged, since that is what the continue commits.

It follows `git -C <dir>`, the options before the subcommand and a `cd` earlier in the same command,
and when a line cannot be read as words but says `git commit`, it reads what is staged and what `-a`
would take in the current folder anyway. A refusal names the file and line. A marker inside a line
(`a <<<<<<< b`) is text, not a marker, and a line of eight or more `=` signs is a rule, not a marker.
On anything unexpected (a folder git cannot read, a slow git) the call goes through: a broken hook
must never stop work.

    claude-ops hook conflict-markers      # reads the tool call on stdin
    python3 -P -m claude_ops.hooks.conflict_markers
"""
from __future__ import annotations

import json
import os
import re
import sys

from .commandline import (
    Git, Row, add_plan, diff_rows, git_invocations, hidden_commit, recorded, untracked_rows,
)

#: a conflict marker line, as git writes it: the three of every conflict, and diff3's base marker
MARKER = re.compile(r"^(<<<<<<< |>>>>>>> |\|\|\|\|\|\|\| |=======$)")
#: the subcommands whose `--continue` commits what is staged
CONTINUES = {"rebase", "merge", "cherry-pick", "revert", "am"}


def marker_rows(rows: list[Row]) -> list[Row]:
    """The rows whose text is a conflict marker line."""
    return [r for r in rows if MARKER.match(r[2])]


def staged_by_add(inv: dict) -> list[Row]:
    """The lines a `git add` would stage, read from the working tree: the changes to the tracked
    files it names, and the untracked files it names."""
    plan = add_plan(inv)
    if plan is None:
        return []
    g = Git(inv["dir"], inv["globals"])
    specs = [":/"] if plan["sweeping"] else plan["paths"]
    if not specs:
        return []
    return diff_rows(g, g.base(), "--", *specs) + untracked_rows(g, specs, plan["force"])


def would_record(command: str, cwd: str) -> list[Row]:
    """The conflict marker lines the git commands on this line would record, each once, sorted by
    file and line."""
    invs = git_invocations(command, cwd)
    rows: set[Row] = set()
    for inv in invs:
        if inv["sub"] == "add":
            rows.update(marker_rows(staged_by_add(inv)))
        elif inv["sub"] in CONTINUES and "--continue" in inv["args"]:
            rows.update(marker_rows(diff_rows(Git(inv["dir"], inv["globals"]), "--cached")))
    if any(inv["sub"] == "commit" for inv in invs):
        rows.update(marker_rows(recorded(command, cwd)[0]))
    elif hidden_commit(command):
        # the words do not show the commit (a quote in a comment, a wrapper program, a shell given a
        # string), so what is staged and what -a would take are read anyway
        rows.update(marker_rows(recorded("git commit -a", cwd)[0]))
    return sorted(rows)


def check(payload: dict) -> str | None:
    """Why this tool call is refused, or None."""
    if payload.get("tool_name") != "Bash":
        return None
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    hits = would_record(str(ti.get("command", "")), cwd)
    if not hits:
        return None
    path, line, text = hits[0]
    more = f" (and {len(hits) - 1} more)" if len(hits) > 1 else ""
    return f"this would record the conflict marker {text.split()[0]} in {path}:{line}{more}"


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
        why = check(payload)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}. A rebase, merge or cherry-pick that stopped on a conflict left the "
              "markers in the file. Settle the file (keep one side, or both), stage it, and run the "
              "command again.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
