"""ci_run — wait for one GitHub Actions run of a pull request, re-run it only when its runner lost a
job before the job's first step, then merge the pull request and wait until the service runs it.

    claude-ops ci-run <run> --pr <number> --budget <minutes>                    # wait, then merge
    claude-ops ci-run <run> --pr 12 --repo owner/name --budget 75 --rerun-starved 1 \\
        --gate "./room-for-a-suite.sh" --serving "./what-is-deployed.sh" --deploy-budget 20
    claude-ops ci-run <run> --pr 12 --budget 75 --no-merge                      # wait and judge only

WHY. On a self-hosted runner a job can fail in two ways that look the same from the pull request.
Either its code failed, or the runner lost the job's worker before its first step (the machine was
too busy to start it in time). The first fails the same way when it is run again. The second is
worth one more run. Waiters written by hand for one run at a time get four things wrong, and each
rule below is one of them.

1. EVERY WAIT IS A DEADLINE FROM THE CLOCK. The wait for a run ends at now plus the job's budget plus
   the time the run has already taken, never after a count of loops. Eighty looks 45 s apart is one
   hour, and a 75-minute job that was 4 minutes in when its waiter started was given up while it ran.
2. THE SHAPE IS PROVEN BEFORE A RUN IS SPENT. A job that failed with no steps never started, so it is
   starved and a new attempt is worth it. A job that failed with steps failed in its own code, and it
   is never run again blindly: the command stops and names the steps that failed.
3. THE RE-RUN FORM FOLLOWS THE SHAPE. `gh run rerun --failed` refuses a run that holds a cancelled
   job, so such a run takes the full `gh run rerun`. Both print nothing when they work, so the run's
   attempt number going up is the only proof that the new attempt started.
4. A GATE IS READ TWICE BEFORE A RUN IS SPENT. `--gate` is a command that exits 0 when the machine
   has room for the job. It must pass twice, `--gate-gap` seconds apart (45), because one reading can
   fall in a moment of room that another job takes a few seconds later.

Then the pull request is merged with `merge.merge_when_green` (every check on the head the run was
on has passed, and a push in between refuses the merge). With `--serving`, the command then waits
until the caller's command prints the merge commit (as a prefix of at least 7 characters), because
what proves a deploy is what the service runs, an image tag or a version endpoint, and a fresh
revision name says nothing about which commit it holds.

Exit codes: 0 done (merged, and serving it with --serving; green with --no-merge), 2 a deadline
passed, 3 a job failed in its own code, or the run cannot be re-run here (no --rerun-starved left,
or only cancelled jobs), 4 gh refused something or a re-run did not start, 5 the pull request is not
open or the run is not on its head, 7 the service did not show the merge commit by its deadline, 8
the pull request's head moved while it waited (nothing merged). Other merge codes are merge's own.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import merge

#: how long apart the two readings of a gate are, in seconds
GATE_GAP_S = 45.0
#: how often a run, a gate or the service is read again, in seconds
POLL_S = 30.0
#: how long a re-run has to show a new attempt number, in seconds
ATTEMPT_WAIT_S = 300.0
#: what a look at the run reads
RUN_FIELDS = "attempt,status,conclusion,headSha,createdAt,jobs"
#: a job with one of these conclusions did not fail
PASSED = {"success", "skipped", "neutral"}
#: a job with one of these conclusions failed, in its code when it has steps, starved when it has none
FAILED = {"failure", "timed_out", "startup_failure"}
#: the shortest prefix of the merge commit that counts as naming it in what the service prints
SHA_PREFIX = 7


@dataclass
class Shape:
    """What a finished attempt of a run looks like. `kind` is "running", "green", "failed" (a job
    failed in its own code), "starved" (a job lost before its first step, with no job failed in its
    code) or "cancelled" (only cancelled jobs besides the green ones)."""
    kind: str
    failed: list[str] = field(default_factory=list)
    starved: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    running: list[str] = field(default_factory=list)

    def rerun_args(self, run: str, repo: str) -> list[str]:
        """gh's arguments for a new attempt: only the failed jobs, unless a job was cancelled,
        which `--failed` refuses."""
        return ["run", "rerun", str(run), "--repo", repo] + ([] if self.cancelled else ["--failed"])

    def words(self) -> str:
        parts = []
        for name, items in (("failed in its code", self.failed), ("starved", self.starved),
                            ("cancelled", self.cancelled), ("still running", self.running)):
            if items:
                parts.append(f"{name}: {', '.join(items)}")
        return "; ".join(parts) or "every job passed"


def shape(jobs: list[dict]) -> Shape:
    """Pure. The shape of one attempt from gh's `jobs` (each with status, conclusion and steps)."""
    s = Shape("running")
    for j in jobs:
        name = j.get("name") or "?"
        steps = j.get("steps") or []
        if (j.get("status") or "").lower() != "completed":
            s.running.append(name)
            continue
        c = (j.get("conclusion") or "").lower()
        if c in PASSED:
            continue
        if c == "cancelled":
            s.cancelled.append(name)
        elif c in FAILED and not steps:
            s.starved.append(name)
        else:
            bad = [st.get("name") or "?" for st in steps
                   if (st.get("conclusion") or "").lower() not in PASSED | {""}]
            s.failed.append(f"{name} ({', '.join(bad) or c or 'no conclusion'})")
    if s.running:
        s.kind = "running"
    elif s.failed:
        s.kind = "failed"
    elif s.starved:
        s.kind = "starved"
    elif s.cancelled:
        s.kind = "cancelled"
    else:
        s.kind = "green"
    return s


