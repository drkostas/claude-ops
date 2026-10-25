"""merge.py — merge a GitHub pull request only when every check on its current head has passed.

Waiting for checks and merging as two separate steps lets a pull request be merged while a check
is failing: the wait reads one moment, the merge happens at another, and a shortened log line can
hide a red job. This does both in one step through the gh CLI. It reads the checks from gh's JSON
(never a printed table), refuses on any check that did not pass and on a pull request with no
checks at all, and merges with --match-head-commit, so a push between the reading and the merge
makes GitHub refuse the merge instead of merging code nobody checked.

    claude-ops merge 123                         # the repository of the current folder
    claude-ops merge 123 --repo owner/name --method squash --timeout 45
    claude-ops merge 123 --repo owner/name --dependency-bump minor   # only a bot's dependency update
    claude-ops merge 12 34 56 --repo owner/name                      # several at once, in turn
    claude-ops merge 12 34 56 --repo owner/name --confirm-prod       # then wait for the deploy

`--method auto` uses the first of merge, squash and rebase the repository allows, so one command
serves repositories with different settings. `--dependency-bump patch|minor` refuses anything but a
bot's update of lockfiles and manifests within that level (dependency_bump.py), before any waiting.

Several numbers merge in turn (`cascade`): each is merged only when it is clean, and a pull request
that falls behind its base is brought up to date between merges, so a set of green pull requests
under a branch rule that requires them to be up to date merges one after another without a person.
`--confirm-prod` then waits for the last merge commit's own checks and for a deployment of it to
succeed.

Exit codes: 0 merged, 2 checks still running at the timeout (or none reported), 3 a check did not
pass, 4 gh refused the merge, 5 the pull request is not open, 6 not the dependency update asked for.
The cascade uses 0 all merged, 2 some still not ready at the timeout, 3 one refused, 7 the
production confirmation did not pass.
"""
from __future__ import annotations

import json
import subprocess
import time

from . import dependency_bump
from . import dependency_bump as dependency_bump_mod

