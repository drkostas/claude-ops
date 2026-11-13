"""merge.py — merge a GitHub pull request only when every check on its current head has passed.

Waiting for checks and merging as two separate steps lets a pull request be merged while a check
is failing: the wait reads one moment, the merge happens at another, and a shortened log line can
hide a red job. This does both in one step through the gh CLI. It reads the checks from gh's JSON
(never a printed table), refuses on any check that did not pass and on a pull request with no
checks at all, and merges with --match-head-commit, so a push between the reading and the merge
makes GitHub refuse the merge instead of merging code nobody checked.

It merges only the head the pull request had when the command started. A push while it waits for the
checks ends it with exit 8 and nothing merged, because the new head is not what the person who typed
the command looked at. `--follow-head` merges whatever head is green at the end instead, as before.

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
pass, 4 gh refused the merge, 5 the pull request is not open, 6 not the dependency update asked for,
8 its head moved while it waited. The cascade uses 0 all merged, 2 some still not ready at the
timeout, 3 one refused (a moved head among the reasons), 7 the production confirmation did not pass.
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
#: a workflow run that has not finished
RUNNING = {"queued", "in_progress", "requested", "waiting", "pending", "action_required"}
METHODS = ("merge", "squash", "rebase", "auto")
#: what a look at the pull request reads; the dependency gate reads the rest
FIELDS = "headRefOid,state,statusCheckRollup,mergeCommit,baseRefName"
GATE_FIELDS = FIELDS + ",title,body,author,files,url"


def _gh(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, env=env)


def _one(check: dict) -> tuple[str, str, str]:
    """Pure. (name, "passed" | "waiting" | "failed", the result in one word) for one rollup entry.
    Check runs carry status and conclusion, commit statuses carry state."""
    name = check.get("name") or check.get("context") or "?"
    if "state" in check and "conclusion" not in check:            # a commit status
        state = (check.get("state") or "").upper()
        if state in WAITING:
            return name, "waiting", state.lower()
        return name, "passed" if state == "SUCCESS" else "failed", state.lower()
    if (check.get("status") or "").upper() != "COMPLETED":
        return name, "waiting", ((check.get("status") or "").lower() or "pending")
    result = (check.get("conclusion") or "").upper()
    return name, "passed" if result in PASSED else "failed", (result.lower() or "none")


def verdict(rollup: list[dict]) -> tuple[str, list[str]]:
    """Pure. ("passed" | "waiting" | "failed" | "none", the checks that hold it up), from gh's
    statusCheckRollup. A check that did not pass is named with its result, so a caller can say
    which one and why.

    A conclusion this function does not count as passed stops the merge, `neutral` included. Some
    scanners report a real finding that way, and a merge is the one moment where the cautious
    reading is the right one. The message a caller builds carries the check's own words, so a
    finding can be told apart from a check that had nothing to do."""
    if not rollup:
        return "none", []
    waiting, bad = [], []
    for check in rollup:
        name, state, result = _one(check)
        if state == "waiting":
            waiting.append(name)
        elif state == "failed":
            bad.append(f"{name}: {result}")
    if bad:
        return "failed", bad
    if waiting:
        return "waiting", waiting
    return "passed", []


def failed_names(rollup: list[dict]) -> list[str]:
    """Pure. The bare names of the checks that did not pass."""
    return [name for check in rollup for name, state, _ in [_one(check)] if state == "failed"]


def still_running(rollup: list[dict]) -> list[str]:
    """Pure. The names of the checks whose result is not in yet."""
    return [name for check in rollup for name, state, _ in [_one(check)] if state == "waiting"]


def check_words(repo: str | None, sha: str, names, gh) -> dict[str, str]:
    """What each of `names` said about itself, as one line per check.

    A check run reports a title and a summary of its own, and gh's statusCheckRollup carries
    neither, so this reads them from the commit's check runs. They are the difference between
    "the scan is neutral" and the scan's own "1 secret uncovered". Returns an empty mapping when
    they cannot be read, since the words only make a message better and must never replace it."""
    wanted = {str(n) for n in names}
    if not (sha and wanted):
        return {}
    where = _name_with_owner(repo, gh)
    if not where:
        return {}
    r = gh(["api", f"repos/{where}/commits/{sha}/check-runs", "--paginate"])
    if r.returncode != 0:
        return {}
    try:
        runs = json.loads(r.stdout).get("check_runs") or []
    except (ValueError, AttributeError):
        return {}
    out = {}
    for run_ in runs:
        name = run_.get("name")
        if name not in wanted or name in out:
            continue
        output = run_.get("output") or {}
        title = (output.get("title") or "").strip()
        summary = (output.get("summary") or "").strip()
        first = next((line.strip(" #").strip() for line in summary.splitlines() if line.strip()), "")
        said = ". ".join(part for part in (title, first) if part)
        if said:
            out[name] = said
    return out


def required_checks(repo: str | None, base: str, gh) -> list[str] | None:
    """The check names the base branch requires, or None when they cannot be read.

    A branch without protection, and a token without the right to read it, both answer nothing, and
    neither means the branch requires nothing. None says "unknown", so a caller gates on the runs
    in flight instead of trusting a short list."""
    where = _name_with_owner(repo, gh)
    if not (where and base):
        return None
    r = gh(["api", f"repos/{where}/branches/{base}/protection/required_status_checks"])
    if r.returncode != 0:
        return None
    try:
        got = json.loads(r.stdout)
    except ValueError:
        return None
    names = [c.get("context") for c in (got.get("checks") or []) if c.get("context")]
    return names or list(got.get("contexts") or [])


def runs_in_flight(repo: str | None, sha: str, gh) -> list[str]:
    """The workflow runs on this head that have not finished, as "<name> (<status>)".

    This is the check that does not depend on a repository having protection rules. A run exists
    from the moment the platform creates it, which is a little before its checks appear on the pull
    request, so a run listed here is work the pull request has not reported yet."""
    where = _name_with_owner(repo, gh)
    if not (where and sha):
        return []
    r = gh(["api", f"repos/{where}/actions/runs?head_sha={sha}&per_page=100"])
    if r.returncode != 0:
        return []
    try:
        runs = json.loads(r.stdout).get("workflow_runs") or []
    except (ValueError, AttributeError):
        return []
    out = []
    for run_ in runs:
        status = (run_.get("status") or "").lower()
        if status in RUNNING:
            out.append(f"{run_.get('name') or run_.get('display_title') or 'a workflow'} ({status})")
    return out


def gate_blockers(rollup: list[dict], repo: str | None, base: str, sha: str, gh) -> list[str]:
    """Why this head is not ready to merge although every check it reported has passed.

    A pull request reports the checks that exist, and nothing about the ones still being created.
    One fast check that goes green on a new head therefore reads as "everything passed" for the few
    seconds before the slow one appears. Two readings close that window. The runs on the head name
    work that has not finished, whether or not its checks exist yet, and the base branch's required
    checks name work that must report at all."""
    blockers = list(runs_in_flight(repo, sha, gh))
    reported = {c.get("name") or c.get("context") for c in rollup}
    for name in required_checks(repo, base, gh) or []:
        if name not in reported:
            blockers.append(f"{name} is required on {base} and has not reported")
    return blockers


def why_not_merged(rollup: list[dict], repo: str | None, sha: str, gh) -> str:
    """The reason a merge stopped, in the checks' own words where they gave any, and naming the
    checks that had not finished, so a caller is not left thinking the finished ones are the whole
    story."""
    _, named = verdict(rollup)
    words = check_words(repo, sha, failed_names(rollup), gh)
    parts = []
    for item in named:
        name = item.split(":")[0]
        said = words.get(name)
        parts.append(f"{item} ({said})" if said else item)
    message = "not every check passed: " + "; ".join(parts)
    running = still_running(rollup)
    if running:
        message += f". Still running, so it was not waited out: {', '.join(running)}"
    return message


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
                     dependency_bots=None, expect_head: str | None = None, follow_head: bool = False,
                     gh=None, env: dict | None = None, sleep=time.sleep,
                     clock=time.monotonic) -> tuple[int, str]:
    """Wait for every check on the pull request's head, then merge it if all passed.

    Returns (exit code, what happened). A pull request reports the checks that exist, so a head
    whose fast check has gone green while its slow one is still being created reads as "everything
    passed". The gate therefore asks for more than the rollup. No workflow run on the head may be
    unfinished, every check the base branch requires must have reported where that list can be
    read, and the whole picture must still hold `settle_s` later, because a run is created a moment
    after a push. The cost is one `settle_s` before a merge, and it is what stops an untested head
    reaching the base branch.

    `settle_s` is also how long "no checks yet" counts as waiting. `dependency_bump` ("patch" or
    "minor") merges only a bot's update of lockfiles and manifests within that level, read once
    before any waiting.

    It merges only `expect_head`, or the head of its first look when that is None: a head that moves
    while it waits returns 8 with nothing merged. `follow_head` merges the head green at the end."""
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
    ready_at = None
    pinned = None if follow_head else expect_head
    while True:
        try:
            pr = _view(number, repo, gh)
        except RuntimeError as e:
            return 4, f"could not read the pull request: {e}"
        if pr.get("state") != "OPEN":
            return 5, f"the pull request is {(pr.get('state') or 'unknown').lower()}, not open"
        head = pr.get("headRefOid") or ""
        if not follow_head:
            pinned = pinned or head
            if head and head != pinned:
                return 8, (f"its head moved from {pinned[:12]} to {head[:12]} while this waited, so nothing was "
                           "merged. Run it again to merge the new head")
        rollup = pr.get("statusCheckRollup") or []
        state, names = verdict(rollup)
        waited = clock() - t0
        if state == "failed":
            return 3, why_not_merged(rollup, repo, pr.get("headRefOid") or "", gh)
        if state == "passed" or (state == "none" and allow_no_checks and waited >= settle_s):
            blockers = gate_blockers(rollup, repo, pr.get("baseRefName") or "",
                                     pr.get("headRefOid") or "", gh)
            if blockers:
                ready_at = None
                if waited >= timeout_s:
                    return 2, "not ready at the timeout: " + "; ".join(blockers)
                sleep(poll_s)
                continue
            if ready_at is None:
                ready_at = clock()         # read it again after settle_s, in case more is coming
            if clock() - ready_at >= settle_s:
                break
            if waited >= timeout_s:
                return 2, "the checks passed but had not settled at the timeout"
            sleep(poll_s)
            continue
        ready_at = None
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


def first_parent(repo: str | None, sha: str, gh) -> str:
    """The first parent of commit `sha`, or "" when it cannot be read."""
    name = _name_with_owner(repo, gh)
    if not name or not sha:
        return ""
    r = gh(["api", f"repos/{name}/commits/{sha}", "--jq", ".parents[0].sha"])
    return r.stdout.strip() if r.returncode == 0 else ""


def cascade(numbers, repo: str | None = None, *, method: str = "merge", timeout_s: float = 1800,
            poll_s: float = 20, settle_s: float = 60, confirm_prod: bool = False,
            prod_environment: str | None = None, follow_head: bool = False, gh=None,
            env: dict | None = None, sleep=time.sleep, clock=time.monotonic,
            merge_one=None) -> tuple[int, str]:
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

    Each pull request merges only the head it had at the first look, or the merge commit its own
    `update-branch` made on top of that head (its first parent). A head that moved any other way is
    refused, unless `follow_head`.

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
    pins: dict = {}            # the head each one may merge
    updated: set = set()       # the ones this brought up to date since their pin
    deadline = clock() + timeout_s
    while remaining:
        try:
            states = {n: merge_state(n, repo, gh) for n in remaining}
        except RuntimeError as e:
            outcome[remaining[0]] = f"refused: could not read it ({e})"
            refused = True
            break
        kinds = {n: classify(states[n]) for n in remaining}

        moved = None
        for n in remaining:
            head = states[n].get("headRefOid") or ""
            if follow_head or not head or kinds[n] == "merged":
                continue
            if n not in pins:
                pins[n] = head
            elif head != pins[n]:
                if n in updated and first_parent(repo, head, gh) == pins[n]:
                    pins[n] = head
                    updated.discard(n)
                else:
                    moved = (n, pins[n], head)
                    break
        if moved:
            n, was, now = moved
            outcome[n] = (f"refused: its head moved from {was[:12]} to {now[:12]} while this waited, and not by "
                          "its own update-branch. Run it again to merge the new head")
            remaining.remove(n)
            refused = True
            break

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
                                   settle_s=settle_s, expect_head=pins.get(n), follow_head=follow_head,
                                   gh=gh, env=env, sleep=sleep, clock=clock)
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
            updated.add(n)

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
