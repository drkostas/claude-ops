"""Refuse a tool call that would change a production checkout by hand (a PreToolUse hook).

Some folders are checkouts that only a deploy step may move: a service runs its code from them,
and the deploy moves them to a new commit only after that commit's tests passed, then restarts
what changed. A `git checkout` or an edit there by hand skips all of that, and the service imports
whatever is on disk at its next start. This hook refuses such a call (exit 2, with the reason sent
back to the model):

- a Bash command that runs a git subcommand that changes the working tree, the index, HEAD or the
  stash (checkout, switch, reset, pull, merge, rebase, restore, clean, stash, commit, add, ...) in a
  protected folder, whether it gets there by `cd`, `pushd`, `git -C` or `--work-tree`, with the
  folder named directly or through a variable the same line assigned (`L=~/x; git -C "$L" ...`);
- `git worktree remove` or `git worktree move` of a protected folder;
- text a Bash command writes into a file there (a here-document, `echo` or `printf` redirected, or
  piped into `tee`);
- Write, Edit, MultiEdit and NotebookEdit of a file inside one.

Reading stays allowed: log, show, status, diff, rev-parse, fetch, reflog, `stash list`, and
`worktree add` of a new folder (how a session makes its own worktree from a protected checkout).
The command line is read by `commandline`, shared with the other hooks.

The protected folders are regular expressions matched against absolute paths, from `--pattern`
(repeatable), else from a JSON file:

    {
      "patterns": ["/worktrees/[^/]+-(live|next)(/|$)"],
      "message": "Build in your own worktree: git worktree add ~/worktrees/<topic> origin/main."
    }

The file is `--config`, else `CLAUDE_OPS_PROTECTED_CHECKOUTS_CONFIG`, else
`~/.config/claude-ops/protected-checkouts.json`. With no patterns nothing is refused. On anything
unexpected the call goes through: a broken hook must never stop work.

    python3 -P -m claude_ops.hooks.protected_checkouts [--pattern REGEX ...] [--config FILE]
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import commandline

#: git subcommands that change the working tree, the index, HEAD, a ref or the stash
CHANGES = {"checkout", "switch", "reset", "pull", "merge", "rebase", "restore", "clean", "stash",
           "cherry-pick", "am", "commit", "apply", "revert", "rm", "mv", "add", "read-tree",
           "checkout-index", "update-index", "update-ref", "symbolic-ref"}
#: `git stash` forms that only read
STASH_READS = {"list", "show"}
EDITS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
DEFAULT_CONFIG = "~/.config/claude-ops/protected-checkouts.json"


def config(argv: list[str]) -> tuple[list[re.Pattern], str]:
    """(the protected folder patterns, the extra message) from the command line or the file."""
    patterns, path, i = [], None, 0
    while i < len(argv):
        if argv[i] == "--pattern" and i + 1 < len(argv):
            patterns.append(argv[i + 1])
            i += 2
        elif argv[i] == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
            i += 2
        else:
            i += 1
    message = ""
    if not patterns:
        path = path or os.environ.get("CLAUDE_OPS_PROTECTED_CHECKOUTS_CONFIG") or DEFAULT_CONFIG
        try:
            with open(os.path.expanduser(path)) as f:
                data = json.load(f)
            patterns, message = list(data.get("patterns") or []), str(data.get("message") or "")
        except (OSError, ValueError):
            return [], ""
    return [re.compile(p) for p in patterns], message


def protected(path: str, patterns: list[re.Pattern]) -> bool:
    full = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    return any(p.search(full + "/") for p in patterns)


def check_bash(command: str, cwd: str, patterns: list[re.Pattern]) -> str | None:
    """Why a Bash command is refused, or None."""
    for inv in commandline.git_invocations(command, cwd):
        where = inv["dir"]
        g = inv["globals"]
        for k, v in zip(g, g[1:]):
            if k == "--work-tree":
                where = os.path.normpath(os.path.join(inv["dir"], v))
        for k in g:
            if k.startswith("--work-tree="):
                where = os.path.normpath(os.path.join(inv["dir"], commandline.expand(k.split("=", 1)[1])))
        sub, args = inv["sub"], inv["args"]
        if sub == "worktree" and args[:1] and args[0] in ("remove", "move"):
            for a in args[1:]:
                if not a.startswith("-"):
                    target = os.path.normpath(os.path.join(inv["dir"], commandline.expand(a, inv.get("names"))))
                    if protected(target, patterns):
                        return f"`git worktree {args[0]}` of {target}"
            continue
        if sub not in CHANGES or not protected(where, patterns):
            continue
        if sub == "stash" and args[:1] and args[0] in STASH_READS:
            continue
        return f"`git {sub}` in {where}"
    for path, _text in commandline.writes(command, cwd):
        if protected(path, patterns):
            return f"a write into {path}"
    return None


def check(payload: dict, patterns: list[re.Pattern]) -> str | None:
    if not patterns:
        return None
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool == "Bash":
        return check_bash(str(ti.get("command", "")), cwd, patterns)
    if tool in EDITS:
        path = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if path and protected(os.path.join(cwd, os.path.expanduser(path)), patterns):
            return f"a {tool} of {path}"
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        patterns, message = config(list(sys.argv[1:] if argv is None else argv))
        payload = json.load(sys.stdin)
        why = check(payload, patterns)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}. This folder is a production checkout that only its deploy step moves, "
              "after the new commit's tests passed, and the deploy then restarts what changed. Read it "
              "with `git -C <it> log/show/status` or `git show origin/main:<path>`, and build in a "
              "worktree of your own." + (f" {message}" if message else ""), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