#: a check counts as passed only with one of these conclusions
PASSED = {"SUCCESS", "SKIPPED"}
#: a check whose result is not in yet
WAITING = {"", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
METHODS = ("merge", "squash", "rebase", "auto")
#: what a look at the pull request reads; the dependency gate reads the rest
FIELDS = "headRefOid,state,statusCheckRollup,mergeCommit"
GATE_FIELDS = FIELDS + ",title,body,author,files,url"


def _gh(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, env=env)


def verdict(rollup: list[dict]) -> tuple[str, list[str]]:
    """Pure. ("passed" | "waiting" | "failed" | "none", the checks that hold it up), from gh's
    statusCheckRollup: check runs carry status and conclusion, commit statuses carry state."""
    if not rollup:
        return "none", []
    waiting, bad = [], []
    for c in rollup:
        name = c.get("name") or c.get("context") or "?"
        if "state" in c and "conclusion" not in c:            # a commit status
            state = (c.get("state") or "").upper()
            if state in WAITING:
                waiting.append(name)
            elif state != "SUCCESS":
                bad.append(f"{name}: {state.lower()}")
            continue
        if (c.get("status") or "").upper() != "COMPLETED":
            waiting.append(name)
        elif (c.get("conclusion") or "").upper() not in PASSED:
            bad.append(f"{name}: {(c.get('conclusion') or 'none').lower()}")
    if bad:
        return "failed", bad
    if waiting:
        return "waiting", waiting
    return "passed", []


def _view(number: str, repo: str | None, gh, fields: str = FIELDS) -> dict:
    args = ["pr", "view", str(number), "--json", fields]
    r = gh(args + (["--repo", repo] if repo else []))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return json.loads(r.stdout)


def pull_request(number: str, repo: str | None = None, *, fields: str = GATE_FIELDS, gh=None,
                 env: dict | None = None) -> dict:
    """The pull request as gh prints it, with `fields`. Raises RuntimeError when gh cannot read it.
    `env` is the environment gh runs with (it needs HOME for its login)."""
    return _view(number, repo, gh or (lambda a: _gh(a, env)), fields)


def allowed_methods(repo: str | None, gh) -> list[str]:
    """The merge methods the repository allows, in this module's order of preference."""
    args = ["repo", "view", "--json", "mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed"]
    r = gh(args + ([repo] if repo else []))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    got = json.loads(r.stdout)
    keys = (("merge", "mergeCommitAllowed"), ("squash", "squashMergeAllowed"), ("rebase", "rebaseMergeAllowed"))
    return [m for m, k in keys if got.get(k)]


def merge_when_green(number: str, repo: str | None = None, *, method: str = "merge",
                     timeout_s: float = 1800, poll_s: float = 20, settle_s: float = 60,
                     allow_no_checks: bool = False, dependency_bump: str | None = None,
                     dependency_bots=None,
                     gh=None, env: dict | None = None, sleep=time.sleep,
                     clock=time.monotonic) -> tuple[int, str]:
    """Wait for every check on the pull request's head, then merge it if all passed.
    Returns (exit code, what happened). `settle_s` is how long "no checks yet" counts as waiting,
    since checks register a little after a push. `dependency_bump` ("patch" or "minor") merges only
    a bot's update of lockfiles and manifests within that level, read once before any waiting."""
    if method not in METHODS:
        return 2, f"method is one of {', '.join(METHODS)}"
    if gh is None:
        gh = lambda a: _gh(a, env)   # noqa: E731 - the default gh, with the caller's environment
    if dependency_bump is not None:
        try:
            pr = _view(number, repo, gh, GATE_FIELDS)
            diff = _diff(number, repo, gh)
        except RuntimeError as e:
            return 4, f"could not read the pull request: {e}"
        gate = dependency_bump_review(pr, dependency_bump, diff,
                                      dependency_bots or dependency_bump_mod.UPDATERS)
        if not gate.ok:
            return 6, "not merged on its own: " + gate.reason
    if method == "auto":
        try:
            allowed = allowed_methods(repo, gh)
        except RuntimeError as e:
            return 4, f"could not read the repository's merge methods: {e}"
        if not allowed:
            return 4, "the repository allows no merge method gh knows"
        method = allowed[0]
    t0 = clock()
    while True:
        try:
            pr = _view(number, repo, gh)
        except RuntimeError as e:
            return 4, f"could not read the pull request: {e}"
        if pr.get("state") != "OPEN":
            return 5, f"the pull request is {(pr.get('state') or 'unknown').lower()}, not open"
        state, names = verdict(pr.get("statusCheckRollup") or [])
        waited = clock() - t0
        if state == "failed":
            return 3, "not every check passed: " + "; ".join(names)
        if state == "passed" or (state == "none" and allow_no_checks and waited >= settle_s):
            break
        if state == "none" and waited >= settle_s and not allow_no_checks:
            return 2, "no checks were reported on its head (pass --allow-no-checks to merge anyway)"
        if waited >= timeout_s:
            return 2, "checks still running at the timeout: " + ", ".join(names)
        sleep(poll_s)
    head = pr["headRefOid"]
    r = gh(["pr", "merge", str(number), f"--{method}", "--match-head-commit", head]
           + (["--repo", repo] if repo else []))
    if r.returncode != 0:
        return 4, f"gh refused the merge: {(r.stderr or r.stdout).strip()[:300]}"
    try:
        after = _view(number, repo, gh)
    except RuntimeError as e:
        return 4, f"merged, but could not read it back: {e}"
    if after.get("state") != "MERGED":
        return 4, f"gh said it merged, but the pull request is {(after.get('state') or '').lower()}"
    oid = (after.get("mergeCommit") or {}).get("oid", "")
    return 0, f"merged {head[:12]} as {oid[:12]} ({method}), every check on it passed"


def _diff(number: str, repo: str | None, gh) -> str:
    r = gh(["pr", "diff", str(number)] + (["--repo", repo] if repo else []))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout


def dependency_bump_review(pr: dict, up_to: str, diff: str | None = None,
                           bots=dependency_bump.UPDATERS) -> dependency_bump.Verdict:
    """The gate `--dependency-bump` applies, over a pull request read with GATE_FIELDS."""
    return dependency_bump.review_pr(pr, up_to=up_to, diff=diff, bots=bots)


# ---------------------------------------------------------------------------
# Cascade: merge several pull requests, each only when it is green, bringing a
# pull request that falls behind its base up to date between merges.
# ---------------------------------------------------------------------------

#: what a look at a pull request's merge state reads
STATE_FIELDS = "number,state,mergeable,mergeStateStatus,headRefOid"

_REFUSAL = {
    "dirty": "refused: it has a merge conflict; resolve it, then run again",
    "draft": "refused: it is a draft; mark it ready, then run again",
    "closed": "refused: it is closed, not open",
}
_WAITING = {
    "behind": "still behind its base at the timeout",
    "waiting": "still waiting on its checks at the timeout",
}


def merge_state(number: str, repo: str | None, gh) -> dict:
    """The pull request's state, mergeability and merge-state status, as gh prints them."""
    return _view(number, repo, gh, STATE_FIELDS)


def classify(pr: dict) -> str:
    """Pure. One word for where a pull request stands, from its state and merge-state status:
    "merged", "closed", "clean" (green and up to date, ready to merge), "behind" (its base moved
    on, needs update-branch), "dirty" (a merge conflict), "draft", or "waiting" (its checks are
    still running, or a branch rule is not yet satisfied)."""
    state = (pr.get("state") or "").upper()
    if state == "MERGED":
        return "merged"
    if state and state != "OPEN":
        return "closed"
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return "dirty"
    status = (pr.get("mergeStateStatus") or "").upper()
    if status == "DIRTY":
        return "dirty"
    if status == "DRAFT":
        return "draft"
    if status == "BEHIND":
        return "behind"
    if status in ("CLEAN", "HAS_HOOKS"):
        return "clean"
    return "waiting"   # BLOCKED, UNSTABLE, UNKNOWN, or not computed yet


def _merge_sha(number: str, repo: str | None, gh) -> str:
    """The commit a merged pull request became, or "" when it cannot be read."""
    try:
        pr = _view(number, repo, gh, "mergeCommit")
    except RuntimeError:
        return ""
    return (pr.get("mergeCommit") or {}).get("oid", "") or ""


def _name_with_owner(repo: str | None, gh) -> str | None:
    """`repo` as given, or the current folder's repository when it is None."""
    if repo:
        return repo
    r = gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def cascade(numbers, repo: str | None = None, *, method: str = "merge", timeout_s: float = 1800,
            poll_s: float = 20, settle_s: float = 60, confirm_prod: bool = False,
            prod_environment: str | None = None, gh=None, env: dict | None = None,
            sleep=time.sleep, clock=time.monotonic, merge_one=None) -> tuple[int, str]:
    """Merge several pull requests, each only when every check on its head has passed.

    Each pass looks at every pull request still open and merges the first that is clean, through
    `merge_one` (the single-pull-request merge, so each merge still reads the checks in the same
    step it merges). A pull request that has fallen behind its base is brought up to date with
    `gh pr update-branch`, one at a time, its checks then re-run, and the loop waits. It never
    merges a pull request that is not clean. It stops when every one is merged, when one is refused
    (a conflict, a draft, a check that did not pass, or a merge gh would not do), or at the timeout
    with some still not ready.

    Under a branch rule that requires a pull request to be up to date, merging one makes the others
    fall behind; this updates and re-checks each in turn, so a set of green pull requests merges
    one after another.

    `confirm_prod`: after the last merge, wait for that commit's own checks to pass and for a
    deployment of it to reach success (`confirm_production`); `prod_environment` keeps only
    deployments whose environment name contains it.

    `timeout_s` is the budget for the next merge to happen, reset by each merge. Returns (exit code,
    one line per pull request): 0 every one merged, 2 some still not ready at the timeout, 3 one was
    refused, 7 merged but the production confirmation did not pass.
    """
    if method not in METHODS:
        return 2, f"method is one of {', '.join(METHODS)}"
    if gh is None:
        gh = lambda a: _gh(a, env)   # noqa: E731 - the default gh, with the caller's environment
    if merge_one is None:
        merge_one = merge_when_green
    numbers = [str(n) for n in numbers]
    outcome = {n: "waiting to be looked at" for n in numbers}
    remaining = list(numbers)
    kinds: dict = {}
    refused = False
    last_sha = ""
    deadline = clock() + timeout_s
    while remaining:
        try:
            states = {n: merge_state(n, repo, gh) for n in remaining}
        except RuntimeError as e:
            outcome[remaining[0]] = f"refused: could not read it ({e})"
            refused = True
            break
        kinds = {n: classify(states[n]) for n in remaining}

        for n in [m for m in remaining if kinds[m] == "merged"]:
            if not outcome[n].startswith("merged"):
                outcome[n] = "merged already (nothing to do)"
            remaining.remove(n)
        if not remaining:
            break

        clean = [n for n in remaining if kinds[n] == "clean"]
        if clean:
            n = clean[0]
            code, said = merge_one(n, repo, method=method, timeout_s=timeout_s, poll_s=poll_s,
                                   settle_s=settle_s, gh=gh, env=env, sleep=sleep, clock=clock)
            if code == 0:
                outcome[n] = said
                remaining.remove(n)
                if confirm_prod:
                    last_sha = _merge_sha(n, repo, gh) or last_sha
                deadline = clock() + timeout_s   # a merge is progress: a fresh budget for the next
                continue
            outcome[n] = "refused: " + said
            remaining.remove(n)
            refused = True
            break

        stuck = [n for n in remaining if kinds[n] in ("dirty", "draft", "closed")]
        if stuck:
            n = stuck[0]
            outcome[n] = _REFUSAL[kinds[n]]
            remaining.remove(n)
            refused = True
            break

        behind = [n for n in remaining if kinds[n] == "behind"]
        if behind:
            n = behind[0]
            r = gh(["pr", "update-branch", n] + (["--repo", repo] if repo else []))
            if r.returncode != 0:
                outcome[n] = f"refused: could not bring it up to date ({(r.stderr or r.stdout).strip()[:200]})"
                remaining.remove(n)
                refused = True
                break
            outcome[n] = "brought up to date, its checks are running"

        if clock() >= deadline:
            break
        sleep(poll_s)

    for n in remaining:
        outcome[n] = ("not reached, an earlier pull request was refused" if refused
                      else _WAITING.get(kinds.get(n, "waiting"), "still not ready at the timeout"))

    merged = sum(1 for n in numbers if outcome[n].startswith("merged"))
    code = 3 if refused else (0 if merged == len(numbers) else 2)
    lines = [f"#{n}  {outcome[n]}" for n in numbers]

    if confirm_prod and last_sha and not refused:
        ok, msg = confirm_production(_name_with_owner(repo, gh), last_sha,
                                     environment=prod_environment, gh=gh, env=env,
                                     timeout_s=timeout_s, poll_s=poll_s, sleep=sleep, clock=clock)
        lines.append(f"production  {msg}")
        if not ok:
            code = 7
    return code, "\n".join(lines)


def confirm_production(repo: str | None, sha: str, *, environment: str | None = None, gh=None,
                       env: dict | None = None, timeout_s: float = 1800, poll_s: float = 20,
                       sleep=time.sleep, clock=time.monotonic) -> tuple[bool, str]:
    """Wait for a commit's own checks to pass and for a deployment of it to reach success.

    `environment` keeps only deployments whose environment name contains it (any, when None).
    Returns (ok, one line). ok is False when a check did not pass, a deployment reported failure or
    error, or the timeout passed first."""
    if gh is None:
        gh = lambda a: _gh(a, env)   # noqa: E731
    if not repo:
        return False, "the repository is not known (pass --repo)"
    deadline = clock() + timeout_s
    ci_ok = dep_ok = False
    ci_word, dep_word = "running", "not created"
    while True:
        if not ci_ok:
            ci = _commit_checks(repo, sha, gh)
            if ci == "failed":
                return False, f"the merge commit {sha[:12]} did not pass its checks"
            ci_ok = ci == "passed"
            ci_word = "passed" if ci_ok else ci
        if not dep_ok:
            dep = _deployment_state(repo, sha, environment, gh)
            if dep in ("failure", "error"):
                return False, f"the production deployment of {sha[:12]} reported {dep}"
            dep_ok = dep == "success"
            dep_word = "succeeded" if dep_ok else dep
        if ci_ok and dep_ok:
            return True, f"the merge commit {sha[:12]} passed its checks and its deployment succeeded"
        if clock() >= deadline:
            return False, f"timed out (checks {ci_word}, deployment {dep_word})"
        sleep(poll_s)


def _commit_checks(repo: str, sha: str, gh) -> str:
    """"passed", "failed" or "waiting" for the check runs on a commit (waiting while none is in)."""
    r = gh(["api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"])
    if r.returncode != 0:
        return "waiting"
    runs = (json.loads(r.stdout or "{}").get("check_runs")) or []
    if not runs:
        return "waiting"
    if any((c.get("status") or "").lower() != "completed" for c in runs):
        return "waiting"
    bad = any((c.get("conclusion") or "").lower() not in ("success", "neutral", "skipped") for c in runs)
    return "failed" if bad else "passed"


def _deployment_state(repo: str, sha: str, environment: str | None, gh) -> str:
    """The latest status of the newest deployment of a commit from GitHub's Deployments: "success",
    "failure", "error", or "waiting" (none created yet, or no status yet). `environment` keeps only
    deployments whose environment name contains it."""
    r = gh(["api", f"repos/{repo}/deployments?sha={sha}&per_page=30"])
    if r.returncode != 0:
        return "waiting"
    deps = json.loads(r.stdout or "[]")
    if environment:
        deps = [d for d in deps if environment.lower() in (d.get("environment") or "").lower()]
    if not deps:
        return "waiting"
    did = deps[0].get("id")
    r2 = gh(["api", f"repos/{repo}/deployments/{did}/statuses?per_page=1"])
    if r2.returncode != 0:
        return "waiting"
    sts = json.loads(r2.stdout or "[]")
    return (sts[0].get("state") or "waiting").lower() if sts else "waiting"
