"""A chat's worktree: its own branch, the main checkout's local-only files and memory linked in.
Everything happens in a throwaway repo, origin and folders under tmp_path."""
import subprocess
from pathlib import Path

import pytest

from claude_ops import worktree as cw


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


def commit(repo, msg):
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)


@pytest.fixture
def world(tmp_path):
    origin, repo = tmp_path / "origin.git", tmp_path / "proj.example"   # a dotted name, on purpose
    git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    (repo / "a.txt").write_text("hi")
    git(repo, "add", "a.txt")
    commit(repo, "init")
    git(repo, "push", "-q", "origin", "master")
    (repo / "CLAUDE.md").write_text("local only")
    (repo / "notes").mkdir()
    (repo / "notes" / "x.md").write_text("n")
    with open(repo / ".git" / "info" / "exclude", "a") as f:
        f.write("CLAUDE.md\n/notes\n*.log\n!keep.me\n# a comment\n")
    worktrees, projects = tmp_path / "worktrees", tmp_path / "projects"
    old = projects / str(repo.resolve()).replace("/", "-") / "memory"   # the OLD spelling
    old.mkdir(parents=True)
    return {"repo": repo, "worktrees": worktrees, "projects": projects, "memory": old, "base": tmp_path}


def prep(w, folder, name):
    return cw.prepare(folder, name, worktrees=w["worktrees"], projects=w["projects"])


def test_excluded_files_and_project_dirs(world):
    repo = world["repo"]
    assert cw.excluded_files(repo) == [repo / "CLAUDE.md", repo / "notes"]
    assert cw.project_dirs(Path("/a/b.c"), Path("/p")) == [Path("/p/-a-b-c"), Path("/p/-a-b.c")]


def test_prepare_makes_a_linked_worktree_and_reuses_it(world):
    out = prep(world, world["repo"], "fix-1")
    wt = Path(out["worktree"])
    assert wt == world["worktrees"] / "proj-example-fix-1" and (wt / "a.txt").exists()
    assert cw.owner_branch(wt) == "chat/fix-1"
    assert (wt / "CLAUDE.md").is_symlink() and (wt / "CLAUDE.md").read_text() == "local only"
    assert (wt / "notes").is_symlink() and sorted(out["linked"]) == ["CLAUDE.md", "notes"]
    mem = cw.project_dirs(wt.resolve(), world["projects"])[0] / "memory"
    assert mem.is_symlink() and mem.resolve() == world["memory"].resolve()
    again = prep(world, world["repo"], "fix-1")
    assert again["worktree"] == out["worktree"] and again["linked"] == []


def test_a_folder_that_is_not_a_repo_is_used_as_it_is(world):
    plain = world["base"] / "not-a-repo"
    plain.mkdir()
    p = prep(world, plain, "x")
    assert p["cwd"] == str(plain.resolve()) and p["worktree"] is None


@pytest.mark.parametrize("name", ["latent_space", "Fix", "ops-core"])
def test_a_name_as_typed_is_kept(world, name):
    plain = world["base"] / "plain"
    plain.mkdir(exist_ok=True)
    prep(world, plain, name)


@pytest.mark.parametrize("name", ["a b", "../x", "-x", "a.b", "a:b", "x" * 41])
def test_a_name_that_is_not_plain_is_refused(world, name):
    with pytest.raises(ValueError):
        prep(world, world["repo"], name)


def test_two_chats_never_share_a_worktree(world):
    ab = prep(world, world["repo"], "a_b")["worktree"]
    ad = prep(world, world["repo"], "a-b")["worktree"]
    assert ab != ad and Path(ab).name.endswith("a_b") and Path(ad).name.endswith("a-b")
    prep(world, world["repo"], "fix")
    # a disk that ignores case would hand Fix the folder fix has: refused, never shared
    if (world["worktrees"] / "proj-example-Fix").exists():
        with pytest.raises(ValueError, match="another chat's worktree"):
            prep(world, world["repo"], "Fix")
    assert prep(world, world["repo"], "fix")["worktree"].endswith("-fix")


def test_a_chat_whose_folder_is_gone_reuses_its_own_branch(world):
    first = Path(prep(world, world["repo"], "back")["worktree"])
    (first / "b.txt").write_text("work")
    git(first, "add", "b.txt")
    commit(first, "the chat's own commit")
    git(world["repo"], "worktree", "remove", "--force", str(first))   # the folder goes, the branch stays
    again = Path(prep(world, world["repo"], "back")["worktree"])
    assert again == first and cw.owner_branch(again) == "chat/back" and (again / "b.txt").read_text() == "work"


