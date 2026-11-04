"""Refuse a session's own addition of a label that only some sessions may add (a PreToolUse hook).

Some labels change what a tool does with a pull request: a label that sends it ahead of a merge
queue, one that skips a review step, one that starts a deploy. Every session on a machine usually
signs in to GitHub as the same account, so GitHub itself lets any of them add such a label, and a
session that means well can put its own change ahead of everyone else's. This hook refuses that call
(exit 2, with the reason sent back to the model) unless the session is one the rule names:

- `gh pr edit` or `gh issue edit` with `--add-label` (repeated, or several names with commas);
- `gh pr create` or `gh issue create` with `-l`/`--label`;
- `gh api` that writes to `repos/<owner>/<name>/issues/<n>/labels` or `repos/<owner>/<name>/issues/<n>`
  with the label in a field (`-f labels[]=<label>`) or in the JSON it sends (`--input <file>`, or a
  here-document on `--input -`);
- a GitHub MCP tool call for that owner and repository whose `labels` names the label.

The repository is `-R`/`--repo`, else the one in a pull request or issue address on the line, else
`GH_REPO` set in front of the command, else the clone the command runs in. A program that adds the
label from its own process (a queue tool that checks who asks before it labels) is not a session's
tool call, so it is not refused. A label in a form the hook cannot read (a GraphQL mutation, which
names labels by id, or JSON piped in from another program) is not judged. Taking a label off is
never refused. Label names are compared without case, as GitHub compares them.

The rules come from a JSON file:

    {"rules": [
      {"repositories": ["example-org/service"],
       "labels": ["queue:first"],
       "sessions": ["0d0e0f10-1111-4222-8333-444455556666"],
       "message": "Ask the release session to put a pull request first."}
    ]}

`repositories` empty or left out means every repository. `sessions` are the session ids (the
`session_id` Claude Code gives each hook call) that may add the label, and empty or left out means
no session may: only a program does. The file is `--config`, else `CLAUDE_OPS_GUARDED_LABELS_CONFIG`,
else `~/.config/claude-ops/guarded-labels.json`. With no rules nothing is refused. On anything
unexpected the call goes through: a broken hook must never stop work.

    python3 -P -m claude_ops.hooks.guarded_labels [--config FILE]
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import commandline
from .merge_gate import option, repo_of

DEFAULT_CONFIG = "~/.config/claude-ops/guarded-labels.json"
#: a pull request or issue address, whose repository the command acts on
ADDRESS = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/(?:pull|issues)/\d+")
#: the REST paths that set an issue's or a pull request's labels
LABEL_PATH = re.compile(r"(?:^|/)repos/([^/]+)/([^/]+)/issues/\d+(?:/labels)?/?$")
#: the `gh api` options that send a field, which make its request a POST when no method is given
FIELD_OPTIONS = ("-f", "--raw-field", "-F", "--field")
#: a GitHub MCP tool, under whatever name the server is registered
MCP_GITHUB = re.compile(r"^mcp__[^_]*github[^_]*__")
#: how much of an --input file is read
INPUT_LIMIT = 1_000_000


def config(argv: list[str]) -> list[dict]:
    """The rules, each {"repositories": set or None, "labels": set, "sessions": set, "message": str},
    from `--config`, the environment or the default file; [] when none is readable."""
    path = None
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
    path = path or os.environ.get("CLAUDE_OPS_GUARDED_LABELS_CONFIG") or DEFAULT_CONFIG
    try:
        with open(os.path.expanduser(path)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    rules = []
    for r in (data.get("rules") or []) if isinstance(data, dict) else []:
        if not isinstance(r, dict):
            continue
        labels = {str(x).strip().lower() for x in r.get("labels") or [] if str(x).strip()}
        if not labels:
            continue
        repos = {str(x).strip().lower() for x in r.get("repositories") or [] if "/" in str(x)}
        rules.append({"repositories": repos or None, "labels": labels,
                      "sessions": {str(x) for x in r.get("sessions") or [] if x},
                      "message": str(r.get("message") or "")})
    return rules


def names(values: list[str]) -> set[str]:
    """Label names from option values, each of which may hold several separated by commas."""
    return {n.strip().lower() for v in values for n in v.split(",") if n.strip()}


def values(words: list[str], *opts: str) -> list[str]:
    """Every value of the options `opts` among `words` (`--label X`, `--label=X`, `-l X`)."""
    out = []
    for k, w in enumerate(words):
        for n in opts:
            if w == n and k + 1 < len(words):
                out.append(words[k + 1])
            elif n.startswith("--") and w.startswith(n + "="):
                out.append(w.split("=", 1)[1])
    return out


def in_json(text: str) -> set[str]:
    """The label names in a request body that sets labels: `{"labels": ["a", {"name": "b"}]}`."""
    try:
        data = json.loads(text)
    except ValueError:
        return set()
    items = data.get("labels") if isinstance(data, dict) else data if isinstance(data, list) else None
    out = set()
    for x in items or []:
        name = x.get("name") if isinstance(x, dict) else x
        if isinstance(name, str) and name.strip():
            out.add(name.strip().lower())
    return out


def api_labels(words: list[str], here: str, heredoc: str | None) -> tuple[str | None, set[str]]:
    """(repository, labels) a `gh api` call sets, or (None, set()) when it sets none."""
    args = words[2:]
    path = next((m for w in args if (m := LABEL_PATH.search(w.split("?", 1)[0]))), None)
    if not path:
        return None, set()
    method = (option(args, "-X", "--method") or "").upper()
    sends = any(w in FIELD_OPTIONS or w.startswith(("--raw-field=", "--field=")) for w in args)
    source = option(args, "--input")
    if method == "GET" or not (method or sends or source):
        return None, set()
    found = set()
    for v in values(args, *FIELD_OPTIONS):
        key, _, value = v.partition("=")
        if key.strip() in ("labels[]", "labels"):
            found |= names([value])
    if source == "-":
        found |= in_json(heredoc or "")
    elif source:
        try:
            with open(os.path.join(here, os.path.expanduser(source))) as f:
                found |= in_json(f.read(INPUT_LIMIT))
        except (OSError, UnicodeDecodeError):
            pass
    return f"{path.group(1)}/{path.group(2)}".lower(), found


def added_labels(cmd: dict, here: str) -> tuple[str | None, set[str]]:
    """(repository, labels) a simple command adds, or (None, set()) when it adds none."""
    words = cmd["words"]
    if os.path.basename(words[0]) != "gh" or "-h" in words or "--help" in words:
        return None, set()
    if words[1:2] == ["api"]:
        return api_labels(words, here, cmd.get("heredoc"))
    if words[1:2] not in (["pr"], ["issue"]) or words[2:3] not in (["edit"], ["create"]):
        return None, set()
    opts = ("--add-label",) if words[2] == "edit" else ("-l", "--label")
    found = names(values(words[3:], *opts))
    if not found:
        return None, set()
    repo = option(words[3:], "-R", "--repo")
    if not repo:
        address = next((m for w in words[3:] if (m := ADDRESS.search(w))), None)
        repo = f"{address.group(1)}/{address.group(2)}" if address else None
    repo = repo or cmd.get("env", {}).get("GH_REPO") or repo_of(here)
    return (repo.lower() if repo else None), found


def refused(rules: list[dict], repo: str | None, labels: set[str], session: str) -> tuple[str, str] | None:
    """(the label, the rule's message) when a rule forbids this session to add it here, else None."""
    for r in rules:
        if r["repositories"] is not None and repo not in r["repositories"]:
            continue
        if session and session in r["sessions"]:
            continue
        hit = sorted(labels & r["labels"])
        if hit:
            return hit[0], r["message"]
    return None


def check(payload: dict, rules: list[dict]) -> str | None:
    """Why the call is refused, or None."""
    if not rules:
        return None
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    session = str(payload.get("session_id") or "")
    if tool == "Bash":
        for cmd, here in commandline.steps(str(ti.get("command", "")), payload.get("cwd") or os.getcwd()):
            repo, labels = added_labels(cmd, here)
            hit = refused(rules, repo, labels, session) if labels else None
            if hit:
                where = f" in {repo}" if repo else ""
                return (f"`{' '.join(cmd['words'][:4])}` adds the label {hit[0]}{where}, which only some "
                        "sessions may add." + (f" {hit[1]}" if hit[1] else ""))
        return None
    if MCP_GITHUB.match(tool) and isinstance(ti.get("labels"), list):
        repo = f"{ti.get('owner', '')}/{ti.get('repo', '')}".lower()
        hit = refused(rules, repo, in_json(json.dumps({"labels": ti["labels"]})), session)
        if hit:
            return (f"{tool} sets the label {hit[0]} in {repo}, which only some sessions may add."
                    + (f" {hit[1]}" if hit[1] else ""))
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        rules = config(list(sys.argv[1:] if argv is None else argv))
        payload = json.load(sys.stdin)
        why = check(payload, rules)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
