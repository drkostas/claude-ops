"""The held-worktrees hook: a worktree a live tool holds is not rewritten under it."""
import io
import json
import os
import subprocess

import pytest

from claude_ops.hooks import held_worktrees as hw


@pytest.fixture()
def place(tmp_path):
    """Two worktrees, a holders folder, and a live process holding the first."""
    held_wt, free_wt, holders = tmp_path / "held", tmp_path / "free", tmp_path / "holders"
    for d in (held_wt, free_wt, holders):
        d.mkdir()
    proc = subprocess.Popen(["sleep", "60"])
    (holders / str(proc.pid)).write_text(f"{held_wt}\n")
    yield held_wt, free_wt, holders, proc
    proc.kill()
    proc.wait()


def bash(command, cwd, holders):
    return hw.check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}, hw.held(str(holders)))


@pytest.mark.parametrize("command", [
    "git rebase origin/main",
    "git reset --hard HEAD~1",
    "git commit --amend --no-edit",
    "git push --force-with-lease",
    "git pull --rebase",
    "git checkout other-branch",
    "git switch -c new",
    "git stash",
    "git merge origin/main",
])
def test_a_rewrite_in_a_held_worktree_is_refused_with_its_holder(place, command):
    held_wt, _free, holders, proc = place
    why = bash(command, held_wt, holders)
    assert why and str(proc.pid) in why and str(held_wt) in why


def test_reached_by_cd_or_dash_c_too(place):
    held_wt, free_wt, holders, _proc = place
    assert bash(f"cd {held_wt} && git rebase main", free_wt, holders)
    assert bash(f"git -C {held_wt} push", free_wt, holders)
    (held_wt / "sub").mkdir()
    assert bash(f"git -C {held_wt}/sub reset --hard", free_wt, holders)


@pytest.mark.parametrize("command", [
    "git commit -m 'a fix on top'",
    "git status",
    "git log --oneline -3",
    "git fetch origin",
    "git stash list",
    "git checkout -- a_file.py",
    "git diff",
])
def test_a_commit_on_top_and_reads_go_through(place, command):
    held_wt, _free, holders, _proc = place
    assert bash(command, held_wt, holders) is None


def test_another_worktree_and_a_dead_holder_hold_nothing(place):
    held_wt, free_wt, holders, proc = place
    assert bash("git rebase main", free_wt, holders) is None
    proc.kill()
    proc.wait()
    assert hw.held(str(holders)) == {}
    assert bash("git rebase main", held_wt, holders) is None


def test_no_holders_folder_refuses_nothing(tmp_path):
    assert hw.held(str(tmp_path / "missing")) == {} and hw.held(None) == {}
    assert hw.check({"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": "/"}, {}) is None


def test_config_from_flags_and_from_a_file(tmp_path, monkeypatch):
    assert hw.config(["--holders", "/x/h", "--message", "stop it first"]) == ("/x/h", "stop it first")
    f = tmp_path / "held.json"
    f.write_text(json.dumps({"holders": "/y/h", "message": "ship again"}))
    assert hw.config(["--config", str(f)]) == ("/y/h", "ship again")
    monkeypatch.setenv("CLAUDE_OPS_HELD_WORKTREES_CONFIG", str(tmp_path / "missing.json"))
    assert hw.config([]) == (None, "")


def test_main_refuses_with_its_signature_and_fails_open(place, monkeypatch, capsys):
    held_wt, _free, holders, proc = place
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "git rebase main"}, "cwd": str(held_wt)})))
    assert hw.main(["--holders", str(holders), "--message", "Stop the ship first."]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Blocked:") and str(proc.pid) in err and "Stop the ship first." in err
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hw.main(["--holders", str(holders)]) == 0
    assert os.path.isdir(holders)


def started_of(pid):
    return subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def full(command, cwd, holders, **kw):
    """check as main runs it: with the holders folder, so start times are compared."""
    return hw.check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)},
                    hw.held(str(holders)), str(holders), **kw)


def test_a_holder_with_its_own_start_time_still_holds(place):
    held_wt, _free, holders, proc = place
    (holders / f"{proc.pid}.started").write_text(started_of(proc.pid) + "\n")
    assert full("git rebase main", held_wt, holders)


def test_a_reused_pid_holds_nothing(place):
    """The holder was killed and another process took its pid: the start time no longer matches."""
    held_wt, _free, holders, proc = place
    (holders / f"{proc.pid}.started").write_text("Thu Jan  1 00:00:00 1970\n")
    assert full("git rebase main", held_wt, holders) is None
    assert hw.same_process(str(holders), proc.pid) is False


def test_a_holder_without_a_start_time_keeps_the_old_rule(place):
    held_wt, _free, holders, proc = place
    assert not (holders / f"{proc.pid}.started").exists()
    assert full("git push", held_wt, holders) and hw.same_process(str(holders), proc.pid)


def test_a_start_time_that_cannot_be_read_now_still_holds(place):
    held_wt, _free, holders, proc = place
    (holders / f"{proc.pid}.started").write_text("Thu Jan  1 00:00:00 1970\n")
    assert full("git reset --hard", held_wt, holders, started=lambda pid: None)


def test_a_reused_pid_does_not_hide_another_live_holder(place, tmp_path):
    """Two files list the same worktree: the reused pid's holds nothing, the live one's still does."""
    held_wt, _free, holders, proc = place
    (holders / f"{proc.pid}.started").write_text("Thu Jan  1 00:00:00 1970\n")
    other = subprocess.Popen(["sleep", "60"])
    try:
        (holders / str(other.pid)).write_text(f"{held_wt}\n")
        why = full("git rebase main", held_wt, holders)
        assert why and str(other.pid) in why and str(proc.pid) not in why
    finally:
        other.kill()
        other.wait()
