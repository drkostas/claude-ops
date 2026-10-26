"""Refuse a session's own merge of a pull request in a repository that merges only through its ship
tool (a PreToolUse hook).

Some repositories merge only through a ship script: it takes a lock, runs the whole test suite on
the branch rebased onto the newest main branch, merges, and then lands what the merge needs (a
database change, a deploy). A merge typed by hand skips the suite and the lock, so the main branch
can go red under every ship waiting behind the lock, and each of them then holds on that red. This
hook refuses such a call (exit 2, with the reason sent back to the model):

- `claude-ops merge <n> --repo <repo>` (or run in a clone of it, with no --repo);
- `gh pr merge` with `-R`/`--repo <repo>` (or run in a clone of it);
- `gh api` with `-X PUT` (or `--method PUT`) on `repos/<repo>/pulls/<n>/merge`;
- the GitHub MCP server's `merge_pull_request` for that owner and repository.

The ship script itself runs `gh` or `claude-ops merge` from its own process, not as a session's
tool call, so it is not refused. Other repositories are not touched.

The repositories (owner/name, any case) and the message come from `--repo` (repeatable) and
`--message`, else from a JSON file:

    {
      "repositories": ["example-org/service"],
      "message": "Ship it with tools/ship.sh <pr> <worktree>."
    }

The file is `--config`, else `CLAUDE_OPS_MERGE_GATE_CONFIG`, else
`~/.config/claude-ops/merge-gate.json`. With no repositories nothing is refused. On anything
unexpected the call goes through: a broken hook must never stop work.

    python3 -P -m claude_ops.hooks.merge_gate [--repo OWNER/NAME ...] [--message TEXT] [--config FILE]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from . import commandline

DEFAULT_CONFIG = "~/.config/claude-ops/merge-gate.json"
#: a GitHub remote's owner/name, from an https or ssh URL
REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
#: the merge endpoint of the REST API
MERGE_PATH = re.compile(r"(?:^|/)repos/([^/]+)/([^/]+)/pulls/\d+/merge/?$")
#: the GitHub MCP server's merge tool, under whatever name the server is registered
MCP_MERGE = re.compile(r"^mcp__[^_]*github[^_]*__merge_pull_request$")


def config(argv: list[str]) -> tuple[set[str], str]:
    """(the gated repositories in lower case, the extra message) from the command line or the file."""
    repos, message, path, i = [], "", None, 0
    while i < len(argv):
        if argv[i] in ("--repo", "--message", "--config") and i + 1 < len(argv):
            if argv[i] == "--repo":
                repos.append(argv[i + 1])
            elif argv[i] == "--message":
                message = argv[i + 1]
            else:
                path = argv[i + 1]
            i += 2
        else:
            i += 1
    if not repos:
        path = path or os.environ.get("CLAUDE_OPS_MERGE_GATE_CONFIG") or DEFAULT_CONFIG
        try:
            with open(os.path.expanduser(path)) as f:
                data = json.load(f)
            repos = list(data.get("repositories") or [])
            message = message or str(data.get("message") or "")
        except (OSError, ValueError):
            return set(), ""
    return {r.strip().lower() for r in repos if r and "/" in r}, message


def repo_of(folder: str) -> str | None:
    """owner/name of the GitHub remote `origin` of the clone at `folder`, in lower case, or None."""
    try:
        p = subprocess.run(["git", "-C", folder, "remote", "get-url", "origin"], capture_output=True,
                           text=True, timeout=commandline.GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    m = REMOTE.search(p.stdout.strip()) if p.returncode == 0 else None
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def option(words: list[str], *names: str) -> str | None:
    """The value of the first of `names` among `words` (`--repo X` or `--repo=X`), or None."""
    for k, w in enumerate(words):
        for n in names:
            if w == n and k + 1 < len(words):
                return words[k + 1]
            if n.startswith("--") and w.startswith(n + "="):
                return w.split("=", 1)[1]
    return None


def merged_repo(words: list[str], here: str) -> str | None:
    """The repository a simple command merges a pull request in, or None when it merges none."""
    if not words or "-h" in words or "--help" in words:   # asking how to merge merges nothing
        return None
    prog = os.path.basename(words[0])
    if prog == "claude-ops" and words[1:2] == ["merge"]:
        repo = option(words[2:], "--repo")
        return repo.lower() if repo else repo_of(here)
    if prog == "gh" and words[1:3] == ["pr", "merge"]:
        repo = option(words[3:], "-R", "--repo")
        return repo.lower() if repo else repo_of(here)
    if prog == "gh" and words[1:2] == ["api"]:
        method = (option(words[2:], "-X", "--method") or "").upper()
        if method != "PUT":
            return None
        for w in words[2:]:
            m = MERGE_PATH.search(w.split("?", 1)[0])
            if m:
                return f"{m.group(1)}/{m.group(2)}".lower()
    return None


def check(payload: dict, repos: set[str]) -> str | None:
    """Why the call is refused, or None."""
    if not repos:
        return None
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    if tool == "Bash":
        for words, here in commandline.walk(str(ti.get("command", "")), payload.get("cwd") or os.getcwd()):
            repo = merged_repo(words, here)
            if repo in repos:
                return f"`{' '.join(words[:4])}` merges a pull request in {repo} by hand"
        return None
    if MCP_MERGE.match(tool):
        repo = f"{ti.get('owner', '')}/{ti.get('repo', '')}".lower()
        if repo in repos:
            return f"{tool} merges pull request {ti.get('pullNumber', '?')} in {repo} by hand"
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        repos, message = config(list(sys.argv[1:] if argv is None else argv))
        payload = json.load(sys.stdin)
        why = check(payload, repos)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}. This repository merges only through its ship script, which runs the "
              "whole suite on the branch rebased onto the newest main branch, under a lock, before it "
              "merges. A merge by hand skips both, and when it breaks the main branch every waiting ship "
              "holds on that red." + (f" {message}" if message else ""), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
