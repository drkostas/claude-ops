"""rescan.py — run a check again when it says it could not complete, instead of reading it as a finding.

Some scanning apps end a check run with the conclusion `neutral` both when they found something and when
they could not finish (their service timed out, or a commit they asked GitHub for was not there yet). The
two differ only in the run's own title. A gate that reads every neutral run as red then stops a merge on a
scan that never ran, and whoever reads its message is told about a finding that does not exist. GitHub
refuses a re-request of such a run from anyone but the app that made it, and most apps scan again when the
pull request is closed and opened again.

`rescan(number)` reads the checks on the pull request's head. When every check that did not pass is a
completed neutral run whose title says it could not complete (INCOMPLETE, or the caller's pattern), it
closes the pull request and opens it again, once, waits for a new run of each such check, and judges the
new runs. A neutral run with any other title is a finding, and nothing is closed for it. Before closing it
lists the open pull requests whose base is the head branch. Closing a pull request leaves its branch, so
they keep their base, and it reads them again after the reopen and says so if any of them changed.

    claude-ops rescan 123 [--repo owner/name] [--head <sha>] [--timeout 30] [--poll 20] [--title <regex>]

Exit codes: 0 the new runs passed, 1 nothing to run again (no check failed, or a check that did not pass is
not a run that could not complete), 2 a new run was not done at the timeout, 3 a new run did not pass (a
finding included), 4 gh could not read, close or reopen the pull request, or it is not open, 5 a new run
could not complete either, 8 the pull request's head is not the --head given.
"""
from __future__ import annotations

import json
import re
import time

from . import merge

#: a check run's title that says the app could not finish, matched without regard to case
INCOMPLETE = r"\bcould not complete\b"
#: a new run counts as passed with one of these conclusions
PASSED = {"success", "skipped"}
FIELDS = "headRefOid,headRefName,state,statusCheckRollup"


def runs_on(repo: str, sha: str, gh) -> list[dict] | None:
    """The newest check run of each name on commit `sha`, as {id, name, status, conclusion, title}, or None
    when gh cannot read them. GitHub lists only the newest run of a name unless asked for all."""
    r = gh(["api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"])
    if r.returncode != 0:
        return None
    try:
        runs = json.loads(r.stdout or "{}").get("check_runs") or []
    except (ValueError, AttributeError):
        return None
    return [{"id": run.get("id"), "name": run.get("name") or "?", "status": (run.get("status") or "").lower(),
             "conclusion": (run.get("conclusion") or "").lower(),
             "title": ((run.get("output") or {}).get("title") or "").strip()} for run in runs]


def incomplete(runs: list[dict], pattern: str = INCOMPLETE) -> list[dict]:
    """Pure. The completed neutral runs whose title says the app could not complete. A neutral run with any
    other title is the app's finding and is not one of them."""
    said = re.compile(pattern, re.I)
    return [run for run in runs if run["status"] == "completed" and run["conclusion"] == "neutral"
            and said.search(run["title"])]


def plan(rollup: list[dict], runs: list[dict], pattern: str = INCOMPLETE) -> tuple[list[dict] | None, str]:
    """Pure. (the runs to have made again, "") when every check that did not pass is a run that could not
    complete, else (None, why not). A pull request rollup also holds commit statuses, which a reopen does
    not change, so any other check that did not pass leaves nothing to run again."""
    failed = merge.failed_names(rollup)
    if not failed:
        return None, "no check on its head failed"
    todo = [run for run in incomplete(runs, pattern) if run["name"] in failed]
    others = [name for name in failed if name not in {run["name"] for run in todo}]
    if others:
        return None, f"{', '.join(others)} did not pass for another reason than an incomplete run"
    return todo, ""


def based_on(branch: str, repo: str, gh) -> list[int] | None:
    """The open pull requests whose base is `branch`, by number, or None when gh cannot list them."""
    r = gh(["pr", "list", "--repo", repo, "--state", "open", "--base", branch, "--json", "number"])
    if r.returncode != 0:
        return None
    try:
        return sorted(int(p["number"]) for p in json.loads(r.stdout or "[]"))
    except (ValueError, TypeError, KeyError):
        return None


