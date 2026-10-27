"""The conflict-markers hook: what a `git add`, a `git commit` and a `git rebase --continue` would
record, and what goes through. The marker lines are built at run time, so that no line of this
file is itself one."""
import io
import json
import subprocess

import pytest

from claude_ops import cli
from claude_ops.hooks import chain
from claude_ops.hooks import conflict_markers as cm

OURS = "<" * 7 + " HEAD"
BASE = "|" * 7 + " merged common ancestors"
MID = "=" * 7
THEIRS = ">" * 7 + " feature"


def git(repo, *args, check=True):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                          cwd=repo, check=check, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.txt").write_text("one\ntwo\nthree\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "first")
    return r


def bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def conflicted(repo, name="a.txt"):
    """Write the file as a stopped merge leaves it: the marker on line 2, two more below."""
    (repo / name).write_text(f"one\n{OURS}\ntwo\n{MID}\ndos\n{THEIRS}\nthree\n")


def test_the_pattern_matches_the_marker_lines_only():
    rows = [("f", 1, OURS), ("f", 2, MID), ("f", 3, THEIRS), ("f", 4, BASE),
            ("f", 5, "a " + OURS + " b"), ("f", 6, "=" * 8), ("f", 7, "<" * 7), ("f", 8, MID + "  "),
            ("f", 9, "=" * 6), ("f", 10, "")]
    assert [r[1] for r in cm.marker_rows(rows)] == [1, 2, 3, 4]


def test_an_add_of_a_tracked_file_with_markers_is_refused_with_file_and_line(repo):
    conflicted(repo)
    why = cm.check(bash("git add a.txt", repo))
    assert why == f"this would record the conflict marker {'<' * 7} in a.txt:2 (and 2 more)"
    assert "a.txt:2" in cm.check(bash("git add -A", repo))
    assert "a.txt:2" in cm.check(bash("git add .", repo))
    assert "a.txt:2" in cm.check(bash("git add -- a.txt", repo))
    assert cm.check(bash("git add -n a.txt", repo)) is None
    assert cm.check(bash("git add --dry-run .", repo)) is None
    assert cm.check(bash("git status && git diff a.txt", repo)) is None


def test_an_untracked_file_named_by_the_add_is_read_from_the_working_tree(repo):
    conflicted(repo, "new.md")
    assert "new.md:2" in cm.check(bash("git add new.md", repo))
    assert "new.md:2" in cm.check(bash("git add .", repo))
    assert cm.check(bash("git add a.txt", repo)) is None


def test_a_commit_records_what_is_staged_and_what_a_takes(repo):
    conflicted(repo)
    git(repo, "add", "a.txt")
    assert "a.txt:2" in cm.check(bash("git commit -m settle", repo))
    assert "a.txt:2" in cm.check(bash("git commit -am settle", repo))
    assert "a.txt:2" in cm.check(bash("git commit -m settle -- a.txt", repo))
    assert cm.check(bash("git commit --dry-run -m settle", repo)) is None
    git(repo, "reset", "-q")
    assert cm.check(bash("git commit -m x", repo)) is None
    assert "a.txt:2" in cm.check(bash("git commit -am x", repo))
    assert "a.txt:2" in cm.check(bash("git add a.txt && git commit -m x", repo))


def test_a_continue_reads_what_is_staged(repo):
    conflicted(repo)
    git(repo, "add", "a.txt")
    for cmd in ("git rebase --continue", "git merge --continue", "git cherry-pick --continue",
                "git revert --continue", "git am --continue", "GIT_EDITOR=true git rebase --continue"):
        assert "a.txt:2" in (cm.check(bash(cmd, repo)) or ""), cmd
    assert cm.check(bash("git rebase --abort", repo)) is None
    assert cm.check(bash("git rebase --skip", repo)) is None
    assert cm.check(bash("git rebase origin/main", repo)) is None
    git(repo, "reset", "-q")
    assert cm.check(bash("git rebase --continue", repo)) is None


