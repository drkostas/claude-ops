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

The file may also hold a second list, of repositories whose pull requests merge by hand but only
once someone approved the head being merged:

    {
      "approval": {
        "repositories": ["example-org/library"],
        "command": ["approved", "--quiet"],
        "message": "Ask the reviewer for an approval of the new head.",
        "timeout": 20
      }
    }

For a merge in one of them, the hook reads the pull request's head from GitHub at the time of the
call (`gh api repos/<repo>/pulls/<n>`) and runs the command with three more arguments: the
repository, the number and that head's full sha. Exit 0 lets the merge through. Any other answer
refuses it with the command's own last line: an exit code, no answer within `timeout` seconds, a
command that cannot start, a head GitHub would not give, or a merge whose number the hook cannot
read (`gh pr merge` on the current branch). This list exists to stop exactly those merges, so here a
failure refuses instead of letting the call through.

    python3 -P -m claude_ops.hooks.merge_gate [--repo OWNER/NAME ...] [--message TEXT] [--config FILE]
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from . import commandline

DEFAULT_CONFIG = "~/.config/claude-ops/merge-gate.json"
#: a GitHub remote's owner/name, from an https or ssh URL
REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
#: the merge endpoint of the REST API
MERGE_PATH = re.compile(r"(?:^|/)repos/([^/]+)/([^/]+)/pulls/(\d+)/merge/?$")
#: a pull request's address, as `gh pr merge` takes it
PR_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")
#: the options that take a value, so their value is not read as a pull request
VALUE_OPTIONS = {
    "claude-ops": {"--repo", "--method", "--timeout", "--poll", "--dependency-bump", "--bot", "--prod-environment"},
    "gh": {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email",
           "--match-head-commit"},
}
#: how long the approval command may take, unless the file says otherwise
APPROVAL_TIMEOUT_S = 20.0
#: how long the read of a pull request's head from GitHub may take
HEAD_TIMEOUT_S = 10.0
SHA = re.compile(r"^[0-9a-f]{40}$")
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


