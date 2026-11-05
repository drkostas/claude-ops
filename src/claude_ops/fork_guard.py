"""fork_guard.py — scheduled workflows that act on the repository and would misfire on a fork.

A workflow that runs on a schedule or on workflow_dispatch also runs in anyone's fork of the
repository, on the fork's own schedule or button. When such a workflow reads or writes the issues,
pull requests or comments of the repository, the copy in a fork acts on the fork, not on the
original. A fork usually has issues turned off, so the job fails for everyone who forked the
project, and when issues are on it writes to the wrong place.

The fix is a guard on the job, so only the original repository runs it and every fork skips it.

    jobs:
      watch:
        if: github.repository == 'owner/name'

This finds the jobs that need that guard and do not have it. It reads the workflow files under a
path (a single file, or a folder such as .github/workflows). For each workflow that a schedule or
a workflow_dispatch can start, it reports a job that acts on issues, pull requests or comments
while neither the job nor the acting step carries a `github.repository ==` guard.

    claude-ops fork-guard .github/workflows
    claude-ops fork-guard . --json

The exit status is 1 when it finds an unguarded job, so a continuous integration step can fail a
pull request that adds one. It needs PyYAML (pip install 'claude-ops[yaml]').

It reads the shell of a `run` step and the script of an actions/github-script step. A third party
action that touches issues through its own inputs is not seen.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

#: a schedule or a manual dispatch starts the workflow in a fork too, unlike push or pull_request
FORK_TRIGGERS = {"schedule", "workflow_dispatch"}

#: text in a step that reads or writes the repository's issues, pull requests or comments
ACTS_ON_REPO = re.compile(
    r"\bgh\s+(issue|pr)\b"                          # gh issue ... / gh pr ...
    r"|\bgh\s+api\b[^\n]*(issues|pulls|comments)"   # gh api .../issues|pulls|comments
    r"|github\.(rest\.)?(issues|pulls)\."           # actions/github-script
    r"|/repos/[^\s'\"]+/(issues|pulls)",            # a REST path to issues or pull requests
    re.IGNORECASE,
)

#: an `if:` that limits a job or step to one repository (github.repository or github.repository_owner)
UPSTREAM_GUARD = re.compile(r"github\.repository(_owner)?\s*==")


def _triggers(on_value) -> set:
    """The trigger names in a workflow's `on:`, whether it is a word, a list or a map."""
    if isinstance(on_value, str):
        return {on_value}
    if isinstance(on_value, list):
        return {t for t in on_value if isinstance(t, str)}
    if isinstance(on_value, dict):
        return set(on_value.keys())
    return set()


def _step_text(step: dict) -> str:
    """The shell and the github-script of one step, joined, for the pattern search."""
    parts = []
    run = step.get("run")
    if isinstance(run, str):
        parts.append(run)
    with_ = step.get("with")
    if isinstance(with_, dict) and isinstance(with_.get("script"), str):
        parts.append(with_["script"])
    return "\n".join(parts)


def unguarded(doc: dict) -> list:
    """The names of the jobs in one parsed workflow that act on the repository without a guard.

    Empty when the workflow is neither schedule nor workflow_dispatch, or when every such job is
    guarded on the job or on the acting step."""
    on_value = doc.get("on")
    if on_value is None:
        on_value = doc.get(True)   # PyYAML reads the bare word `on` as the boolean True
    if not (_triggers(on_value) & FORK_TRIGGERS):
        return []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    hits = []
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if UPSTREAM_GUARD.search(str(job.get("if", ""))):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            if ACTS_ON_REPO.search(_step_text(step)) and not UPSTREAM_GUARD.search(str(step.get("if", ""))):
                hits.append(str(name))
                break
    return hits


def workflow_files(path: Path) -> list:
    """The workflow files at `path`: the file itself, the yaml files in the folder, or, when the
    folder holds none, the yaml files in its .github/workflows."""
    path = Path(path)
    if path.is_file():
        return [path]
    if path.is_dir():
        here = sorted(p for p in path.iterdir() if p.suffix in (".yml", ".yaml"))
        if here:
            return here
        workflows = path / ".github" / "workflows"
        if workflows.is_dir():
            return sorted(p for p in workflows.iterdir() if p.suffix in (".yml", ".yaml"))
    return []


def scan(path: Path) -> dict:
    """{"hits": [{"file", "job"}], "unreadable": [{"file", "error"}]} for the workflows at `path`."""
    import yaml   # lazy, so the rest of the package needs no yaml
    hits, unreadable = [], []
    for f in workflow_files(path):
        try:
            doc = yaml.safe_load(f.read_text(errors="replace"))
        except yaml.YAMLError as e:
            unreadable.append({"file": str(f), "error": str(e).splitlines()[0]})
            continue
        if not isinstance(doc, dict):
            continue
        for job in unguarded(doc):
            hits.append({"file": str(f), "job": job})
    return {"hits": hits, "unreadable": unreadable}


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="claude-ops fork-guard",
        description="find scheduled workflows that act on the repository without a fork guard")
    p.add_argument("path", nargs="?", default=".github/workflows",
                   help="a workflow file, a folder of them, or a repository root")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        import yaml   # noqa: F401
    except ImportError:
        print("fork-guard needs PyYAML (pip install 'claude-ops[yaml]')")
        return 2
    out = scan(Path(a.path))
    if a.json:
        print(json.dumps(out, indent=1))
        return 1 if out["hits"] else 0
    for h in out["hits"]:
        print(f"{h['file']}::{h['job']} acts on issues, pull requests or comments with no "
              f"`github.repository ==` guard")
    for u in out["unreadable"]:
        print(f"unreadable: {u['file']} ({u['error']})")
    if not out["hits"]:
        print("no unguarded scheduled jobs found")
    return 1 if out["hits"] else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