def deadline(budget_s: float, created: str | None, now_wall: float, now: float) -> float:
    """Pure. When the wait for a run ends, on the `now` clock: the job's budget plus the time the run
    has already taken, from now (rule 1)."""
    taken = 0.0
    if created:
        try:
            at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            taken = max(0.0, now_wall - at.astimezone(timezone.utc).timestamp())
        except ValueError:
            taken = 0.0
    return now + budget_s + taken


def names_commit(text: str, sha: str, at_least: int = SHA_PREFIX) -> bool:
    """Pure. Whether `text` holds a hexadecimal word of at least `at_least` characters that begins
    `sha` (a tag, a version line, a label)."""
    sha = sha.lower()
    return any(len(w) >= at_least and sha.startswith(w)
               for w in re.findall(r"(?<![0-9a-f])[0-9a-f]+(?![0-9a-f])", text.lower()))


def _shell(cmd: str, timeout_s: float = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return 124, f"did not answer within {timeout_s:.0f} s"
    return r.returncode, (r.stdout + r.stderr).strip()


def view_run(run: str, repo: str, gh) -> dict:
    r = gh(["run", "view", str(run), "--repo", repo, "--json", RUN_FIELDS])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return json.loads(r.stdout)


class Waiter:
    """One run of `see_through`, with its clocks, its gh and its shell, so tests can give their own."""

    def __init__(self, run: str, repo: str, *, gh, sh=_shell, clock=time.monotonic, wall=time.time,
                 sleep=time.sleep, say=None, poll_s: float = POLL_S):
        self.run, self.repo, self.gh, self.sh = str(run), repo, gh, sh
        self.clock, self.wall, self.sleep, self.poll_s = clock, wall, sleep, poll_s
        self.say = say or (lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True))

    def until_done(self, budget_s: float) -> tuple[dict | None, str]:
        """The run once it completed, or (None, why) at the deadline or when gh cannot read it."""
        try:
            got = view_run(self.run, self.repo, self.gh)
        except RuntimeError as e:
            return None, f"could not read run {self.run}: {e}"
        end = deadline(budget_s, got.get("createdAt"), self.wall(), self.clock())
        self.say(f"run {self.run} attempt {got.get('attempt')}: waiting until it completes, "
                 f"at most {(end - self.clock()) / 60:.0f} minutes from now")
        while True:
            if (got.get("status") or "").lower() == "completed":
                return got, ""
            if self.clock() >= end:
                return None, (f"run {self.run} was still {got.get('status')} at its deadline "
                              f"({shape(got.get('jobs') or []).words()})")
            self.sleep(self.poll_s)
            try:
                got = view_run(self.run, self.repo, self.gh)
            except RuntimeError as e:
                return None, f"could not read run {self.run}: {e}"

    def gate_open(self, gate: str, gap_s: float, budget_s: float) -> tuple[bool, str]:
        """Whether the gate passed twice, gap_s apart, before the budget ran out (rule 4)."""
        end = self.clock() + budget_s
        last = ""
        while True:
            rc, out = self.sh(gate)
            if rc == 0:
                self.sleep(gap_s)
                rc, out = self.sh(gate)
                if rc == 0:
                    return True, ""
                last = f"passed once, then not {gap_s:.0f} s later: {out.splitlines()[-1] if out else rc}"
            else:
                last = out.splitlines()[-1] if out else f"exit {rc}"
            if self.clock() >= end:
                return False, f"the gate stayed shut to its deadline ({last})"
            self.say(f"the gate holds the re-run: {last}")
            self.sleep(self.poll_s)

    def new_attempt(self, s: Shape, was: int) -> tuple[bool, str]:
        """Start a new attempt and wait for the attempt number to go up (rule 3)."""
        args = s.rerun_args(self.run, self.repo)
        r = self.gh(args)
        if r.returncode != 0:
            return False, f"gh refused the re-run ({' '.join(args[:3])}): {(r.stderr or r.stdout).strip()[:200]}"
        end = self.clock() + ATTEMPT_WAIT_S
        while True:
            try:
                now = int(view_run(self.run, self.repo, self.gh).get("attempt") or 0)
            except RuntimeError:
                now = 0
            if now > was:
                return True, f"attempt {now} started ({'every job' if s.cancelled else 'the failed jobs'})"
            if self.clock() >= end:
                return False, (f"gh said nothing and the attempt stayed {was} for "
                               f"{ATTEMPT_WAIT_S / 60:.0f} minutes, so no new attempt started")
            self.sleep(min(self.poll_s, 20.0))

    def serving(self, cmd: str, sha: str, budget_s: float) -> tuple[bool, str]:
        """Whether the caller's command printed the merge commit before the budget ran out."""
        end = self.clock() + budget_s
        while True:
            rc, out = self.sh(cmd)
            if rc == 0 and names_commit(out, sha):
                return True, f"the service runs {sha[:12]}"
            last = (out.splitlines()[-1] if out else "") or f"exit {rc}"
            if self.clock() >= end:
                return False, f"the service did not show {sha[:12]} by its deadline (last: {last[:160]})"
            self.sleep(self.poll_s)


