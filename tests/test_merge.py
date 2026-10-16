"""merge_when_green: merge only when every check on the head passed, in one step."""
import json
import subprocess

from claude_ops import merge


def run(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def check_run(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


class FakeGh:
    """gh with a pull request whose checks move through `views`, one per look."""

    def __init__(self, views, merge_code=0):
        self.views, self.calls, self.merge_code, self.merged = list(views), [], merge_code, False

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "merge"]:
            self.merged = self.merge_code == 0
            return run(self.merge_code, err="" if self.merge_code == 0 else "head moved")
        if self.merged:
            return run(out=json.dumps({"state": "MERGED", "headRefOid": "abc123def4567",
                                       "mergeCommit": {"oid": "fedcba9876543"}}))
        rollup = self.views.pop(0) if len(self.views) > 1 else self.views[0]
        return run(out=json.dumps({"state": "OPEN", "headRefOid": "abc123def4567",
                                   "statusCheckRollup": rollup}))


def go(gh, **kw):
    t = [0.0]
    return merge.merge_when_green("7", "owner/repo", gh=gh, poll_s=10, settle_s=30,
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0], **kw)


def test_verdict():
    assert merge.verdict([]) == ("none", [])
    assert merge.verdict([check_run("a"), check_run("b", conclusion="SKIPPED")]) == ("passed", [])
    assert merge.verdict([check_run("a", status="IN_PROGRESS", conclusion="")]) == ("waiting", ["a"])
    assert merge.verdict([check_run("a", conclusion="FAILURE")]) == ("failed", ["a: failure"])
    assert merge.verdict([check_run("a", conclusion="NEUTRAL")])[0] == "failed"
    assert merge.verdict([{"context": "ci/x", "state": "PENDING"}]) == ("waiting", ["ci/x"])
    assert merge.verdict([{"context": "ci/x", "state": "ERROR"}]) == ("failed", ["ci/x: error"])


def test_waits_then_merges_with_the_head_it_checked():
    gh = FakeGh([[check_run("tests", status="IN_PROGRESS", conclusion="")], [check_run("tests")]])
    code, said = go(gh)
    assert code == 0 and "every check on it passed" in said
    merge_call = [c for c in gh.calls if c[:2] == ["pr", "merge"]][0]
    assert "--merge" in merge_call and merge_call[merge_call.index("--match-head-commit") + 1] == "abc123def4567"


def test_a_failed_check_is_never_merged():
    gh = FakeGh([[check_run("lint"), check_run("tests", conclusion="FAILURE")]])
    code, said = go(gh)
    assert code == 3 and "tests: failure" in said
    assert not any(c[:2] == ["pr", "merge"] for c in gh.calls)


def test_no_checks_is_refused_unless_allowed():
    code, said = go(FakeGh([[]]))
    assert code == 2 and "no checks" in said
    code, _ = go(FakeGh([[]]), allow_no_checks=True)
    assert code == 0


def test_running_checks_past_the_timeout_are_not_merged():
    gh = FakeGh([[check_run("slow", status="IN_PROGRESS", conclusion="")]])
    code, said = go(gh, timeout_s=60)
    assert code == 2 and "slow" in said and not any(c[:2] == ["pr", "merge"] for c in gh.calls)


def test_a_refused_merge_is_reported():
    code, said = go(FakeGh([[check_run("tests")]], merge_code=1))
    assert code == 4 and "head moved" in said


def test_an_unknown_method_is_refused():
    code, _ = go(FakeGh([[check_run("tests")]]), method="fast-forward")
    assert code == 2
