"""merge_when_green: merge only when every check on the head passed, in one step."""
import json
import subprocess

from claude_ops import merge


def run(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def check_run(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


BOT_PR = {"title": "chore(deps): bump next from 16.3.8 to 16.4.1 in /web", "body": "",
          "author": {"login": "app/dependabot", "is_bot": True},
          "files": [{"path": "web/package.json"}, {"path": "web/package-lock.json"}],
          "url": "https://github.com/owner/repo/pull/7"}


class FakeGh:
    """gh with a pull request whose checks move through `views`, one per look. `pr` is what the
    dependency gate reads; `allowed` what `repo view` says the repository permits."""

    def __init__(self, views, merge_code=0, pr=None, allowed=("merge", "squash", "rebase")):
        self.views, self.calls, self.merge_code, self.merged = list(views), [], merge_code, False
        self.pr, self.allowed = pr or BOT_PR, allowed

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "merge"]:
            self.merged = self.merge_code == 0
            return run(self.merge_code, err="" if self.merge_code == 0 else "head moved")
        if args[:2] == ["repo", "view"]:
            return run(out=json.dumps({"mergeCommitAllowed": "merge" in self.allowed,
                                       "squashMergeAllowed": "squash" in self.allowed,
                                       "rebaseMergeAllowed": "rebase" in self.allowed}))
        if self.merged:
            return run(out=json.dumps({"state": "MERGED", "headRefOid": "abc123def4567",
                                       "mergeCommit": {"oid": "fedcba9876543"}}))
        rollup = self.views.pop(0) if len(self.views) > 1 else self.views[0]
        return run(out=json.dumps({"state": "OPEN", "headRefOid": "abc123def4567",
                                   "statusCheckRollup": rollup, **self.pr}))


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


def test_auto_picks_the_first_method_the_repository_allows():
    gh = FakeGh([[check_run("tests")]], allowed=("squash", "rebase"))
    code, said = go(gh, method="auto")
    assert code == 0 and "(squash)" in said
    assert "--squash" in [c for c in gh.calls if c[:2] == ["pr", "merge"]][0]
    code, said = go(FakeGh([[check_run("tests")]], allowed=()), method="auto")
    assert code == 4 and "no merge method" in said


def test_dependency_bump_merges_only_a_bots_update_within_the_level():
    gh = FakeGh([[check_run("tests")]])
    code, said = go(gh, dependency_bump="minor")
    assert code == 0 and gh.calls[0][:3] == ["pr", "view", "7"] and "files" in gh.calls[0][4], \
        "the gate reads the pull request with its title, author and files first"
    code, said = go(FakeGh([[check_run("tests")]]), dependency_bump="patch")
    assert code == 6 and "minor bump: next 16.3.8 to 16.4.1" in said
    person = dict(BOT_PR, author={"login": "someone", "is_bot": False})
    gh = FakeGh([[check_run("tests")]], pr=person)
    code, said = go(gh, dependency_bump="minor")
    assert code == 6 and "not by a bot" in said and not any(c[:2] == ["pr", "merge"] for c in gh.calls)
    code_change = dict(BOT_PR, files=BOT_PR["files"] + [{"path": "web/src/app.ts"}])
    code, said = go(FakeGh([[check_run("tests")]], pr=code_change), dependency_bump="minor")
    assert code == 6 and "web/src/app.ts" in said
    gh = FakeGh([[check_run("tests", conclusion="FAILURE")]])
    code, said = go(gh, dependency_bump="minor")
    assert code == 3, "the gate passing never skips the checks"


def test_pull_request_reads_the_gate_fields():
    gh = FakeGh([[check_run("tests")]])
    pr = merge.pull_request("7", "owner/repo", gh=gh)
    assert pr["title"] == BOT_PR["title"] and gh.calls[0][4] == merge.GATE_FIELDS and gh.calls[0][-1] == "owner/repo"
    assert merge.dependency_bump_review(pr, "minor").ok
