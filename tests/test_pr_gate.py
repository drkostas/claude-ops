import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from claude_ops import pr_gate as pg

REPO = "someone/app"


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *map(str, args)], cwd=cwd,
                       capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """An origin with a pull request ref 7, a clone of it, a fake python3 that says 3.11, and a gate home."""
    origin, checkout, work = tmp_path / "origin.git", tmp_path / "clone", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", origin)
    git(tmp_path, "clone", "-q", origin, work)
    (work / "app.txt").write_text("base\n")
    git(work, "add", "app.txt")
    git(work, "commit", "-q", "-m", "base")
    git(work, "push", "-q", "origin", "HEAD:main")
    (work / ".github" / "workflows").mkdir(parents=True)
    (work / ".github" / "workflows" / "ci.yml").write_text("env:\n  PYTHON_VERSION: \"3.11\"\n")
    (work / "app.txt").write_text("the change\n")
    git(work, "add", "app.txt", ".github/workflows/ci.yml")
    git(work, "commit", "-q", "-m", "the change")
    sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", "HEAD:refs/pull/7/head")
    git(tmp_path, "clone", "-q", origin, checkout)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    py = fakebin / "python3"
    py.write_text("#!/bin/sh\necho \"${FAKE_PYTHON:-3.11}\"\n")
    py.chmod(0o755)
    monkeypatch.setenv("PATH", f"{os.pathsep.join(['/usr/bin', '/bin'])}")
    return {"tmp": tmp_path, "checkout": checkout, "sha": sha, "bin": fakebin, "home": tmp_path / "home",
            "work": work}


class GH:
    """gh pr view as a stand-in: each call answers the next state in `states` (the last one repeats)."""

    def __init__(self, *states):
        self.states, self.calls = list(states), 0

    def __call__(self, args):
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        return subprocess.CompletedProcess(args, 0, json.dumps(state), "")


def pr(sha, state="OPEN", fork=False):
    return {"state": state, "headRefOid": sha, "isCrossRepository": fork}


def run(world, gh=None, **kw):
    said = []
    kw.setdefault("path", str(world["bin"]))
    code, report = pg.gate(REPO, "7", checkout=world["checkout"], home=world["home"],
                           gh=gh or GH(pr(world["sha"])), say=said.append, **kw)
    return code, report, said


def trees(world):
    folder = world["home"] / "trees"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_a_green_run_tests_the_prs_own_head_in_order_and_leaves_nothing(world):
    out = world["tmp"] / "seen"
    code, report, said = run(world, python_from=".github/workflows/ci.yml",
                             lint=[f"echo lint >> {out}"], canary=f"echo canary >> {out}",
                             tests=[f"git rev-parse HEAD >> {out}", f"cat app.txt >> {out}"])
    assert code == pg.GREEN, said
    assert out.read_text().splitlines() == ["lint", "canary", world["sha"], "the change"]
    assert [s["kind"] for s in report["steps"]] == ["lint", "canary", "test", "test"]
    assert report["head"] == world["sha"] and report["exit"] == 0
    assert trees(world) == []                                       # the worktree is gone
    assert git(world["checkout"], "worktree", "list").count("\n") == 0   # and git no longer lists it
    assert len(pg.canary_times(world["home"], REPO)) == 1            # the canary's time is recorded


def test_a_pull_request_from_a_fork_is_refused_before_any_fetch(world):
    code, report, said = run(world, gh=GH(pr(world["sha"], fork=True)), tests=["true"])
    assert code == pg.FORK and "fork" in report["why"] and report["steps"] == []
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "FETCH_HEAD"], cwd=world["checkout"],
                          capture_output=True).returncode != 0          # nothing was fetched
    code, report, said = run(world, gh=GH(pr(world["sha"], fork=True)), tests=["true"], allow_fork=True)
    assert code == pg.GREEN, said


def test_a_closed_pull_request_runs_nothing(world):
    code, report, _ = run(world, gh=GH(pr(world["sha"], state="MERGED")), tests=["true"])
    assert code == pg.NOT_OPEN and report["steps"] == []


def test_env_file_values_reach_the_commands_and_are_never_printed(world, capsys):
    secret = "pw-" + uuid.uuid4().hex                  # built here, so no literal looks like a key
    env_file = world["tmp"] / "test.env"
    env_file.write_text(f"# the test database\nexport DB_PASSWORD=\"{secret}\"\nDB_HOST=localhost\n\n")
    out = world["tmp"] / "out"
    code, report, said = run(world, env_file=str(env_file), lint=["true"],
                             tests=[f"printf %s \"$DB_PASSWORD\" > {out}", "test \"$DB_HOST\" = localhost"])
    assert code == pg.GREEN, said
    assert out.read_text() == secret                   # it reached the command's environment
    printed = "\n".join(said) + json.dumps(report) + capsys.readouterr().out
    logs = "".join(p.read_text() for p in (world["home"] / "logs").rglob("*.log"))
    assert secret not in printed and secret not in logs
    assert any("values not shown" in s for s in said)


def test_the_python_the_workflow_names_must_be_the_commands_python(world, monkeypatch):
    monkeypatch.setenv("FAKE_PYTHON", "3.12")
    code, report, said = run(world, python_from=".github/workflows/ci.yml", tests=["true"])
    assert code == pg.SETUP and "3.11" in report["why"] and "python3 3.12" in report["why"]
    assert report["steps"] == [] and trees(world) == []
    assert pg.workflow_python(world["work"] / ".github" / "workflows" / "ci.yml") == "3.11"


