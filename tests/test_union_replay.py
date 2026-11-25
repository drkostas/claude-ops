"""union's branch reader: whether a rebase can leave a line of a union file twice.

Everything happens in a throwaway repo under tmp_path. The point of the reader is that no merge of
the two heads shows this, so each case is built from the commits, not from a conflict."""
import subprocess
from pathlib import Path

import pytest

from claude_ops import union


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


def commit(repo, msg):
    git(repo, "-c", "user.email=t@example.org", "-c", "user.name=t", "commit", "-qm", msg)


def write(repo, name, lines):
    (repo / name).write_text("".join(f"{line}\n" for line in lines))
    git(repo, "add", "--", name)


@pytest.fixture
def repo(tmp_path):
    """A repo on `base`, with a journal of two entries and another file beside it."""
    r = tmp_path / "proj"
    git(tmp_path, "init", "-q", "-b", "base", str(r))
    write(r, "journal.md", ["one", "two"])
    write(r, "other.md", ["x"])
    commit(r, "init")
    git(r, "checkout", "-q", "-b", "work")
    return r


def risk(repo, **kw):
    return union.replay_doubles(repo, "base", "journal.md", **kw)


def test_a_later_commit_rewriting_an_earlier_line_can_double_it(repo):
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    first = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                           text=True, check=True).stdout.strip()
    write(repo, "journal.md", ["one", "two", "three and a half"])
    commit(repo, "say it better")
    second = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=True).stdout.strip()
    rows = risk(repo)
    assert [(r["line"], r["added_by"], r["removed_by"]) for r in rows] == [("three", first, second)]


def test_a_later_commit_removing_an_earlier_line_can_double_it(repo):
    write(repo, "journal.md", ["one", "two", "three", "three"])
    commit(repo, "add three twice by mistake")
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "keep one of them")
    assert [r["line"] for r in risk(repo)] == ["three"]


def test_two_commits_that_only_add_cannot_double_a_line(repo):
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    write(repo, "journal.md", ["one", "two", "three", "four"])
    commit(repo, "add four")
    assert risk(repo) == []


def test_a_line_the_base_wrote_is_a_different_question(repo):
    write(repo, "journal.md", ["one", "two changed by the branch"])
    commit(repo, "change what the base wrote")
    assert risk(repo) == []


def test_a_blank_line_is_not_a_double(repo):
    write(repo, "journal.md", ["one", "two", "", "three"])
    commit(repo, "add a blank line and three")
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "take the blank line out")
    assert risk(repo) == []


def test_only_the_named_file_is_read(repo):
    write(repo, "other.md", ["x", "y"])
    commit(repo, "touch the other file")
    write(repo, "other.md", ["x", "y changed"])
    commit(repo, "change it again")
    assert union.commits_touching(repo, "base", "journal.md") == []
    assert len(union.commits_touching(repo, "base", "other.md")) == 2


def test_a_base_that_moved_still_gives_only_the_branch_commits(repo):
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    git(repo, "checkout", "-q", "base")
    write(repo, "journal.md", ["one", "two", "something else"])
    commit(repo, "the base moves on")
    rows = union.commits_touching(repo, "base", "journal.md", head)
    assert [r["subject"] for r in rows] == ["add three"]


def test_the_report_exits_1_on_the_shape_and_names_both_commits(repo, capsys):
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    write(repo, "journal.md", ["one", "two", "three and a half"])
    commit(repo, "say it better")
    assert union.report_replay_risk(repo, "base", "journal.md") == 1
    out = capsys.readouterr().out
    assert "2 commit(s) of base..HEAD change it" in out
    assert "a rebase can leave a line of journal.md twice" in out
    assert "squash this branch's commits on journal.md" in out
    assert "the line: three" in out


def test_the_report_says_why_two_add_only_commits_are_safe(repo, capsys):
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    write(repo, "journal.md", ["one", "two", "three", "four"])
    commit(repo, "add four")
    assert union.report_replay_risk(repo, "base", "journal.md") == 0
    out = capsys.readouterr().out
    assert "each of them only adds lines" in out
    assert "a rebase can leave" not in out


def test_one_commit_needs_no_advice(repo, capsys):
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    assert union.report_replay_risk(repo, "base", "journal.md") == 0
    out = capsys.readouterr().out
    assert "1 commit(s) of base..HEAD change it" in out
    assert "each of them" not in out


def test_a_file_no_commit_touches_is_said_so(repo, capsys):
    write(repo, "other.md", ["x", "y"])
    commit(repo, "touch the other file")
    assert union.report_replay_risk(repo, "base", "journal.md") == 0
    assert "no commit of base..HEAD changes it" in capsys.readouterr().out


def test_the_command_reads_a_head_without_checking_it_out(repo):
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    write(repo, "journal.md", ["one", "two", "three and a half"])
    commit(repo, "say it better")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    git(repo, "checkout", "-q", "base")
    assert union.main([str(repo), "--replay-risk", "journal.md", "--base", "base",
                       "--head", head]) == 1
    assert union.main([str(repo), "--replay-risk", "journal.md", "--base", "base"]) == 0


def test_a_merge_commit_is_left_out_and_what_it_merged_is_not(repo):
    """A rebase drops the merge and replays both sides, so both sides' commits are rows and the
    merge is not. Commits on parallel branches have no order between them, hence the set."""
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    git(repo, "checkout", "-q", "-b", "side", "base")
    write(repo, "journal.md", ["one", "two", "four"])
    commit(repo, "add four")
    git(repo, "checkout", "-q", "work")
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.org", "-c", "user.name=t",
                    "merge", "-q", "--no-edit", "-X", "ours", "side"], check=True, capture_output=True)
    rows = union.commits_touching(repo, "base", "journal.md")
    assert len(rows) == 2
    assert {r["subject"] for r in rows} == {"add three", "add four"}


def test_a_base_the_repository_lacks_is_not_an_empty_history(repo, capsys):
    """A range naming a revision the repository does not hold used to read as "no commit changes
    it", exit 0, which a caller gating on this takes as a promise that a rebase is safe. The read
    now raises, the command exits 2, and the file is never called safe on a question nobody
    answered. A clone with no remote is the ordinary way to meet it."""
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    with pytest.raises(union.Refused) as e:
        union.commits_touching(repo, "origin/main", "journal.md")
    assert "origin/main..HEAD" in str(e.value)
    assert union.main([str(repo), "--replay-risk", "journal.md", "--base", "origin/main"]) == 2
    said = capsys.readouterr()
    assert "could not read origin/main..HEAD" in said.err
    assert "no commit of" not in said.out        # ⛔ never the reassuring line for an unread range


def test_a_head_that_does_not_resolve_is_the_same_answer(repo, capsys):
    """The other end of the range, for a caller that passes a head read from somewhere else."""
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    assert union.main([str(repo), "--replay-risk", "journal.md", "--base", "base",
                       "--head", "no/such/head"]) == 2
    assert "no commit of" not in capsys.readouterr().out


def test_a_range_that_reads_still_answers_0_and_1(repo):
    """The fix changes nothing for a range git can read, which is every ordinary call."""
    write(repo, "beside.md", ["a line"])
    commit(repo, "a file the journal does not name")
    assert union.main([str(repo), "--replay-risk", "journal.md", "--base", "base"]) == 0
    write(repo, "journal.md", ["one", "two", "three"])
    commit(repo, "add three")
    write(repo, "journal.md", ["one", "two", "three and a half"])
    commit(repo, "say it better")
    assert union.main([str(repo), "--replay-risk", "journal.md", "--base", "base"]) == 1
