"""merge_when_green: merge only when every check on the head passed, in one step."""
import json
import subprocess

from claude_ops import merge


def run(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def check_run(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


BOT_DIFF = ("diff --git a/web/package.json b/web/package.json\n--- a/web/package.json\n+++ b/web/package.json\n"
            "@@ -10,3 +10,3 @@\n   \"dependencies\": {\n-    \"next\": \"16.3.8\",\n+    \"next\": \"16.4.1\",\n")
BOT_PR = {"title": "chore(deps): bump next from 16.3.8 to 16.4.1 in /web", "body": "",
          "author": {"login": "app/dependabot", "is_bot": True},
          "files": [{"path": "web/package.json"}, {"path": "web/package-lock.json"}],
          "url": "https://github.com/owner/repo/pull/7"}


class FakeGh:
    """gh with a pull request whose checks move through `views`, one per look. `pr` is what the
    dependency gate reads; `allowed` what `repo view` says the repository permits."""

    def __init__(self, views, merge_code=0, pr=None, allowed=("merge", "squash", "rebase"), diff=None,
                 check_runs=None, runs=None, required=None):
        self.views, self.calls, self.merge_code, self.merged = list(views), [], merge_code, False
        self.pr, self.allowed, self.diff = pr or BOT_PR, allowed, BOT_DIFF if diff is None else diff
        self.check_runs = check_runs
        # one entry per look, like `views`, so a run can appear after the first read
        self.runs = [[]] if runs is None else [list(r) for r in runs]
        self.required = required

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "api" and "check-runs" in args[1]:
            if self.check_runs is None:
                return run(1, err="not found")
            return run(out=json.dumps({"check_runs": self.check_runs}))
        if args[0] == "api" and "actions/runs" in args[1]:
            now = self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]
            return run(out=json.dumps({"workflow_runs": now}))
        if args[0] == "api" and "required_status_checks" in args[1]:
            if self.required is None:
                return run(1, err="Branch not protected")
            return run(out=json.dumps({"checks": [{"context": c} for c in self.required]}))
        if args[:3] == ["repo", "view", "--json"] and "nameWithOwner" in args[3]:
            return run(out="owner/repo\n")
        if args[:2] == ["pr", "diff"]:
            return run(out=self.diff)
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
                                   "baseRefName": "main",
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
    sneaky = BOT_DIFF + "diff --git a/web/package-lock.json b/web/package-lock.json\n@@ -1,2 +1,2 @@\n     \"node_modules/left-pad\": {\n-      \"version\": \"1.3.0\",\n+      \"version\": \"2.0.0\",\n"
    gh = FakeGh([[check_run("tests")]], diff=sneaky)
    code, said = go(gh, dependency_bump="minor")
    assert code == 6 and "left-pad" in said and "do not name" in said, "the diff is read, not only the text"
    assert any(c[:2] == ["pr", "diff"] for c in gh.calls)
    app = dict(BOT_PR, author={"login": "app/some-app", "is_bot": True})
    code, said = go(FakeGh([[check_run("tests")]], pr=app), dependency_bump="minor")
    assert code == 6 and "not by a bot" in said, "an App that is not a named updater is not a bot here"
    code, said = go(FakeGh([[check_run("tests")]], pr=app), dependency_bump="minor",
                    dependency_bots=merge.dependency_bump.UPDATERS + ("app/some-app",))
    assert code == 0, "unless the caller names it"
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


# --- cascade: merge several, each only when it is clean --------------------

class PR:
    """A pull request gh can look at, merge and update. `status` is its merge-state status; after
    `update-branch` from BEHIND it becomes `update_to`; while BLOCKED it turns CLEAN after
    `blocked_polls` cascade state-reads (checks finishing)."""

    def __init__(self, number, status="CLEAN", *, update_to="CLEAN", blocked_polls=0, oid=None):
        self.n = str(number)
        self.status = status
        self.update_to = update_to
        self.blocked_polls = blocked_polls
        self.oid = oid or ("oid" + str(number))
        self.merged = False
        self.merge_oid = None


class FakeRepo:
    """gh over a set of pull requests. `strict` makes merging one put every other open pull request
    behind its base, the way a branch rule that requires up-to-date does. `checks` and `deployment`
    are what the merge commit's check-runs and its deployment report, for --confirm-prod."""

    def __init__(self, prs, *, strict=False, checks="success", deployment="success"):
        self.prs = {p.n: p for p in prs}
        self.strict, self.checks, self.deployment = strict, checks, deployment
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "api":
            return self._api(args[1])
        cmd = args[:2]
        if cmd == ["pr", "view"]:
            return self._view(args)
        if cmd == ["pr", "merge"]:
            return self._merge(args)
        if cmd == ["pr", "update-branch"]:
            p = self.prs[args[2]]
            if p.status == "BEHIND":
                p.status, p.oid = p.update_to, p.oid + "u"
            return run(0)
        if cmd == ["repo", "view"]:
            return run(out=json.dumps({"mergeCommitAllowed": True, "squashMergeAllowed": True,
                                       "rebaseMergeAllowed": True, "nameWithOwner": "owner/repo"}))
        return run(1, err="unexpected: " + " ".join(args))

    def _view(self, args):
        p = self.prs[args[2]]
        fields = args[args.index("--json") + 1]
        if p.merged:
            return run(out=json.dumps({"number": int(p.n), "state": "MERGED", "headRefOid": p.oid,
                                       "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                                       "mergeCommit": {"oid": p.merge_oid},
                                       "statusCheckRollup": [check_run("ci")]}))
        if fields == merge.STATE_FIELDS and p.status == "BLOCKED" and p.blocked_polls > 0:
            p.blocked_polls -= 1
            if p.blocked_polls == 0:
                p.status = "CLEAN"
        rollup = [check_run("ci")] if p.status == "CLEAN" else [check_run("ci", status="IN_PROGRESS", conclusion="")]
        return run(out=json.dumps({"number": int(p.n), "state": "OPEN", "headRefOid": p.oid,
                                   "mergeCommit": None, "statusCheckRollup": rollup,
                                   "mergeable": "CONFLICTING" if p.status == "DIRTY" else "MERGEABLE",
                                   "mergeStateStatus": p.status}))

    def _merge(self, args):
        p = self.prs[args[2]]
        if p.status != "CLEAN":
            return run(1, err="not mergeable")
        if "--match-head-commit" in args and args[args.index("--match-head-commit") + 1] != p.oid:
            return run(1, err="head moved")
        p.merged, p.merge_oid = True, "merged" + p.n
        if self.strict:
            for q in self.prs.values():
                if not q.merged and q.status in ("CLEAN", "BLOCKED"):
                    q.status = "BEHIND"
        return run(0)

    def _api(self, path):
        if "check-runs" in path:
            return run(out=json.dumps({"check_runs": [{"name": "ci", "status": "completed",
                                                       "conclusion": self.checks}]}))
        if "/statuses" in path:
            return run(out=json.dumps([{"state": self.deployment}]))
        if "deployments" in path:
            return run(out=json.dumps([{"id": 99, "environment": "Production"}]))
        return run(1, err="unexpected api: " + path)


def casc(gh, numbers, *, timeout_s=600, **kw):
    t = [0.0]
    return merge.cascade(numbers, "owner/repo", gh=gh, poll_s=10, settle_s=30, timeout_s=timeout_s,
                         sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0], **kw)


