"""claude-ops unwrap-github: what the user posted with broken paragraphs is found, and with --apply
rewritten with each paragraph on one line; nothing else is touched."""
import json

from claude_ops import github_unwrap as gu

WRAPPED = "Title\n\nA sentence long enough that its writer wrapped it\nin the middle.\n"
FLAT = "Title\n\nA sentence on one line.\n"


class FakeGitHub:
    def __init__(self):
        self.patches = []

    def __call__(self, args, stdin=None):
        path = [a for a in args if not a.startswith("-") and a not in ("PATCH",)][-1]
        if args[:2] == ["-X", "PATCH"]:
            self.patches.append((args[2], json.loads(stdin)["body"]))
            return {}
        if args == ["user"]:
            return {"login": "someone"}
        if path.startswith("user/repos"):
            return [{"full_name": "someone/demo"}, {"full_name": "someone/old", "archived": True},
                    {"full_name": "someone/forked", "fork": True}]
        if path.startswith("repos/someone/demo/issues?"):
            return [{"number": 1, "body": WRAPPED, "html_url": "u1", "pull_request": {}},
                    {"number": 2, "body": FLAT, "html_url": "u2"},
                    {"number": 3, "body": None, "html_url": "u3"}]
        if path.startswith("repos/someone/demo/issues/comments"):
            return [{"id": 10, "body": WRAPPED, "html_url": "c10", "user": {"login": "someone"}},
                    {"id": 11, "body": WRAPPED, "html_url": "c11", "user": {"login": "a-stranger"}}]
        if path.startswith("repos/someone/demo/releases"):
            return [{"id": 20, "body": WRAPPED, "html_url": "r20", "author": {"login": "someone"}}]
        raise AssertionError(path)


def test_it_reports_without_writing_by_default():
    gh, said = FakeGitHub(), []
    out = gu.run(gh=gh, say=said.append, sleep=lambda s: None)
    assert out["found"] == 3 and out["fixed"] == 0 and gh.patches == []
    assert out["skipped"] == ["someone/old"]
    assert any("would join the pull request u1" in s for s in said)


def test_apply_rewrites_only_the_users_wrapped_texts():
    gh = FakeGitHub()
    out = gu.run(write=True, gh=gh, say=lambda s: None, sleep=lambda s: None)
    joined = "Title\n\nA sentence long enough that its writer wrapped it in the middle.\n"
    assert out["fixed"] == 3 and gh.patches == [("repos/someone/demo/issues/1", joined),
                                                ("repos/someone/demo/issues/comments/10", joined),
                                                ("repos/someone/demo/releases/20", joined)]


def test_named_repositories_and_a_limit():
    gh = FakeGitHub()
    out = gu.run(["someone/demo"], write=True, limit=1, gh=gh, say=lambda s: None, sleep=lambda s: None)
    assert out["fixed"] == 1 and len(gh.patches) == 1


def test_gh_json_reads_a_paginated_answer_whole():
    class R:
        returncode, stderr = 0, ""
        stdout = json.dumps([[{"a": 1}], [{"a": 2}]])
    assert gu.gh_json(["--paginate", "--slurp", "x"], run=lambda *a, **k: R()) == [{"a": 1}, {"a": 2}]