def test_a_real_conflict_staged_as_git_left_it_is_refused(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "first")
    git(r, "checkout", "-q", "-b", "feature")
    (r / "a.txt").write_text("uno\n")
    git(r, "commit", "-q", "-am", "feature")
    git(r, "checkout", "-q", "main")
    (r / "a.txt").write_text("ena\n")
    git(r, "commit", "-q", "-am", "main")
    git(r, "checkout", "-q", "feature")
    assert git(r, "rebase", "main", check=False).returncode != 0
    text = (r / "a.txt").read_text()
    assert text.startswith("<" * 7) and MID in text
    assert cm.check(bash("git rebase --continue", r)) is None     # unmerged: git refuses it itself
    why = cm.check(bash("git add a.txt", r))
    assert why and "a.txt:1" in why
    git(r, "add", "a.txt")
    why = cm.check(bash("git rebase --continue", r))
    assert why and "a.txt:1" in why and "and 2 more" in why
    (r / "a.txt").write_text("ena uno\n")
    git(r, "add", "a.txt")
    assert cm.check(bash("git rebase --continue", r)) is None


def test_a_marker_inside_a_line_and_a_longer_rule_go_through(repo):
    (repo / "a.txt").write_text("one\nsee a " + OURS + " here\n" + "=" * 8 + "\n" + "=" * 6 + "\nthree\n")
    assert cm.check(bash("git add a.txt", repo)) is None
    git(repo, "add", "a.txt")
    assert cm.check(bash("git commit -m rule", repo)) is None
    assert cm.check(bash("git rebase --continue", repo)) is None


def test_C_and_cd_are_followed_and_outside_a_repository_the_call_goes_through(repo, tmp_path):
    conflicted(repo)
    assert "a.txt:2" in cm.check(bash(f"git -C {repo} add a.txt", tmp_path))
    assert "a.txt:2" in cm.check(bash(f"cd {repo} && git add a.txt", tmp_path))
    assert cm.check(bash("git add a.txt", tmp_path)) is None
    assert cm.check(bash("git add a.txt", tmp_path / "missing")) is None


def test_a_hidden_commit_reads_what_is_staged(repo):
    conflicted(repo)
    git(repo, "add", "a.txt")
    assert "a.txt:2" in cm.check(bash("bash -c 'git commit -m x'", repo))
    assert "a.txt:2" in cm.check(bash("nice git commit -m x", repo))


def test_other_tools_and_a_broken_payload_go_through(repo):
    assert cm.check({"tool_name": "Write", "tool_input": {"file_path": str(repo / "a.txt"), "content": OURS},
                     "cwd": str(repo)}) is None
    assert cm.check({"tool_name": "Bash"}) is None
    assert cm.check({}) is None


def test_main_exit_codes_and_message(repo, monkeypatch, capsys):
    conflicted(repo)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bash("git add a.txt", repo))))
    assert cm.main([]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Blocked: this would record the conflict marker " + "<" * 7 + " in a.txt:2")
    assert "Settle the file" in err
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bash("git status", repo))))
    assert cm.main([]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cm.main() == 0


def test_the_cli_and_the_chain_offer_the_hook(repo, monkeypatch):
    assert any(h.get("matcher") == "Bash" and h["hooks"][0]["command"] == "claude-ops hook conflict-markers"
               for h in cli.HOOKS["PreToolUse"])
    conflicted(repo)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bash("git add a.txt", repo))))
    assert cli.main(["hook", "conflict-markers"]) == 2
    rc, out, err = chain.run_module("claude_ops.hooks.conflict_markers", [], json.dumps(bash("git add a.txt", repo)))
    assert rc == 2 and err.startswith("Blocked:") and out == ""
    rc, out, err = chain.run_module("claude_ops.hooks.conflict_markers", [], json.dumps(bash("git status", repo)))
    assert rc == 0 and err == ""
