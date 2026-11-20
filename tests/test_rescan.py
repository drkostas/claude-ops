"""rescan: a check run that could not complete is run again once, and a finding never is."""
import json
import subprocess

from claude_ops import cli, rescan

SHA = "a" * 40
INCOMPLETE_TITLE = "Could not complete scanning of your commits"


def run(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def check_run(id_, name="Scanner", status="completed", conclusion="neutral", title=INCOMPLETE_TITLE, text=""):
    return {"id": id_, "name": name, "status": status, "conclusion": conclusion,
            "output": {"title": title, "text": text}}


def rollup(*checks):
    return [{"__typename": "CheckRun", "name": n, "status": "COMPLETED", "conclusion": c} for n, c in checks]


class FakeGh:
    """gh with one open pull request. `runs` is the commit's check runs, one list per read (the last one
    repeats), `children` the pull requests based on its branch before and after the reopen."""

    def __init__(self, runs, checks=(("Scanner", "NEUTRAL"),), head=SHA, children=([], []), close_code=0,
                 reopen_code=0, state="OPEN"):
        self.runs, self.calls = [list(r) for r in runs], []
        self.view = {"headRefOid": head, "headRefName": "feature", "state": state,
                     "statusCheckRollup": rollup(*checks)}
        self.children = [list(c) for c in children]
        self.close_code, self.reopen_code = close_code, reopen_code

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "view"]:
            return run(out=json.dumps(self.view))
        if args[:3] == ["repo", "view", "--json"]:
            return run(out="owner/repo\n")
        if args[0] == "api" and "/check-runs" in args[1]:
            now = self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]
            return run(out=json.dumps({"check_runs": now}))
        if args[:2] == ["pr", "list"]:
            now = self.children.pop(0) if len(self.children) > 1 else self.children[0]
            return run(out=json.dumps([{"number": n} for n in now]))
        if args[:2] == ["pr", "close"]:
            return run(self.close_code, err="" if self.close_code == 0 else "not allowed")
        if args[:2] == ["pr", "reopen"]:
            return run(self.reopen_code, err="" if self.reopen_code == 0 else "not allowed")
        raise AssertionError(f"unexpected gh call {args}")

    def did(self, verb):
        return [c for c in self.calls if c[:2] == ["pr", verb]]


def go(gh, **kw):
    return rescan.rescan("7", "owner/repo", gh=gh, sleep=lambda s: None, poll_s=0, **kw)


def test_incomplete_then_a_passing_run_is_green():
    gh = FakeGh([[check_run(1)], [check_run(1)], [check_run(2, status="in_progress", conclusion="", title="")],
                 [check_run(2, conclusion="success", title="No secrets detected")]])
    code, said = go(gh)
    assert code == 0, said
    assert len(gh.did("close")) == 1 and len(gh.did("reopen")) == 1
    assert "Scanner 1 could not complete" in said and "Scanner 2 passed" in said
    assert "no open pull request is based on its branch" in said


def test_incomplete_twice_stops_with_its_own_words():
    gh = FakeGh([[check_run(1)], [check_run(1)], [check_run(2)]])
    code, said = go(gh)
    assert code == 5, said
    assert len(gh.did("close")) == 1 and len(gh.did("reopen")) == 1
    assert "Scanner 2 could not complete either, so nothing was found" in said


def test_a_neutral_run_that_names_a_finding_is_never_run_again():
    gh = FakeGh([[check_run(1, title="1 secret uncovered", text="Some resources do not exist")]])
    code, said = go(gh)
    assert code == 1 and "another reason" in said, said
    assert gh.did("close") == [] and gh.did("reopen") == []


def test_the_title_decides_not_the_text():
    finding = check_run(1, title="1 secret uncovered", text="could not complete the rest")
    assert rescan.incomplete(rescan.runs_on("owner/repo", SHA, FakeGh([[finding]]))) == []
    lower = check_run(2, title="could NOT complete scanning")
    assert [r["id"] for r in rescan.incomplete(rescan.runs_on("owner/repo", SHA, FakeGh([[lower]])))] == [2]
    other = check_run(3, title="Scan stopped early")
    assert [r["id"] for r in rescan.incomplete(rescan.runs_on("o/r", SHA, FakeGh([[other]])), r"stopped early")] == [3]


def test_another_red_check_leaves_nothing_to_run_again():
    gh = FakeGh([[check_run(1), check_run(9, name="tests", conclusion="failure", title="2 failed")]],
                checks=(("Scanner", "NEUTRAL"), ("tests", "FAILURE")))
    code, said = go(gh)
    assert code == 1 and "tests did not pass" in said, said
    assert gh.did("close") == []


def test_no_red_check_leaves_nothing_to_run_again():
    gh = FakeGh([[check_run(1, conclusion="success", title="ok")]], checks=(("Scanner", "SUCCESS"),))
    code, said = go(gh)
    assert code == 1 and "no check on its head failed" in said
    assert gh.did("close") == []


def test_a_new_run_that_finds_something_is_red():
    gh = FakeGh([[check_run(1)], [check_run(2, title="1 secret uncovered")]])
    code, said = go(gh)
    assert code == 3 and "Scanner 2 is neutral: 1 secret uncovered" in said, said


def test_a_new_run_not_done_at_the_timeout():
    ticks = iter(range(0, 10000, 100))
    gh = FakeGh([[check_run(1)]])
    code, said = rescan.rescan("7", "owner/repo", gh=gh, sleep=lambda s: None, poll_s=0, timeout_s=250,
                               clock=lambda: next(ticks))
    assert code == 2 and "no new run started" in said, said


def test_another_head_is_left_alone():
    gh = FakeGh([[check_run(1)]], head="b" * 40)
    code, said = go(gh, head=SHA)
    assert code == 8 and gh.did("close") == []


def test_a_closed_pull_request_is_left_alone():
    gh = FakeGh([[check_run(1)]], state="CLOSED")
    code, said = go(gh)
    assert code == 4 and "not open" in said and gh.did("close") == []


def test_pull_requests_based_on_its_branch_are_named_before_and_after():
    gh = FakeGh([[check_run(1)], [check_run(2, conclusion="success", title="ok")]], children=([8, 9], [8, 9]))
    code, said = go(gh)
    assert code == 0 and "#8, #9 are based on its branch and still open on it" in said, said
    gh = FakeGh([[check_run(1)], [check_run(2, conclusion="success", title="ok")]], children=([8], []))
    code, said = go(gh)
    assert code == 0 and "#8 was based on its branch and is no longer open on it after the reopen" in said, said


def test_a_reopen_that_fails_says_the_pull_request_is_closed():
    gh = FakeGh([[check_run(1)]], reopen_code=1)
    code, said = go(gh)
    assert code == 4 and "WAS CLOSED" in said and "open it again by hand" in said


def test_the_command_passes_its_options(monkeypatch, capsys):
    seen = {}

    def fake(number, repo, **kw):
        seen.update(number=number, repo=repo, **kw)
        return 5, "could not complete either"
    monkeypatch.setattr(rescan, "rescan", fake)
    code = cli.main(["rescan", "7", "--repo", "owner/repo", "--head", SHA, "--timeout", "2", "--poll", "5"])
    assert code == 5 and "could not complete either" in capsys.readouterr().err
    assert seen == {"number": "7", "repo": "owner/repo", "head": SHA, "pattern": rescan.INCOMPLETE,
                    "timeout_s": 120.0, "poll_s": 5.0}
