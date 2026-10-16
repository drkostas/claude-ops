"""merge.py — merge a GitHub pull request only when every check on its current head has passed.

Waiting for checks and merging as two separate steps lets a pull request be merged while a check
is failing: the wait reads one moment, the merge happens at another, and a shortened log line can
hide a red job. This does both in one step through the gh CLI. It reads the checks from gh's JSON
(never a printed table), refuses on any check that did not pass and on a pull request with no
checks at all, and merges with --match-head-commit, so a push between the reading and the merge
makes GitHub refuse the merge instead of merging code nobody checked.

    claude-ops merge 123                         # the repository of the current folder
    claude-ops merge 123 --repo owner/name --method squash --timeout 45

Exit codes: 0 merged, 2 checks still running at the timeout (or none reported), 3 a check did not
pass, 4 gh refused the merge, 5 the pull request is not open.
"""
from __future__ import annotations

import json
import subprocess
import time

#: a check counts as passed only with one of these conclusions
PASSED = {"SUCCESS", "SKIPPED"}
#: a check whose result is not in yet
WAITING = {"", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
METHODS = ("merge", "squash", "rebase")


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


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


def _view(number: str, repo: str | None, gh) -> dict:
    args = ["pr", "view", str(number), "--json", "headRefOid,state,statusCheckRollup,mergeCommit"]
    r = gh(args + (["--repo", repo] if repo else []))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return json.loads(r.stdout)


def merge_when_green(number: str, repo: str | None = None, *, method: str = "merge",
                     timeout_s: float = 1800, poll_s: float = 20, settle_s: float = 60,
                     allow_no_checks: bool = False, gh=_gh, sleep=time.sleep,
                     clock=time.monotonic) -> tuple[int, str]:
    """Wait for every check on the pull request's head, then merge it if all passed.
    Returns (exit code, what happened). `settle_s` is how long "no checks yet" counts as waiting,
    since checks register a little after a push."""
    if method not in METHODS:
        return 2, f"method is one of {', '.join(METHODS)}"
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
    return 0, f"merged {head[:12]} as {oid[:12]}, every check on it passed"
