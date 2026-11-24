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

With `--hosted-only` it also reports a job whose `runs-on` is not a runner GitHub hosts, fixed by
the file itself. A repository whose code anyone can read usually wants every job on a hosted
runner, because a pull request from a fork would otherwise run on a machine of its own, and a value
the file leaves open, such as a repository variable, is enough to send it there without the
workflow changing. A matrix of hosted labels still passes, since the file fixes those values.

    claude-ops fork-guard .github/workflows --hosted-only

It reads the shell of a `run` step and the script of an actions/github-script step, and it knows a
few third party actions that act on the repository through their own inputs (actions/stale,
dessant/lock-threads, peter-evans/create-issue-from-file). Another such action is not seen.
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

#: third party actions whose own inputs act on the repository's issues, pull requests or comments,
#: matched against a step's `uses:`. A `run` step that only names one of these slugs is not a use of it.
ACTS_ON_REPO_ACTIONS = re.compile(
    r"^(actions/stale|dessant/lock-threads|peter-evans/create-issue-from-file)(?=[/@]|$)",
    re.IGNORECASE,
)

#: an `if:` that limits a job or step to one repository (github.repository or github.repository_owner)
UPSTREAM_GUARD = re.compile(r"github\.repository(_owner)?\s*==")

#: a label of a runner GitHub hosts, as a workflow names it (ubuntu-latest, ubuntu-24.04, macos-14)
HOSTED_LABEL = re.compile(r"^(ubuntu|macos|windows)-[0-9a-z._]+$", re.IGNORECASE)

#: a `${{ ... }}` expression, and the one shape of it a workflow file fixes by itself
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
MATRIX_READ = re.compile(r"^\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*$")


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
            uses = step.get("uses")
            acts = ACTS_ON_REPO.search(_step_text(step)) or (
                isinstance(uses, str) and ACTS_ON_REPO_ACTIONS.search(uses.strip()))
            if acts and not UPSTREAM_GUARD.search(str(step.get("if", ""))):
                hits.append(str(name))
                break
    return hits


def _matrix_values(job: dict, key: str) -> list | None:
    """The values this job's own matrix gives `key`, or None when the file does not fix them."""
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    values = matrix.get(key) if isinstance(matrix, dict) else None
    if isinstance(values, list) and values and all(isinstance(v, (str, int, float)) for v in values):
        return [str(v) for v in values]
    return None


def hosted_runner(runs_on, job: dict) -> bool:
    """Pure. Whether `runs-on` names a runner GitHub hosts and the workflow file fixes which one.

    A list or a map of labels is a runner group or a self-hosted set, so it is not one. An
    expression is one only when it reads this job's own matrix and every value there is a hosted
    label, because then the file still says which runners the job can use."""
    if not isinstance(runs_on, str):
        return False
    text = runs_on.strip()
    found = EXPRESSION.findall(text)
    if not found:
        return bool(HOSTED_LABEL.match(text))
    if len(found) != 1 or EXPRESSION.sub("", text).strip():
        return False                      # several expressions, or one mixed with other text
    key = MATRIX_READ.match(found[0])
    if not key:
        return False                      # it reads something the file does not fix
    values = _matrix_values(job, key.group(1))
    return bool(values) and all(HOSTED_LABEL.match(v) for v in values)


def not_hosted(doc: dict) -> list:
    """The jobs in one parsed workflow that do not run on a hosted runner the file fixes, as
    [{"job", "runs_on"}]. A job with no `runs-on` of its own (one that calls another workflow) is
    not one of them."""
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    out = []
    for name, job in jobs.items():
        if not isinstance(job, dict) or job.get("runs-on") is None:
            continue
        runs_on = job["runs-on"]
        if not hosted_runner(runs_on, job):
            out.append({"job": str(name),
                        "runs_on": runs_on if isinstance(runs_on, str) else json.dumps(runs_on)})
    return out


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


def scan(path: Path, hosted_only: bool = False) -> dict:
    """{"hits": [{"file", "job"}], "runners": [{"file", "job", "runs_on"}], "unreadable":
    [{"file", "error"}]} for the workflows at `path`. `runners` is empty unless `hosted_only`."""
    import yaml   # lazy, so the rest of the package needs no yaml
    hits, runners, unreadable = [], [], []
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
        if hosted_only:
            for r in not_hosted(doc):
                runners.append({"file": str(f), **r})
    return {"hits": hits, "runners": runners, "unreadable": unreadable}


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="claude-ops fork-guard",
        description="find scheduled workflows that act on the repository without a fork guard")
    p.add_argument("path", nargs="?", default=".github/workflows",
                   help="a workflow file, a folder of them, or a repository root")
    p.add_argument("--json", action="store_true")
    p.add_argument("--hosted-only", action="store_true",
                   help="also fail a job whose runs-on is not a hosted runner this file fixes")
    a = p.parse_args(argv)
    try:
        import yaml   # noqa: F401
    except ImportError:
        print("fork-guard needs PyYAML (pip install 'claude-ops[yaml]')")
        return 2
    out = scan(Path(a.path), a.hosted_only)
    bad = out["hits"] or out["runners"]
    if a.json:
        print(json.dumps(out, indent=1))
        return 1 if bad else 0
    for h in out["hits"]:
        print(f"{h['file']}::{h['job']} acts on issues, pull requests or comments with no "
              f"`github.repository ==` guard")
    for r in out["runners"]:
        print(f"{r['file']}::{r['job']} runs on {r['runs_on']}, which this file does not fix as a "
              f"runner GitHub hosts, so the job can be sent to a machine of its own without the "
              f"workflow changing")
    for u in out["unreadable"]:
        print(f"unreadable: {u['file']} ({u['error']})")
    if not bad:
        print("no unguarded scheduled jobs found"
              + (" and every job runs on a hosted runner" if a.hosted_only else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