def test_the_lock_is_held_by_a_process_and_let_go_when_its_stdin_closes(world):
    mark = world["tmp"] / "lock-mark"
    holder = f"echo held; cat > /dev/null; echo released >> {mark}"
    code, report, said = run(world, lock=holder, tests=[f"test ! -e {mark}"])
    assert code == pg.GREEN, said                       # the test ran while the lock was held
    assert mark.read_text() == "released\n"
    code, report, said = run(world, lock="exit 3", tests=["true"])
    assert code == pg.NO_LOCK and "ended with exit 3" in report["why"] and report["steps"] == []
    code, report, said = run(world, lock="sleep 30", lock_wait_s=0.5, tests=["true"])
    assert code == pg.NO_LOCK and "not held" in report["why"] and report["steps"] == []


def test_a_slow_canary_is_the_machine_and_a_red_one_the_code(world):
    code, report, said = run(world, canary="sleep 3", canary_limit_s=0.5, tests=["true"])
    assert code == pg.MACHINE and "machine" in report["why"] and [s["kind"] for s in report["steps"]] == ["canary"]
    code, report, said = run(world, canary="false", tests=["true"])
    assert code == pg.RED and [s["kind"] for s in report["steps"]] == ["canary"]
    assert pg.canary_limit([10, 8, 30]) == 60 and pg.canary_limit([20]) == 80 and pg.canary_limit([]) is None


def test_a_test_past_its_budget_has_its_process_group_ended(world):
    pidfile = world["tmp"] / "child.pid"
    code, report, said = run(world, tests=[f"sleep 60 & echo $! > {pidfile}; wait"], budget_s=1)
    assert code == pg.MACHINE and report["steps"][0]["over"] and report["steps"][0]["rc"] is None
    pid = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                 # the command's own child was ended too


def test_a_red_test_is_red_and_the_others_still_run(world):
    out = world["tmp"] / "after"
    code, report, said = run(world, tests=["false", f"touch {out}"])
    assert code == pg.RED and "`false`" in report["why"] and out.exists()
    code, report, said = run(world, lint=["false"], tests=[f"touch {out}2"])
    assert code == pg.RED and "lint" in report["why"] and not Path(f"{out}2").exists()


def test_a_head_that_moved_while_it_ran_is_said(world):
    code, report, said = run(world, gh=GH(pr(world["sha"]), pr("f" * 40)), tests=["true"])
    assert code == pg.MOVED and "f" * 12 in report["why"] and "(green)" in report["why"]


def test_leftovers_of_ended_runs_are_removed_and_nothing_else(world):
    folder = world["home"] / "trees"
    folder.mkdir(parents=True)
    dead = folder / f"run-{2 ** 22 + 12345}-{int(time.time())}"      # a pid no process has
    git(world["checkout"], "worktree", "add", "-q", "--detach", dead, "HEAD")
    alive = folder / f"run-{os.getppid()}-{int(time.time())}"         # a live run of another gate
    alive.mkdir()
    other = folder / "notes"
    other.mkdir()
    code, report, said = run(world, tests=["true"])
    assert code == pg.GREEN, said
    assert sorted(p.name for p in folder.iterdir()) == sorted([alive.name, "notes"])
    assert str(dead) not in git(world["checkout"], "worktree", "list")
    assert any("ended runs" in s for s in said)


def test_sigterm_and_sigkill_let_the_lock_go(world):
    """As a separate process, through the command line, with gh as a stand-in program on PATH."""
    fake_gh = world["bin"] / "gh"
    fake_gh.write_text(f"#!/bin/sh\necho '{json.dumps(pr(world['sha']))}'\n")
    fake_gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{world['bin']}{os.pathsep}/usr/bin{os.pathsep}/bin",
           "CLAUDE_OPS_PR_GATE_HOME": str(world["home"]), "PYTHONPATH": str(Path(pg.__file__).parents[1])}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        mark, started = world["tmp"] / f"released-{sig}", world["tmp"] / f"started-{sig}"
        child = world["tmp"] / f"child-{sig}"
        cmd = [sys.executable, "-m", "claude_ops.pr_gate", "7", "--repo", REPO, "--checkout", str(world["checkout"]),
               "--path", str(world["bin"]), "--lock", f"echo held; cat > /dev/null; echo released >> {mark}",
               "--test", f"echo $$ > {child}; touch {started}; exec sleep 60"]
        p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(200):
            if started.exists():
                break
            time.sleep(0.05)
        assert started.exists(), p.stdout.read() if p.poll() is not None else "the test never started"
        p.send_signal(sig)
        p.wait(timeout=30)
        for _ in range(100):
            if mark.exists():
                break
            time.sleep(0.05)
        assert mark.read_text() == "released\n", sig      # the holder let go, even after a SIGKILL
        if sig == signal.SIGTERM:
            assert p.returncode == pg.STOPPED and trees(world) == []
        else:
            assert len(trees(world)) == 1                   # a killed run leaves its worktree for the next run
            os.kill(int(child.read_text()), signal.SIGTERM)  # and its command, which no one is left to end
    code, report, said = run(world, tests=["true"])
    assert code == pg.GREEN and trees(world) == []


def test_read_env_file_takes_export_quotes_and_comments(tmp_path):
    f = tmp_path / "e"
    f.write_text("# c\nexport A=1\nB='two words'\nC=\"x=y\"\nnot a line\n 1BAD=2\n\n")
    assert pg.read_env_file(f) == {"A": "1", "B": "two words", "C": "x=y"}