def merges_of(gh, n):
    return [c for c in gh.calls if c[:2] == ["pr", "merge"] and c[2] == str(n)]


def test_classify():
    assert merge.classify({"state": "OPEN", "mergeStateStatus": "CLEAN"}) == "clean"
    assert merge.classify({"state": "OPEN", "mergeStateStatus": "BEHIND"}) == "behind"
    assert merge.classify({"state": "OPEN", "mergeStateStatus": "BLOCKED"}) == "waiting"
    assert merge.classify({"state": "OPEN", "mergeStateStatus": "UNSTABLE"}) == "waiting"
    assert merge.classify({"state": "OPEN", "mergeStateStatus": "DIRTY"}) == "dirty"
    assert merge.classify({"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "UNKNOWN"}) == "dirty"
    assert merge.classify({"state": "OPEN", "mergeStateStatus": "DRAFT"}) == "draft"
    assert merge.classify({"state": "MERGED"}) == "merged"
    assert merge.classify({"state": "CLOSED"}) == "closed"


def test_cascade_merges_each_clean_pull_request_in_one_step():
    gh = FakeRepo([PR(1), PR(2)])
    code, said = casc(gh, ["1", "2"])
    assert code == 0 and "#1  merged" in said and "#2  merged" in said
    assert gh.prs["1"].merged and gh.prs["2"].merged
    assert len(merges_of(gh, 1)) == 1 and len(merges_of(gh, 2)) == 1


def test_cascade_updates_a_behind_pull_request_then_merges_it():
    gh = FakeRepo([PR(1, status="BEHIND", update_to="CLEAN")])
    code, said = casc(gh, ["1"])
    assert code == 0 and "#1  merged" in said
    assert any(c[:2] == ["pr", "update-branch"] and c[2] == "1" for c in gh.calls)


def test_cascade_waits_while_blocked_and_never_merges_until_clean():
    gh = FakeRepo([PR(1, status="BLOCKED", blocked_polls=2)])
    code, said = casc(gh, ["1"])
    assert code == 0 and "#1  merged" in said
    # it is blocked, not behind: it must not update the branch, and it merges once the checks clear
    assert not any(c[:2] == ["pr", "update-branch"] for c in gh.calls)
    assert len(merges_of(gh, 1)) == 1


def test_cascade_never_merges_a_conflict_but_merges_the_clean_one_first():
    gh = FakeRepo([PR(1, status="DIRTY"), PR(2, status="CLEAN")])
    code, said = casc(gh, ["1", "2"])
    assert gh.prs["2"].merged and not gh.prs["1"].merged
    assert code == 3 and "#1  refused" in said and "conflict" in said
    assert merges_of(gh, 1) == []


def test_cascade_refuses_a_lone_conflict_without_merging():
    gh = FakeRepo([PR(1, status="DIRTY")])
    code, said = casc(gh, ["1"])
    assert code == 3 and "refused" in said and not gh.prs["1"].merged
    assert not any(c[:2] == ["pr", "merge"] for c in gh.calls)


def test_cascade_handles_a_strict_rule_that_puts_the_rest_behind():
    gh = FakeRepo([PR(1), PR(2), PR(3)], strict=True)
    code, said = casc(gh, ["1", "2", "3"])
    assert code == 0 and all(gh.prs[n].merged for n in ("1", "2", "3"))
    updated = [c[2] for c in gh.calls if c[:2] == ["pr", "update-branch"]]
    assert "2" in updated and "3" in updated          # each later one was behind after a merge
    assert all(len(merges_of(gh, n)) == 1 for n in ("1", "2", "3"))


def test_cascade_confirms_production_after_the_last_merge():
    gh = FakeRepo([PR(1)], checks="success", deployment="success")
    code, said = casc(gh, ["1"], confirm_prod=True)
    assert code == 0 and "deployment succeeded" in said
    assert any(c[0] == "api" and "deployments" in c[1] for c in gh.calls)


def test_cascade_reports_a_failed_production_deployment():
    gh = FakeRepo([PR(1)], deployment="failure")
    code, said = casc(gh, ["1"], confirm_prod=True)
    assert gh.prs["1"].merged                          # the merge happened
    assert code == 7 and "failure" in said


def test_cascade_times_out_with_a_pull_request_still_waiting():
    gh = FakeRepo([PR(1, status="BLOCKED", blocked_polls=0)])   # never clears
    code, said = casc(gh, ["1"], timeout_s=50)
    assert code == 2 and "still waiting" in said and not gh.prs["1"].merged


def test_confirm_production_needs_both_checks_and_the_deployment():
    ok, msg = merge.confirm_production("owner/repo", "abc", gh=FakeRepo([], checks="success", deployment="success"),
                                       sleep=lambda s: None, clock=lambda: 0.0)
    assert ok and "passed its checks" in msg
    ok, msg = merge.confirm_production("owner/repo", "abc", gh=FakeRepo([], checks="failure"),
                                       sleep=lambda s: None, clock=lambda: 0.0)
    assert not ok and "did not pass" in msg


def said(name, title, summary=""):
    """A check run as the commit's check-runs API returns it."""
    return {"name": name, "output": {"title": title, "summary": summary}}


def test_failed_names_and_still_running():
    rollup = [check_run("fast", conclusion="NEUTRAL"),
              check_run("slow", status="IN_PROGRESS", conclusion=""),
              check_run("done")]
    assert merge.failed_names(rollup) == ["fast"]
    assert merge.still_running(rollup) == ["slow"]


def test_check_words_reads_what_each_check_said():
    gh = FakeGh([[]], check_runs=[said("scan", "1 secret uncovered!",
                                      "#### 1 secret was uncovered from the scan\n\nPlease look.\n"),
                                  said("other", "ignored, nobody asked for it")])
    words = merge.check_words("owner/repo", "abc123", ["scan"], gh)
    assert words == {"scan": "1 secret uncovered!. 1 secret was uncovered from the scan"}


def test_check_words_is_empty_when_the_api_cannot_be_read():
    """The words only improve a message, so a failure to read them must not raise."""
    assert merge.check_words("owner/repo", "abc123", ["scan"], FakeGh([[]])) == {}
    assert merge.check_words("owner/repo", "", ["scan"], FakeGh([[]], check_runs=[])) == {}


def test_a_stop_carries_the_check_own_words_and_names_what_was_still_running():
    """The case this came from: a fast scanner reported neutral while the long check had only just
    started, so the message must say what the scanner found and that the other had not finished."""
    rollup = [check_run("scan", conclusion="NEUTRAL"),
              check_run("tests", status="IN_PROGRESS", conclusion="")]
    gh = FakeGh([rollup], check_runs=[said("scan", "1 secret uncovered!")])
    code, said_back = go(gh)
    assert code == 3
    assert not gh.merged
    assert "scan: neutral" in said_back
    assert "1 secret uncovered!" in said_back
    assert "Still running" in said_back and "tests" in said_back


def test_neutral_stops_the_merge_even_with_nothing_to_read():
    gh = FakeGh([[check_run("scan", conclusion="NEUTRAL")]])
    code, said_back = go(gh)
    assert code == 3
    assert not gh.merged
    assert said_back == "not every check passed: scan: neutral"


def test_a_plain_failure_still_reads_as_before_when_nothing_else_runs():
    gh = FakeGh([[check_run("tests", conclusion="FAILURE")]], check_runs=[])
    code, said_back = go(gh)
    assert code == 3
    assert said_back == "not every check passed: tests: failure"

def workflow_run(name, status="completed"):
    return {"name": name, "status": status}


def test_runs_in_flight_names_unfinished_runs():
    gh = FakeGh([[]], runs=[[workflow_run("Tests", "in_progress"), workflow_run("Lint")]])
    assert merge.runs_in_flight("owner/repo", "abc123", gh) == ["Tests (in_progress)"]


def test_runs_in_flight_is_empty_when_it_cannot_be_read():
    class NoApi(FakeGh):
        def __call__(self, args):
            if args[0] == "api":
                return run(1, err="no")
            return super().__call__(args)
    assert merge.runs_in_flight("owner/repo", "abc123", NoApi([[]])) == []


def test_required_checks_is_unknown_without_protection():
    assert merge.required_checks("owner/repo", "main", FakeGh([[]])) is None
    assert merge.required_checks("owner/repo", "main", FakeGh([[]], required=["Tests"])) == ["Tests"]


def test_a_required_check_that_has_not_reported_holds_the_merge():
    rollup = [check_run("Lint")]
    gh = FakeGh([rollup], required=["Lint", "Tests"])
    code, said_back = go(gh, timeout_s=50)
    assert code == 2
    assert not gh.merged
    assert "Tests is required on main and has not reported" in said_back


def test_one_fast_green_check_does_not_merge_while_a_run_is_still_going():
    """The case this came from. A two-second check went green on a new head, the sixteen-minute one
    had been created but had no check yet, and the rollup read as everything passed."""
    rollup = [check_run("Scan")]
    gh = FakeGh([rollup], runs=[[workflow_run("Tests", "in_progress")]])
    code, said_back = go(gh, timeout_s=50)
    assert code == 2
    assert not gh.merged
    assert "Tests (in_progress)" in said_back


def test_a_run_that_appears_after_the_first_read_also_holds_it():
    """A workflow run is created a moment after a push, so one clean reading is not enough."""
    rollup = [check_run("Scan")]
    gh = FakeGh([rollup], runs=[[], [workflow_run("Tests", "queued")]])
    code, said_back = go(gh, timeout_s=50)
    assert code == 2
    assert not gh.merged
    assert "Tests (queued)" in said_back


def test_it_merges_once_nothing_is_in_flight_and_the_picture_holds():
    rollup = [check_run("Scan"), check_run("Tests")]
    gh = FakeGh([rollup], runs=[[workflow_run("Tests", "completed")]], required=["Scan", "Tests"])
    code, said_back = go(gh)
    assert code == 0, said_back
    assert gh.merged
    assert said_back.startswith("merged abc123def456 as fedcba987654 (merge)")
