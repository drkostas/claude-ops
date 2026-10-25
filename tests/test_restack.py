"""restack rebuilds a stack of one-commit branches, settling add-only conflicts with union."""
import os
import subprocess

from claude_ops import cli, restack

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.org", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
       "GIT_EDITOR": "true", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=ENV,
                          check=True).stdout


def commit(wt, name, text, msg):
    (wt / name).write_text(text)
    git(wt, "add", name)
    git(wt, "commit", "-qm", msg)


def stack(tmp_path):
    """main has list.txt [a]; one adds b, two (on one) adds c; then main gains z at the end."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "list.txt", "a\n", "base")
    one, two = tmp_path / "one", tmp_path / "two"
    git(repo, "worktree", "add", "-q", "-b", "one", str(one), "main")
    commit(one, "list.txt", "a\nb\n", "add b")
    git(repo, "worktree", "add", "-q", "-b", "two", str(two), "one")
    commit(two, "list.txt", "a\nb\nc\n", "add c")
    commit(repo, "list.txt", "a\nz\n", "add z")
    return repo, one, two


def test_restacks_and_settles_add_only_conflicts(tmp_path):
    repo, one, two = stack(tmp_path)
    said = []
    assert restack.restack("main", [one, two], env=ENV, say=said.append) == 0
    assert (one / "list.txt").read_text() == "a\nz\nb\n"
    assert (two / "list.txt").read_text() == "a\nz\nb\nc\n"
    assert git(two, "log", "--format=%s", "main..two").split("\n")[:2] == ["add c", "add b"]
    assert [s.split()[0] for s in said] == ["one", "two"]


def test_stops_at_a_real_conflict_and_leaves_the_rebase(tmp_path):
    repo, one, two = stack(tmp_path)
    commit(repo, "list.txt", "A\nz\n", "edit a")
    said = []
    assert restack.restack("main", [one, two], env=ENV, say=said.append) == 3
    assert said and said[-1].startswith("STOP at one") and "list.txt" in said[-1]
    assert (one / ".git").exists() and "<<<<<<<" in (one / "list.txt").read_text()


def test_the_command_line(tmp_path, capsys):
    repo, one, two = stack(tmp_path)
    old = dict(os.environ)
    os.environ.update(ENV)
    try:
        assert cli.main(["restack", "main", str(one), str(two)]) == 0
    finally:
        os.environ.clear()
        os.environ.update(old)
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "kept both sides in list.txt" in lines
    assert [line.split()[0] for line in lines if not line.startswith("kept")] == ["one", "two"]


def test_upstream_moves_every_commit_after_a_rewritten_base(tmp_path):
    """The pull request below merged with its commit rewritten: only the commits after its old head
    move, settled one by one, and the old commit does not come back."""
    repo, one, two = stack(tmp_path)
    old_head = git(one, "rev-parse", "HEAD").strip()
    commit(two, "notes.txt", "1\n", "first note")
    commit(two, "list.txt", "a\nb\nc\nd\n", "add d")
    # the base takes "add b" as a new commit of its own, and gains another line at the end
    commit(repo, "list.txt", "a\nz\nb\n", "add b (squashed)")
    commit(repo, "list.txt", "a\nz\nb\ny\n", "add y")
    said = []
    assert restack.restack("main", [two], upstream=old_head, env=ENV, say=said.append) == 0
    assert git(two, "log", "--format=%s", "main..two").split() == ["add", "d", "first", "note", "add", "c"]
    assert (two / "list.txt").read_text() == "a\nz\nb\ny\nc\nd\n"
    assert said and said[-1].startswith("two ")


def test_upstream_on_the_command_line(tmp_path, capsys):
    repo, one, two = stack(tmp_path)
    old_head = git(one, "rev-parse", "HEAD").strip()
    commit(repo, "list.txt", "a\nz\nb\n", "add b (squashed)")
    old = dict(os.environ)
    os.environ.update(ENV)
    try:
        assert cli.main(["restack", "main", str(two), "--upstream", old_head]) == 0
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert git(two, "log", "--format=%s", "main..two").strip() == "add c"
