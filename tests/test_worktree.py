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


@pytest.mark.parametrize("name", ["latent_space", "Fix", "drlab-core"])
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