def _ids(runs: list[dict]) -> str:
    return ", ".join(f"{run['name']} {run['id']}" for run in runs)


def _children_words(before: list[int] | None, after: list[int] | None) -> str:
    if before is None or after is None:
        return "the pull requests based on its branch could not be listed"
    if not before:
        return "no open pull request is based on its branch"
    gone = [n for n in before if n not in after]
    if gone:
        return (f"#{', #'.join(map(str, gone))} {'was' if len(gone) == 1 else 'were'} based on its branch and "
                f"{'is' if len(gone) == 1 else 'are'} no longer open on it after the reopen")
    return f"#{', #'.join(map(str, before))} {'is' if len(before) == 1 else 'are'} based on its branch and still open on it"


def rescan(number: str, repo: str | None = None, *, head: str | None = None, pattern: str = INCOMPLETE,
           timeout_s: float = 1800, poll_s: float = 20, gh=None, env: dict | None = None, sleep=time.sleep,
           clock=time.monotonic) -> tuple[int, str]:
    """Close and reopen pull request `number` once when every check that did not pass on its head is a run
    that could not complete, and judge the new runs. (exit code, what happened), the codes in the module's
    docstring. Nothing is closed for a finding, a failure or a check still running."""
    if gh is None:
        gh = lambda a: merge._gh(a, env)   # noqa: E731 - the default gh, with the caller's environment
    try:
        pr = merge._view(number, repo, gh, FIELDS)
    except RuntimeError as e:
        return 4, f"could not read the pull request: {e}"
    if pr.get("state") != "OPEN":
        return 4, f"#{number} is {(pr.get('state') or 'unknown').lower()}, not open"
    sha = pr.get("headRefOid") or ""
    if head and sha != head:
        return 8, f"#{number}'s head is {sha[:12]}, not {head[:12]}, so nothing was run again"
    where = merge._name_with_owner(repo, gh)
    runs = runs_on(where, sha, gh) if where else None
    if runs is None:
        return 4, f"could not read the check runs on {sha[:12]}"
    todo, why = plan(pr.get("statusCheckRollup") or [], runs, pattern)
    if todo is None:
        return 1, f"#{number}: nothing to run again, {why}"
    old = {run["id"] for run in todo}
    branch = pr.get("headRefName") or ""
    before = based_on(branch, where, gh) if branch else []
    closed = gh(["pr", "close", str(number), "--repo", where])
    if closed.returncode != 0:
        return 4, f"#{number}: could not close it to run {_ids(todo)} again: {(closed.stderr or closed.stdout).strip()[:200]}"
    opened = gh(["pr", "reopen", str(number), "--repo", where])
    if opened.returncode != 0:
        return 4, (f"#{number} WAS CLOSED and could not be reopened, open it again by hand: "
                   f"{(opened.stderr or opened.stdout).strip()[:200]}")
    after = based_on(branch, where, gh) if branch else []
    lead = (f"#{number} closed and reopened once, since {_ids(todo)} could not complete on {sha[:12]} "
            f"({_children_words(before, after)})")
    t0, new = clock(), {}
    while True:
        now = runs_on(where, sha, gh) or []
        for run in todo:
            fresh = [r for r in now if r["name"] == run["name"] and r["id"] not in old]
            if fresh:
                new[run["name"]] = fresh[0]
        done = [r for r in new.values() if r["status"] == "completed"]
        if len(done) == len(todo):
            break
        if clock() - t0 >= timeout_s:
            started = _ids(list(new.values())) or "no new run started"
            return 2, f"{lead}, and the new runs were not done at the timeout ({started})"
        sleep(poll_s)
    runs_after = list(new.values())
    if incomplete(runs_after, pattern):
        return 5, f"{lead}, and {_ids(incomplete(runs_after, pattern))} could not complete either, so nothing was found"
    bad = [r for r in runs_after if r["conclusion"] not in PASSED]
    if bad:
        return 3, f"{lead}, and " + ", ".join(f"{r['name']} {r['id']} is {r['conclusion']}: {r['title']}" for r in bad)
    passed = ", ".join(f"{r['name']} {r['id']} passed" for r in runs_after)
    return 0, f"{lead}, and {passed}"