def test_a_chat_branch_checked_out_elsewhere_is_refused(world):
    first = Path(prep(world, world["repo"], "busy")["worktree"])
    other = world["base"] / "elsewhere"
    git(world["repo"], "worktree", "move", str(first), str(other))
    with pytest.raises(ValueError, match="checked out in another worktree"):
        prep(world, world["repo"], "busy")


def test_a_subfolder_is_the_same_place_inside_the_worktree(world):
    repo = world["repo"]
    (repo / "kernel").mkdir()
    (repo / "kernel" / "k.py").write_text("x")
    git(repo, "add", "kernel/k.py")
    commit(repo, "kernel")
    git(repo, "push", "-q", "origin", "master")
    sub = prep(world, repo / "kernel", "sub-1")
    assert sub["cwd"] == str(Path(sub["worktree"]) / "kernel") and sub["subfolder"] == "kernel"


def test_unsafe_and_remove(world):
    repo = world["repo"]
    wt = Path(prep(world, repo, "sub-1")["worktree"])
    assert cw.unsafe(wt) is None
    (wt / "new.txt").write_text("work")
    assert "uncommitted" in (cw.unsafe(wt) or "")
    git(wt, "add", "new.txt")
    commit(wt, "work")
    assert "no remote holds" in (cw.unsafe(wt) or "")
    git(wt, "push", "-q", "origin", "HEAD:refs/heads/chat/sub-1")
    git(repo, "fetch", "-q", "origin")
    assert cw.unsafe(wt) is None
    mem = cw.project_dirs(wt.resolve(), world["projects"])[0] / "memory"
    assert mem.is_symlink()
    cw.remove(wt, projects=world["projects"])
    assert not wt.exists() and not mem.is_symlink()
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "chat/sub-1"],
                              capture_output=True, text=True).stdout.strip()
    assert branches == ""
    assert world["memory"].is_dir()      # the main checkout's memory is never touched


def test_home_repo_is_the_main_checkout(world):
    wt = Path(prep(world, world["repo"], "home")["worktree"])
    assert cw.home_repo(wt) == world["repo"].resolve()
    plain = world["base"] / "none"
    plain.mkdir()
    assert cw.home_repo(plain) == plain.resolve()


def test_every_git_call_carries_the_env(world, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(argv, **k):
        if argv and argv[0] == cw.GIT:
            seen.append(k.get("env"))
        return real(argv, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    import os
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]}
    wt = Path(cw.prepare(world["repo"], "envd", worktrees=world["worktrees"],
                         projects=world["projects"], env=env)["worktree"])
    cw.unsafe(wt, env=env)
    cw.remove(wt, projects=world["projects"], env=env)
    assert seen and all(e is env for e in seen)


T0 = 1700000000      # git takes a raw time only from about 1973 on


def at(cwd, when, *a):
    """git at T0 + `when` seconds, which its HEAD log records for the move"""
    import os
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{T0 + when} +0000", "GIT_AUTHOR_DATE": f"{T0 + when} +0000"}
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, env=env)


def test_last_checked_out_names_the_folder_a_branch_was_left_in(world):
    repo, base = world["repo"], world["base"]
    one, two = base / "one", base / "two"
    at(repo, 1000, "worktree", "add", "-q", "-b", "topic", str(one), "master")
    at(one, 1100, "checkout", "-q", "-b", "later-work")       # one moves on, topic is held nowhere
    at(repo, 1200, "worktree", "add", "-q", "--detach", str(two), "master")
    at(two, 1300, "checkout", "-q", "-b", "other")
    assert cw.checked_out_at(repo, "topic") is None
    got = cw.last_checked_out(repo, ["topic", "other", "never-made"])
    assert got["topic"] == (str(one), T0 + 1100.0)                 # it left topic at 1100
    assert got["other"] == (str(two), T0 + 1300.0)
    assert "never-made" not in got and cw.last_checked_out(repo, []) == {}


def test_the_newest_folder_wins_and_the_main_checkout_counts(world):
    repo, base = world["repo"], world["base"]
    one = base / "one"
    at(repo, 1000, "checkout", "-q", "-b", "shared")
    at(repo, 1100, "checkout", "-q", "master")
    at(repo, 1200, "worktree", "add", "-q", str(one), "shared")
    at(one, 1300, "checkout", "-q", "-b", "next")
    assert cw.last_checked_out(repo, ["shared"])["shared"] == (str(one), T0 + 1300.0)
    git(repo, "worktree", "remove", str(one))                # a removed worktree takes its log with it
    assert cw.last_checked_out(repo, ["shared"])["shared"] == (str(repo), T0 + 1100.0)