def see_through(run: str, pr: str, repo: str | None = None, *, budget_s: float, head: str | None = None,
                reruns: int = 0, gate: str | None = None, gate_gap_s: float = GATE_GAP_S,
                no_merge: bool = False, method: str = "merge", merge_timeout_s: float = 1800,
                serving: str | None = None, deploy_budget_s: float = 1200, gh=None, env=None,
                sh=_shell, clock=time.monotonic, wall=time.time, sleep=time.sleep, say=None,
                poll_s: float = POLL_S, merger=None) -> tuple[int, str]:
    """Wait for run `run` of pull request `pr`, re-run it at most `reruns` times when a job was
    starved, merge the pull request when every job passed, and with `serving` wait until the
    service runs the merge commit. Returns (exit code, what happened). `merger` stands in for
    merge.merge_when_green (a test's)."""
    if gh is None:
        gh = lambda a: merge._gh(a, env)   # noqa: E731 - gh with the caller's environment
    repo = merge._name_with_owner(repo, gh)
    if not repo:
        return 4, "could not tell which repository this is (pass --repo owner/name)"
    w = Waiter(run, repo, gh=gh, sh=sh, clock=clock, wall=wall, sleep=sleep, say=say, poll_s=poll_s)
    try:
        p = merge.pull_request(pr, repo, fields="state,headRefOid", gh=gh)
        r = view_run(run, repo, gh)
    except RuntimeError as e:
        return 4, f"could not read the pull request or the run: {e}"
    if p.get("state") != "OPEN":
        return 5, f"pull request {pr} is {(p.get('state') or 'unknown').lower()}, not open"
    pr_head, run_head = p.get("headRefOid") or "", r.get("headSha") or ""
    if head and not pr_head.startswith(head.lower()):
        return 5, f"pull request {pr}'s head is {pr_head[:12]}, not {head}"
    if run_head != pr_head:
        return 5, f"run {run} is on {run_head[:12]}, and pull request {pr}'s head is {pr_head[:12]}"
    left = reruns
    while True:
        got, why = w.until_done(budget_s)
        if got is None:
            return (4 if why.startswith("could not") else 2), why
        s = shape(got.get("jobs") or [])
        attempt = int(got.get("attempt") or 0)
        w.say(f"attempt {attempt} finished: {s.words()}")
        if s.kind == "green":
            break
        if s.kind == "failed":
            return 3, f"a job failed in its own code, so it is not run again: {s.words()}"
        if s.kind == "cancelled":
            return 3, (f"only cancelled jobs failed, and nothing shows the runner lost one: {s.words()}. "
                       f"Re-run it by hand if that was meant")
        if left <= 0:
            return 3, (f"a job was lost before its first step ({', '.join(s.starved)}), and no re-run is "
                       f"left (--rerun-starved)")
        if gate:
            ok, why = w.gate_open(gate, gate_gap_s, budget_s)
            if not ok:
                return 2, why
        ok, why = w.new_attempt(s, attempt)
        if not ok:
            return 4, why
        left -= 1
        w.say(why)
    if no_merge:
        return 0, f"every job of run {run} passed on {pr_head[:12]}"
    code, said = (merger or merge.merge_when_green)(pr, repo, method=method, timeout_s=merge_timeout_s,
                                                    expect_head=pr_head, gh=gh, sleep=sleep, clock=clock)
    if code != 0:
        return code, said
    sha = merge._merge_sha(pr, repo, gh)
    if not serving:
        return 0, said          # the result line says it, so it is not said twice
    w.say(said)                 # the deploy wait can be long, so the merge is said before it
    if not sha:
        return 4, "merged, but the merge commit could not be read, so the deploy cannot be checked"
    ok, why = w.serving(serving, sha, deploy_budget_s)
    return (0, f"{said}; {why}") if ok else (7, why)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops ci-run", allow_abbrev=False,
                                 description=(__doc__ or "").strip().split("\n")[0])
    ap.add_argument("run", help="the workflow run's id")
    ap.add_argument("--pr", required=True, help="the pull request the run is for")
    ap.add_argument("--repo", help="owner/name (default: the repository of the current folder)")
    ap.add_argument("--head", help="the commit the pull request must be on (a prefix is enough)")
    ap.add_argument("--budget", type=float, required=True, help="minutes the longest job may take")
    ap.add_argument("--rerun-starved", type=int, default=0, metavar="N",
                    help="new attempts allowed for jobs lost before their first step (default 0)")
    ap.add_argument("--gate", help="a command that exits 0 when there is room for a new attempt")
    ap.add_argument("--gate-gap", type=float, default=GATE_GAP_S, help="seconds between the gate's two readings")
    ap.add_argument("--no-merge", action="store_true", help="stop when every job passed")
    ap.add_argument("--method", choices=merge.METHODS, default="merge")
    ap.add_argument("--merge-timeout", type=float, default=30, help="minutes the merge waits for checks")
    ap.add_argument("--serving", help="a command that prints what the service runs; only where every merge deploys, "
                                      "since a skipped deploy job never shows the merge commit and ends with 7")
    ap.add_argument("--deploy-budget", type=float, default=20, help="minutes to wait for the deploy")
    ap.add_argument("--poll", type=float, default=POLL_S, help="seconds between looks")
    a = ap.parse_args(argv)
    code, said = see_through(a.run, a.pr, a.repo, budget_s=a.budget * 60, head=a.head,
                             reruns=a.rerun_starved, gate=a.gate, gate_gap_s=a.gate_gap,
                             no_merge=a.no_merge, method=a.method, merge_timeout_s=a.merge_timeout * 60,
                             serving=a.serving, deploy_budget_s=a.deploy_budget * 60, poll_s=a.poll)
    print(said)
    return code


if __name__ == "__main__":
    sys.exit(main())
