import os
import subprocess

from claude_ops import slots


def profile(root, name, target=None):
    d = root / name
    d.mkdir(parents=True)
    if target:
        os.symlink(target, d / "SingletonLock")
    return d


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_states(tmp_path):
    profile(tmp_path, "playwright-1")
    profile(tmp_path, "playwright-2", f"host-{os.getpid()}")
    profile(tmp_path, "playwright-3", f"host-{dead_pid()}")
    profile(tmp_path, "playwright-10", "garbage")
    got = {s["slot"]: s["state"] for s in slots.status(tmp_path)}
    assert got == {"playwright-1": "free", "playwright-2": "in-use", "playwright-3": "stale", "playwright-10": "stale"}
    assert [s["slot"] for s in slots.status(tmp_path)][-1] == "playwright-10"  # natural order


def test_a_held_lock_is_seen_though_its_target_does_not_exist(tmp_path):
    d = profile(tmp_path, "p1", f"host-{os.getpid()}")
    assert not (d / "SingletonLock").exists()  # the trap
    assert slots.read(d)["state"] == "in-use"


def test_release_stale_keeps_live_locks(tmp_path):
    profile(tmp_path, "playwright-1", f"host-{os.getpid()}")
    profile(tmp_path, "playwright-2", f"host-{dead_pid()}")
    assert slots.first_free(tmp_path) is None
    assert slots.release_stale(tmp_path) == ["playwright-2"]
    assert slots.first_free(tmp_path) == "playwright-2"
    assert slots.read(tmp_path / "playwright-1")["state"] == "in-use"