def test_one_move_between_two_wanted_branches_names_both(world):
    repo, one = world["repo"], world["base"] / "one"
    at(repo, 1000, "worktree", "add", "-q", "-b", "a-branch", str(one), "master")
    at(one, 1100, "checkout", "-q", "-b", "b-branch")
    got = cw.last_checked_out(one, ["a-branch", "b-branch"])  # asked from inside a worktree too
    assert got == {"a-branch": (str(one), T0 + 1100.0), "b-branch": (str(one), T0 + 1100.0)}


def test_a_rebase_that_ends_on_a_branch_counts(world):
    repo, one = world["repo"], world["base"] / "one"
    at(repo, 1000, "worktree", "add", "-q", "-b", "rebased", str(one), "master")
    (one / "r.txt").write_text("r")
    git(one, "add", "r.txt")
    at(one, 1100, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "r")
    (repo / "m.txt").write_text("m")
    git(repo, "add", "m.txt")
    at(repo, 1150, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "m")  # master moves on
    at(one, 1200, "-c", "user.email=t@t", "-c", "user.name=t", "rebase", "-q", "master")
    # no checkout names the branch after 1000, so only the rebase's last line can give 1200
    assert cw.last_checked_out(repo, ["rebased"])["rebased"] == (str(one), T0 + 1200.0)


def test_last_checked_out_carries_the_env(world, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(argv, **k):
        if argv and argv[0] == cw.GIT:
            seen.append(k.get("env"))
        return real(argv, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    import os
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]}
    cw.last_checked_out(world["repo"], ["master"], env=env)
    assert seen and all(e is env for e in seen)


def _on(repo, branch, name, text, *, start=None):
    if start:
        git(repo, "checkout", "-q", "-b", branch, start)
    else:
        git(repo, "checkout", "-q", branch)
    (repo / name).write_text(text)
    git(repo, "add", name)
    commit(repo, f"{branch}: {name}")
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_change_id_holds_through_a_rebase_and_changes_with_an_edit(world):
    repo = world["repo"]
    read = _on(repo, "topic", "b.txt", "one\n", start="master")
    seen = cw.change_id(repo, read, "master")
    assert seen and len(seen) == 40
    _on(repo, "master", "c.txt", "elsewhere\n")                     # the base moves on
    git(repo, "checkout", "-q", "topic")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "rebase", "-q", "master")
    rebased = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert rebased != read and cw.change_id(repo, rebased, "master") == seen
    edited = _on(repo, "topic", "b.txt", "one\ntwo\n")             # a new commit after the read
    assert cw.change_id(repo, edited, "master") not in (None, seen)


def test_change_id_of_a_stacked_branch_holds_when_the_one_below_is_rebased(world):
    repo = world["repo"]
    _on(repo, "low", "low.txt", "low\n", start="master")
    high = _on(repo, "high", "high.txt", "high\n", start="low")
    seen = cw.change_id(repo, high, "low")
    _on(repo, "master", "c.txt", "elsewhere\n")
    git(repo, "checkout", "-q", "low")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "rebase", "-q", "master")
    old_low = subprocess.run(["git", "-C", str(repo), "rev-parse", "low@{1}"], capture_output=True, text=True).stdout.strip()
    git(repo, "checkout", "-q", "high")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "rebase", "-q", "--onto", "low", old_low)
    moved = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert moved != high and seen and cw.change_id(repo, moved, "low") == seen


def test_change_id_is_none_for_no_change_or_an_unknown_commit(world):
    repo = world["repo"]
    assert cw.change_id(repo, "master", "master") is None
    assert cw.change_id(repo, "0" * 40, "master") is None


def test_change_id_carries_the_env(world, monkeypatch):
    repo = world["repo"]
    head = _on(repo, "topic", "b.txt", "one\n", start="master")
    seen = []
    real = subprocess.run

    def spy(argv, **k):
        if argv and argv[0] == cw.GIT:
            seen.append(k.get("env"))
        return real(argv, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    import os
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]}
    assert cw.change_id(repo, head, "master", env=env)
    assert len(seen) == 3 and all(e is env for e in seen)