def _config_file(argv: list[str]) -> dict | None:
    path, i = None, 0
    while i < len(argv):
        if argv[i] == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
        i += 1
    path = path or os.environ.get("CLAUDE_OPS_MERGE_GATE_CONFIG") or DEFAULT_CONFIG
    try:
        with open(os.path.expanduser(path)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def approval_config(argv: list[str]) -> dict | None:
    """The file's approval list: {repositories (lower case), command (argv), message, timeout}, or None
    when the file has none. A list without a command is kept, and every merge in it is refused."""
    data = (_config_file(argv) or {}).get("approval")
    if not isinstance(data, dict):
        return None
    repos = {str(r).strip().lower() for r in data.get("repositories") or [] if r and "/" in str(r)}
    if not repos:
        return None
    cmd = data.get("command") or []
    cmd = shlex.split(cmd) if isinstance(cmd, str) else [str(a) for a in cmd]
    try:
        timeout = float(data.get("timeout") or APPROVAL_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = APPROVAL_TIMEOUT_S
    return {"repositories": repos, "command": cmd, "message": str(data.get("message") or ""), "timeout": timeout}


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


def positionals(words: list[str], prog: str) -> list[str]:
    """The words that are not options or an option's value."""
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w.startswith("-"):
            skip = w in VALUE_OPTIONS.get(prog, set())
        else:
            out.append(w)
    return out


def merge_target(words: list[str], here: str) -> tuple[str | None, list[int]]:
    """(the repository a simple command merges a pull request in, the numbers it merges), with no
    repository when it merges none and no numbers when they cannot be read from the command."""
    if not words or "-h" in words or "--help" in words:   # asking how to merge merges nothing
        return None, []
    prog = os.path.basename(words[0])
    if prog == "claude-ops" and words[1:2] == ["merge"]:
        repo = option(words[2:], "--repo")
        return (repo.lower() if repo else repo_of(here),
                [int(w) for w in positionals(words[2:], prog) if w.isdigit()])
    if prog == "gh" and words[1:3] == ["pr", "merge"]:
        repo = option(words[3:], "-R", "--repo")
        args = positionals(words[3:], prog)
        url = PR_URL.search(args[0]) if args else None
        if url:
            return f"{url.group(1)}/{url.group(2)}".lower(), [int(url.group(3))]
        return (repo.lower() if repo else repo_of(here),
                [int(args[0])] if args and args[0].isdigit() else [])
    if prog == "gh" and words[1:2] == ["api"]:
        method = (option(words[2:], "-X", "--method") or "").upper()
        if method != "PUT":
            return None, []
        for w in words[2:]:
            m = MERGE_PATH.search(w.split("?", 1)[0])
            if m:
                return f"{m.group(1)}/{m.group(2)}".lower(), [int(m.group(3))]
    return None, []


def merged_repo(words: list[str], here: str) -> str | None:
    """The repository a simple command merges a pull request in, or None when it merges none."""
    return merge_target(words, here)[0]


def merge_calls(payload: dict) -> list[tuple[str, list[int], str]]:
    """Each merge the call makes: (repository, the numbers it merges, the words that make it)."""
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    out = []
    if tool == "Bash":
        for words, here in commandline.walk(str(ti.get("command", "")), payload.get("cwd") or os.getcwd()):
            repo, numbers = merge_target(words, here)
            if repo:
                out.append((repo, numbers, f"`{' '.join(words[:4])}`"))
    elif MCP_MERGE.match(tool):
        repo = f"{ti.get('owner', '')}/{ti.get('repo', '')}".lower()
        number = str(ti.get("pullNumber", ""))
        out.append((repo, [int(number)] if number.isdigit() else [], tool))
    return out


def head_of(repo: str, number: int, timeout: float = HEAD_TIMEOUT_S) -> tuple[str | None, str]:
    """(the pull request's head sha as GitHub has it now, or None with why)."""
    gh = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh")
                                     if os.path.exists(p)), None)
    if not gh:
        return None, "gh is not installed"
    try:
        p = subprocess.run([gh, "api", f"repos/{repo}/pulls/{int(number)}", "--jq", ".head.sha"],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"GitHub did not answer within {timeout:g} s"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"gh could not start ({e})"
    sha = p.stdout.strip()
    if p.returncode != 0 or not SHA.match(sha):
        return None, _last_line(p.stderr) or _last_line(p.stdout) or f"gh exited {p.returncode}"
    return sha, ""


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def ask(command: list[str], repo: str, number: int, sha: str, timeout: float) -> tuple[bool, str]:
    """(whether the approval command says yes for this head, its last line or why it gave no answer)."""
    argv = [os.path.expanduser(command[0]), *command[1:], repo, str(int(number)), sha]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"the approval command did not answer within {timeout:g} s"
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return False, f"the approval command could not start ({e})"
    said = _last_line(p.stderr) or _last_line(p.stdout)
    if p.returncode == 0:
        return True, said
    return False, said or f"the approval command exited {p.returncode}"


def approve(payload: dict, gate: dict | None, *, head=None, answer=None) -> str | None:
    """Why a merge in the approval list is refused, or None. A merge it cannot check is refused.
    `head` and `answer` stand in for head_of and ask."""
    if not gate:
        return None
    head, answer = head or head_of, answer or ask
    for repo, numbers, what in merge_calls(payload):
        if repo not in gate["repositories"]:
            continue
        if not numbers:
            return (f"{what} merges a pull request in {repo} without naming its number, so its approval "
                    "cannot be checked. Name the pull request by its number")
        for n in numbers:
            if not gate.get("command"):
                return f"{what} merges {repo}#{n}, which needs an approval, and no approval command is set"
            sha, err = head(repo, n)
            if not sha:
                return f"{what} merges {repo}#{n}, and its head could not be read from GitHub ({err})"
            ok, said = answer(gate["command"], repo, n, sha, gate["timeout"])
            if not ok:
                return f"{what} merges {repo}#{n} at {sha[:12]}, a head that is not approved ({said})"
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
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        repos, message = config(argv)
        payload = json.load(sys.stdin)
        why = check(payload, repos)
        gate = approval_config(argv)
        gated = gate and any(repo in gate["repositories"] for repo, _n, _w in merge_calls(payload))
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}. This repository merges only through its ship script, which runs the "
              "whole suite on the branch rebased onto the newest main branch, under a lock, before it "
              "merges. A merge by hand skips both, and when it breaks the main branch every waiting ship "
              "holds on that red." + (f" {message}" if message else ""), file=sys.stderr)
        return 2
    if not gated:
        return 0
    try:
        refused = approve(payload, gate)
    except Exception as e:  # noqa: BLE001 - this list exists to stop a merge, so a check that breaks refuses it
        refused = f"the approval check failed ({type(e).__name__}: {e})"
    if refused:
        print(f"Blocked: {refused}. This repository merges a pull request only once its head is approved."
              + (f" {gate['message']}" if gate["message"] else ""), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
