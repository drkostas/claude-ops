"""The stray-refs hook: a fetch whose refspec names its own destination is refused, with the two
ways through in the refusal, and every fetch that leaves no stray goes through.

⭐ THE SENTENCE THE REFUSAL TURNS ON IS MEASURED HERE AGAINST REAL GIT, AND IT IS TWO ANSWERS. A
destination inside a remote's own tracking namespace IS removed by a prune of that remote, so the
refusal says only that nothing runs one by itself; a destination anywhere else is removed by neither
`git fetch --prune` nor `git remote prune`, ever. The first version of this hook said the harsher
sentence for both, and this test is what refuted it.
"""
import json
import subprocess

import pytest

from claude_ops import cli
from claude_ops.hooks import stray_refs as sr


def call(command: str, cwd: str = "/tmp") -> str | None:
    return sr.check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})


REFUSED = [
    "git fetch origin +refs/pull/7/head:refs/remotes/origin/pr-7",
    'git fetch origin "+refs/pull/*/head:refs/remotes/pr-tmp/*"',
    "git fetch -q origin +refs/pull/7/head:refs/rider/7",
    "git -C /tmp/elsewhere fetch origin refs/pull/7/head:refs/remotes/origin/pr-7",
    "cd /tmp && git fetch -q origin +refs/pull/7/head:refs/remotes/origin/pr-7",
    "git fetch origin refs/heads/main:refs/remotes/origin/kept",
]
THROUGH = [
    "git fetch origin refs/pull/7/head",
    "git fetch --no-write-fetch-head origin refs/pull/7/head refs/pull/8/head",
    "git fetch origin master:master",
    "git fetch origin refs/heads/main:refs/heads/main",
    "git fetch origin refs/tags/v1:refs/tags/v1",
    'git fetch origin "refs/notes/*:refs/notes/*"',
    'git fetch origin "refs/replace/*:refs/replace/*"',
    "git fetch git@github.com:owner/repo.git main",
    "git fetch https://host.example.org/owner/repo.git main",
    "git push origin HEAD:refs/heads/feature",
    "git fetch --all --prune",
    "git remote prune origin",
]


@pytest.mark.parametrize("command", REFUSED)
def test_a_fetch_that_writes_its_own_ref_is_refused(command):
    assert call(command), command


@pytest.mark.parametrize("command", THROUGH)
def test_everything_else_goes_through(command):
    assert call(command) is None, command


def test_the_refusal_names_the_ref_and_both_ways_through():
    why = call("git fetch origin +refs/pull/7/head:refs/remotes/origin/pr-7")
    assert "refs/remotes/origin/pr-7" in why
    assert "--no-write-fetch-head" in why and "ls-remote" in why
    assert "remote.<name>.fetch" in why


@pytest.mark.parametrize("ref, remotes, says", [
    ("refs/remotes/origin/pr-7", {"origin"}, "only `git fetch --prune origin` removes it"),
    ("refs/remotes/pr-tmp/1000", {"origin"}, "pr-tmp is no remote of this repository"),
    ("refs/mine/7", {"origin"}, "nothing will ever remove it"),
    ("refs/remotes/origin/pr-7", None, "could not be read here"),
])
def test_what_would_remove_it_is_four_answers_and_never_a_guess(ref, remotes, says):
    assert says in sr.fate(ref, remotes)


def test_a_wildcard_says_how_many_it_would_leave():
    why = call('git fetch origin "+refs/pull/*/head:refs/remotes/a/*" '
               '"+refs/pull/*/head:refs/remotes/b/*"')
    assert "1 more on this line" in why


def test_another_tool_and_an_empty_command_are_not_judged():
    assert sr.check({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x", "content": "y"}}) is None
    assert sr.check({"tool_name": "Bash", "tool_input": {"command": ""}}) is None
    assert sr.check({}) is None


def test_the_cli_runs_it_and_exits_2(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "git fetch o +refs/pull/7/head:refs/remotes/o/pr-7"}})))
    assert cli.main(["hook", "stray-refs"]) == 2
    assert "Blocked:" in capsys.readouterr().err


# ---------------------------------------------------------------- the measurement
IDENT = ["-c", "user.name=test", "-c", "user.email=test@example.org", "-c", "commit.gpgsign=false",
         "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"]


def git(cwd, *args):
    done = subprocess.run(["git", *IDENT, *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr.strip()}"
    return done.stdout


def refs(cwd):
    return set(git(cwd, "for-each-ref", "--format=%(refname)").split())


def configured(cwd):
    return sr.configured_remotes(str(cwd))


def test_a_prune_reaches_the_remotes_own_namespace_and_nothing_else(tmp_path):
    git(tmp_path, "init", "-q", "--bare", "origin.git")
    origin, work, clone = tmp_path / "origin.git", tmp_path / "work", tmp_path / "clone"
    git(tmp_path, "init", "-q", "work")
    (work / "a.txt").write_text("one\n")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "one")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "HEAD:refs/heads/feature")
    git(tmp_path, "clone", "-q", str(origin), "clone")

    # three destinations written on a command line: inside origin's namespace, in another one, and
    # at a name of its own
    git(clone, "fetch", "-q", "origin", "+refs/heads/feature:refs/remotes/origin/inside")
    git(clone, "fetch", "-q", "origin", "+refs/heads/feature:refs/remotes/elsewhere/outside")
    git(clone, "fetch", "-q", "origin", "+refs/heads/feature:refs/mine/7")
    assert {"refs/remotes/origin/inside", "refs/remotes/elsewhere/outside",
            "refs/mine/7"} <= refs(clone)

    git(work, "push", "-q", "origin", "--delete", "feature")
    git(clone, "fetch", "-q", "--prune", "origin")
    git(clone, "remote", "prune", "origin")
    left = refs(clone)
    assert "refs/remotes/origin/inside" not in left, "a prune reaches its own namespace"
    assert "refs/remotes/elsewhere/outside" in left, "and no prune reaches another name"
    assert "refs/mine/7" in left, "nor one invented at the top"
    assert "refs/remotes/origin/feature" not in left, "the real tracking ref goes with the branch"

    # so the refusal says the milder thing for the first and the harsher for the other two, read
    # from this repository's own remotes rather than from the shape alone
    assert configured(clone) == {"origin"}
    assert "which nothing runs by itself" in call(
        "git fetch origin +refs/heads/feature:refs/remotes/origin/inside", str(clone))
    assert "elsewhere is no remote of this repository" in call(
        "git fetch origin +refs/heads/feature:refs/remotes/elsewhere/outside", str(clone))
    assert call("git fetch --prune origin", str(clone)) is None


def test_the_source_only_fetch_the_refusal_offers_writes_no_ref(tmp_path):
    git(tmp_path, "init", "-q", "--bare", "origin.git")
    origin, work, clone = tmp_path / "origin.git", tmp_path / "work", tmp_path / "clone"
    git(tmp_path, "init", "-q", "work")
    (work / "a.txt").write_text("one\n")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "one")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "HEAD:refs/custom/7/head")
    git(tmp_path, "init", "-q", "clone")
    git(clone, "remote", "add", "origin", str(origin))

    sha = git(clone, "ls-remote", "origin", "refs/custom/7/head").split()[0]
    git(clone, "fetch", "-q", "--no-write-fetch-head", "origin", "refs/custom/7/head")
    assert refs(clone) == set(), "it writes no ref at all"
    assert not (clone / ".git" / "FETCH_HEAD").exists(), "and no FETCH_HEAD to be ambiguous later"
    git(clone, "cat-file", "-e", f"{sha}^{{commit}}")       # the objects are here, by sha
