"""ci_run: wait on a deadline, re-run only a starved job, read a gate twice, merge, see it deployed."""
import json
import subprocess
from datetime import datetime, timezone

import pytest

from claude_ops import ci_run, cli

HEAD = "abc123def4567890abc123def4567890abc12345"
MERGED = "fedcba9876543210fedcba9876543210fedcba98"
T0 = 1_800_000_000.0          # the wall clock when a test starts


def run(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def job(name, conclusion="success", steps=3, status="completed", failing=None):
    st = [{"name": f"step {i}", "conclusion": "success"} for i in range(1, steps + 1)]
    if failing and st:
        st[-1] = {"name": failing, "conclusion": "failure"}
    return {"name": name, "status": status, "conclusion": conclusion, "steps": st}


GREEN = [job("tests"), job("lint")]
STARVED = [job("tests", "failure", steps=0), job("lint")]


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t

    def wall(self):
        return T0 + self.t


class FakeGh:
    """gh with one pull request and one run. `attempts` maps an attempt number to the looks at it: a
    list of job lists, one per look (the last repeats), and a run is completed once its jobs are.
    `starts` is whether a re-run makes a new attempt."""

    def __init__(self, attempts, *, run_head=HEAD, created_min_ago=4.0, clock=None, starts=True,
                 rerun_code=0, state="OPEN"):
        self.attempts = {k: list(v) for k, v in attempts.items()}
        self.attempt, self.calls, self.reruns = 1, [], []
        self.run_head, self.clock, self.starts, self.rerun_code = run_head, clock, starts, rerun_code
        self.created = T0 - created_min_ago * 60
        self.state = state

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "view"]:
            fields = args[args.index("--json") + 1]
            if fields == "mergeCommit":
                return run(out=json.dumps({"mergeCommit": {"oid": MERGED}}))
            return run(out=json.dumps({"state": self.state, "headRefOid": HEAD}))
        if args[:2] == ["run", "rerun"]:
            self.reruns.append(args)
            if self.rerun_code == 0 and self.starts:
                self.attempt += 1
            return run(self.rerun_code, err="" if self.rerun_code == 0 else "cannot be rerun")
        if args[:2] == ["run", "view"]:
            looks = self.attempts[self.attempt]
            jobs = looks.pop(0) if len(looks) > 1 else looks[0]
            done = all(j["status"] == "completed" for j in jobs)
            created = datetime.fromtimestamp(self.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return run(out=json.dumps({
                "attempt": self.attempt, "status": "completed" if done else "in_progress",
                "conclusion": "", "headSha": self.run_head, "jobs": jobs, "createdAt": created}))
        raise AssertionError(f"unexpected gh call {args}")


def go(gh, clock, merged=None, **kw):
    said = []
    merges = merged if merged is not None else []

    def merger(pr, repo, **mkw):
        merges.append((pr, repo, mkw.get("expect_head")))
        return 0, f"merged {HEAD[:12]} as {MERGED[:12]} (merge)"

    kw.setdefault("budget_s", 75 * 60)
    code, why = ci_run.see_through("99", "12", "owner/repo", gh=gh, clock=clock.now, wall=clock.wall,
                                   sleep=clock.sleep, say=said.append, poll_s=45, merger=merger, **kw)
    return code, why, said


def test_shape_tells_a_starved_job_from_a_failed_one():
    assert ci_run.shape(GREEN).kind == "green"
    assert ci_run.shape([job("tests", status="in_progress", conclusion="")]).kind == "running"
    s = ci_run.shape(STARVED)
    assert (s.kind, s.starved) == ("starved", ["tests"])
    s = ci_run.shape([job("tests", "failure", failing="pytest"), job("build", "failure", steps=0)])
    assert s.kind == "failed" and s.failed == ["tests (pytest)"], "a job with steps failed in its own code"
    assert ci_run.shape([job("tests", "timed_out", steps=0)]).kind == "starved"
    assert ci_run.shape([job("tests", "cancelled"), job("lint")]).kind == "cancelled"


def test_rerun_form_follows_the_shape():
    only = ci_run.shape(STARVED)
    assert only.rerun_args("99", "o/r") == ["run", "rerun", "99", "--repo", "o/r", "--failed"]
    both = ci_run.shape([job("tests", "failure", steps=0), job("integration", "cancelled", steps=2)])
    assert both.kind == "starved" and both.cancelled == ["integration"]
    assert both.rerun_args("99", "o/r") == ["run", "rerun", "99", "--repo", "o/r"], \
        "--failed refuses a cancelled job, so the whole run is run again"


def test_deadline_is_the_budget_plus_the_time_already_taken():
    created = "2027-01-15T08:00:00Z"
    wall = 1_800_000_000.0
    at = datetime(2027, 1, 15, 8, 4, tzinfo=timezone.utc)
    end = ci_run.deadline(75 * 60, created, at.timestamp(), 100.0)
    assert end == 100.0 + 75 * 60 + 4 * 60
    assert ci_run.deadline(60, None, wall, 5.0) == 65.0
    assert ci_run.deadline(60, "not a time", wall, 5.0) == 65.0


def test_a_long_job_is_waited_for_past_any_count_of_looks():
    clock = Clock()
    running = [job("tests", status="in_progress", conclusion="")]
    looks = [running] * 93 + [GREEN]               # done 70 minutes in, at 45 s a look
    gh = FakeGh({1: looks}, clock=clock)
    merges = []
    code, why, _ = go(gh, clock, merged=merges)
    assert code == 0, why
    assert merges == [("12", "owner/repo", HEAD)], "merged, pinned to the head the run was on"
    assert sum(1 for c in gh.calls if c[:2] == ["run", "view"]) > 80


def test_it_gives_up_at_the_deadline_not_before():
    clock = Clock()
    gh = FakeGh({1: [[job("tests", status="in_progress", conclusion="")]]}, clock=clock)
    code, why, _ = go(gh, clock)
    assert code == 2 and "deadline" in why
    assert 79 * 60 <= clock.t < 79 * 60 + 46, "the budget plus the 4 minutes the run had taken"


def test_a_job_that_failed_in_its_code_is_never_run_again():
    clock = Clock()
    gh = FakeGh({1: [[job("tests", "failure", failing="pytest")]]}, clock=clock)
    code, why, _ = go(gh, clock, reruns=1)
    assert code == 3 and "tests (pytest)" in why
    assert gh.reruns == []


def test_a_starved_job_with_no_rerun_left_stops():
    clock = Clock()
    gh = FakeGh({1: [STARVED]}, clock=clock)
    code, why, _ = go(gh, clock)
    assert code == 3 and "--rerun-starved" in why and gh.reruns == []


def test_only_cancelled_jobs_are_not_run_again():
    clock = Clock()
    gh = FakeGh({1: [[job("tests", "cancelled", steps=0)]]}, clock=clock)
    code, why, _ = go(gh, clock, reruns=1)
    assert code == 3 and "cancelled" in why and gh.reruns == []


def test_the_gate_must_pass_twice_before_a_rerun():
    clock = Clock()
    gh = FakeGh({1: [STARVED], 2: [GREEN]}, clock=clock)
    readings, at = [0, 1, 0, 0], []

    def sh(cmd):
        at.append(clock.t)
        return readings.pop(0), "room"

    code, why, said = go(gh, clock, reruns=1, gate="./room.sh", sh=sh)
    assert code == 0, why
    assert len(at) == 4 and readings == [], "pass, then shut 45 s later, then two passes"
    assert at[1] - at[0] == 45 and at[3] - at[2] == 45
    assert len(gh.reruns) == 1 and gh.reruns[0][-1] == "--failed"
    assert any("attempt 2 started" in s for s in said)


def test_a_cancelled_and_a_starved_job_take_the_full_rerun():
    clock = Clock()
    mixed = [job("tests", "failure", steps=0), job("integration", "cancelled", steps=2)]
    gh = FakeGh({1: [mixed], 2: [GREEN]}, clock=clock)
    code, why, _ = go(gh, clock, reruns=1)
    assert code == 0, why
    assert gh.reruns == [["run", "rerun", "99", "--repo", "owner/repo"]]


def test_a_rerun_that_prints_nothing_and_starts_nothing_is_not_trusted():
    clock = Clock()
    gh = FakeGh({1: [STARVED]}, clock=clock, starts=False)
    code, why, _ = go(gh, clock, reruns=1)
    assert code == 4 and "no new attempt started" in why


def test_a_run_on_another_commit_is_refused():
    clock = Clock()
    gh = FakeGh({1: [GREEN]}, clock=clock, run_head="0" * 40)
    code, why, _ = go(gh, clock)
    assert code == 5 and "000000000000" in why
    code, why, _ = go(FakeGh({1: [GREEN]}, clock=clock, state="MERGED"), clock)
    assert code == 5 and "merged" in why


def test_no_merge_stops_at_green():
    clock = Clock()
    merges = []
    code, why, _ = go(FakeGh({1: [GREEN]}, clock=clock), clock, merged=merges, no_merge=True)
    assert code == 0 and merges == [] and "passed" in why


def test_the_deploy_is_proven_by_what_the_service_runs():
    clock = Clock()
    outs = [(0, "image registry/app:0f1e2d3c"), (0, "revision app-00042 image registry/app:fedcba98")]

    def sh(cmd):
        return outs.pop(0) if len(outs) > 1 else outs[0]

    code, why, _ = go(FakeGh({1: [GREEN]}, clock=clock), clock, serving="./deployed.sh", sh=sh)
    assert code == 0 and "the service runs fedcba987654" in why

    clock = Clock()
    code, why, _ = go(FakeGh({1: [GREEN]}, clock=clock), clock, serving="./deployed.sh",
                      sh=lambda c: (0, "revision app-00043 image registry/app:0f1e2d3c"), deploy_budget_s=600)
    assert code == 7 and "fedcba987654" in why and 600 <= clock.t < 700


def test_names_commit_wants_a_whole_hex_word_of_seven():
    assert ci_run.names_commit("tag fedcba9", MERGED)
    assert not ci_run.names_commit("tag fedcba", MERGED), "six characters are not enough"
    assert not ci_run.names_commit("tag 0fedcba98", MERGED), "a word that only holds it does not count"
    assert ci_run.names_commit(f"version: {MERGED}", MERGED)


def test_the_command_is_routed(capsys):
    assert "ci-run" in cli.TOOLS
    with pytest.raises(SystemExit) as e:
        cli.main(["ci-run", "--help"])
    assert e.value.code == 0 and "--rerun-starved" in capsys.readouterr().out


def test_the_edges_each_give_their_own_code():
    clock = Clock()

    def no_repo(args):
        return run(1, err="not a git repository")

    code, why = ci_run.see_through("99", "12", None, budget_s=60, gh=no_repo, clock=clock.now,
                                   wall=clock.wall, sleep=clock.sleep, say=lambda m: None)
    assert code == 4 and "--repo" in why

    code, why, _ = go(FakeGh({1: [GREEN]}, clock=clock), clock, head="0000000")
    assert code == 5 and "not 0000000" in why

    class Unreadable(FakeGh):
        def __call__(self, args):
            if args[:2] == ["run", "view"]:
                return run(1, err="run not found")
            return super().__call__(args)

    code, why, _ = go(Unreadable({1: [GREEN]}, clock=clock), clock)
    assert code == 4 and "run not found" in why


def test_a_refused_merge_keeps_merge_s_own_code():
    clock = Clock()
    code, why = ci_run.see_through("99", "12", "owner/repo", budget_s=600, gh=FakeGh({1: [GREEN]}, clock=clock),
                                   clock=clock.now, wall=clock.wall, sleep=clock.sleep, say=lambda m: None,
                                   merger=lambda *a, **k: (3, "not every check passed: lint: failure"))
    assert (code, why) == (3, "not every check passed: lint: failure")


def test_a_merge_with_no_merge_commit_cannot_be_checked_on_the_service():
    clock = Clock()

    class NoCommit(FakeGh):
        def __call__(self, args):
            if args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "mergeCommit":
                return run(out=json.dumps({"mergeCommit": None}))
            return super().__call__(args)

    code, why, _ = go(NoCommit({1: [GREEN]}, clock=clock), clock, serving="./deployed.sh",
                      sh=lambda c: (0, MERGED))
    assert code == 4 and "merge commit could not be read" in why


def test_a_gate_that_stays_shut_spends_no_run():
    clock = Clock()
    gh = FakeGh({1: [STARVED]}, clock=clock)
    code, why, said = go(gh, clock, reruns=1, gate="./room.sh", sh=lambda c: (1, "free 2.1 GB, wants 6"),
                         budget_s=600)
    assert code == 2 and "gate stayed shut" in why and "wants 6" in why
    assert gh.reruns == [] and any("the gate holds the re-run" in s for s in said)


def test_the_merge_is_said_once():
    clock = Clock()
    code, why, said = go(FakeGh({1: [GREEN]}, clock=clock), clock)
    assert code == 0 and why.startswith("merged ") and not any(s.startswith("merged ") for s in said)

    clock = Clock()
    code, why, said = go(FakeGh({1: [GREEN]}, clock=clock), clock, serving="./deployed.sh",
                         sh=lambda c: (0, f"image registry/app:{MERGED[:8]}"))
    assert code == 0 and [s for s in said if s.startswith("merged ")] == [f"merged {HEAD[:12]} as {MERGED[:12]} (merge)"]
