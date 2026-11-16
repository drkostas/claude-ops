"""A heavy job the machine swapped out is said once, told to HEAVY_HOOK as `swapped`, and kept in its sample,
so a scheduler that counts heavy jobs can count it. The sampled sizes and the swap reading stand in for a
real job's, so every figure is the test's own."""
import importlib
import os
import signal
import subprocess
import sys
import time

import pytest


def _fresh(monkeypatch, tmp_path, **extra):
    """claude_ops.heavy imported again with its own state folder and these settings, and nothing else of
    HEAVY_* from the caller's environment."""
    for k in [k for k in os.environ if k.startswith("HEAVY_")]:
        monkeypatch.delenv(k)
    for k, v in {"HEAVY_DIR": str(tmp_path / "heavy"), "HEAVY_POLL": "0.2", **extra}.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("claude_ops.heavy", None)
    return importlib.import_module("claude_ops.heavy")


def test_the_words_come_only_when_resident_falls_under_a_quarter_of_its_peak_while_swap_grows(monkeypatch, tmp_path):
    h = _fresh(monkeypatch, tmp_path)
    line = h.swapped_words("emulator job", 0.27, 3.0, 3.0, 26.0, 21.0)
    assert line is not None and line.startswith("emulator job was swapped out: 0.27 GB of its 3 GB resident peak")
    assert "swap grew 5.0 GB since that peak" in line
    assert h.swapped_words("emulator job", 0.27, 3.0, 3.0, 21.0, 21.0) is None, "swap that did not grow says nothing"
    assert h.swapped_words("emulator job", 0.8, 3.0, 3.0, 26.0, 21.0) is None, "over a quarter of the peak is not swapped out"
    assert h.swapped_words("small job", 0.1, 0.9, 0.9, 26.0, 21.0) is None, "a peak under HEAVY_SWAPPED_MIN_GB is never judged"
    assert h.swapped_words("build job", 0.27, 3.0, 0.3, 26.0, 21.0) is None, \
        "a job whose footprint fell with its resident size let its memory go, it was not swapped out"
    h = _fresh(monkeypatch, tmp_path, HEAVY_SWAPPED_SHARE="0.5", HEAVY_SWAPPED_MIN_GB="0.5")
    assert h.swapped_words("small job", 0.4, 0.9, 0.9, 26.0, 21.0) is not None, "both limits are settings"


def _run(h, monkeypatch, tmp_path, resident: list[float], swap: list[float], seconds: float = 1.6,
         footprint: list[float] | None = None):
    """Run a job whose samples read `resident` (and `footprint`, else 3 GB) in turn, then their last value,
    and whose swap reads `swap` in turn. Returns what it said, the hook's lines and every sample it wrote."""
    said, samples = [], []
    monkeypatch.setattr(h, "say", said.append)
    res, sw, foot = list(resident), list(swap), list(footprint or [3.0])
    monkeypatch.setattr(h, "group_memory", lambda _pgid, _seen=None: (
        foot.pop(0) if len(foot) > 1 else foot[0], res.pop(0) if len(res) > 1 else res[0]))
    monkeypatch.setattr(h, "swap_gb", lambda: sw.pop(0) if len(sw) > 1 else sw[0])
    monkeypatch.setattr(h, "peak_of_children", lambda: 3.0)
    real_taking = h.taking

    def keep(pid, now_gb, peak_gb, *rest):
        samples.append(rest)
        real_taking(pid, now_gb, peak_gb, *rest)
    monkeypatch.setattr(h, "taking", keep)
    told = tmp_path / "told.txt"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'printf "%s\\n" "$*" >> "{told}"\n')
    monkeypatch.setenv("HEAVY_HOOK", f"bash {hook}")
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        assert h.run("t-chat", "invented emulator job", ["sleep", str(seconds)]) == 0
    finally:
        for s, f in handlers.items():
            signal.signal(s, f)
    lines = told.read_text().splitlines() if told.exists() else []
    return said, lines, samples


def test_a_job_swapped_out_says_so_once_and_tells_the_hook(monkeypatch, tmp_path):
    h = _fresh(monkeypatch, tmp_path, HEAVY_FAKE_MACHINE="20 2 50", HEAVY_CHECK="0.05")
    try:
        said, told, samples = _run(h, monkeypatch, tmp_path, resident=[1.0, 3.0, 3.0, 0.27],
                                   swap=[20.0, 21.0, 26.0])
    finally:
        sys.modules.pop("claude_ops.heavy", None)
    out = [x for x in said if "was swapped out" in x]
    assert len(out) == 1, said
    assert out[0].startswith("invented emulator job was swapped out: 0.27 GB of its 3 GB resident peak"), out
    assert "swap grew" in out[0], out
    swapped = [x for x in told if x.startswith("swapped ")]
    assert len(swapped) == 1 and swapped[0].startswith("swapped t-chat invented emulator job "), told
    assert samples[0] == (1.0, 1.0, False) and (3.0, 3.0, False) in samples, samples[:4]
    assert samples[-1] == (0.27, 3.0, True), "each sample after it carries the resident size, its peak and the flag"


def test_a_job_that_comes_back_into_memory_is_said_again_only_after_a_second_swap_out(monkeypatch, tmp_path):
    h = _fresh(monkeypatch, tmp_path, HEAVY_FAKE_MACHINE="20 2 50", HEAVY_CHECK="0.05")
    try:
        said, told, samples = _run(h, monkeypatch, tmp_path,
                                   resident=[3.0, 0.5, 0.5, 0.5, 2.0, 0.4],
                                   swap=[20.0, 22.0, 22.0, 22.0, 25.0], seconds=2.0)
    finally:
        sys.modules.pop("claude_ops.heavy", None)
    assert len([x for x in said if "was swapped out" in x]) == 2, said
    assert len([x for x in told if x.startswith("swapped ")]) == 2, told
    assert (2.0, 3.0, False) in samples, "back above half its peak, the flag is down"


def test_a_job_that_stays_in_memory_or_is_small_says_nothing(monkeypatch, tmp_path):
    h = _fresh(monkeypatch, tmp_path, HEAVY_FAKE_MACHINE="20 2 50", HEAVY_CHECK="0.05")
    try:
        said, told, _ = _run(h, monkeypatch, tmp_path, resident=[3.0, 2.5, 2.9], swap=[20.0, 26.0], seconds=0.8)
        assert not [x for x in said if "was swapped out" in x] and not [x for x in told if x.startswith("swapped")]
        said, told, _ = _run(h, monkeypatch, tmp_path, resident=[0.6, 0.05], swap=[20.0, 26.0], seconds=0.8)
        assert not [x for x in said if "was swapped out" in x], "a resident peak under 1 GB is never judged"
    finally:
        sys.modules.pop("claude_ops.heavy", None)


def test_a_job_that_lets_its_memory_go_is_never_called_swapped_out(monkeypatch, tmp_path):
    """Footprint and resident size fall together (a build whose compiler ended, a job that freed its arrays)
    while swap grows from other work: nothing is said and the hook is told nothing. And a job said to be
    swapped out that then lets its memory go is no longer marked so."""
    h = _fresh(monkeypatch, tmp_path, HEAVY_FAKE_MACHINE="20 2 50", HEAVY_CHECK="0.05")
    try:
        said, told, samples = _run(h, monkeypatch, tmp_path, resident=[3.0, 3.0, 0.27], footprint=[3.2, 3.2, 0.3],
                                   swap=[20.0, 26.0], seconds=0.8)
        assert not [x for x in said if "was swapped out" in x], said
        assert not [x for x in told if x.startswith("swapped")], told
        assert samples[-1] == (0.27, 3.0, False), samples[-3:]
        said, told, samples = _run(h, monkeypatch, tmp_path, resident=[3.0, 0.27, 0.27, 0.27, 0.27],
                                   footprint=[3.2, 3.2, 3.2, 3.2, 0.3], swap=[20.0, 26.0], seconds=1.2)
        assert len([x for x in said if "was swapped out" in x]) == 1, said
        assert (0.27, 3.0, True) in samples and samples[-1] == (0.27, 3.0, False), \
            "swapped out while its footprint stayed, and no longer once the footprint went"
    finally:
        sys.modules.pop("claude_ops.heavy", None)


def test_status_shows_the_holders_resident_figures(monkeypatch, tmp_path):
    h = _fresh(monkeypatch, tmp_path, HEAVY_FAKE_MACHINE="20 2 50")
    try:
        assert h.sampled(None) == {"resident_gb": None, "peak_resident_gb": None, "swapped_out": None}
        h.taking(4242, 3.0, 3.0, 0.27, 3.0, True)
        assert h.sampled({"pid": 4242}) == {"resident_gb": 0.27, "peak_resident_gb": 3.0, "swapped_out": True}
        h.taking(4243, 1.0, 1.0)
        assert h.sampled({"pid": 4243})["swapped_out"] is None, "a sample with no resident size says none"
    finally:
        sys.modules.pop("claude_ops.heavy", None)


def test_group_memory_reads_footprint_and_resident_over_the_same_processes(monkeypatch, tmp_path):
    h = _fresh(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(h.killguard, "process_table", lambda: {})
        monkeypatch.setattr(h, "job_tree", lambda _pgid, _table=None: ([10, 11], {12: 99}))
        monkeypatch.setattr(h, "still_running", lambda seen, _table=None: sorted(seen))
        asked = []
        monkeypatch.setattr(h, "total_gb", lambda pids: asked.append(("foot", list(pids))) or 3.04)
        monkeypatch.setattr(h, "total_resident_gb", lambda pids: asked.append(("res", list(pids))) or 0.274)
        seen = {}
        assert h.group_memory(10, seen) == (3.0, 0.27) and seen == {12: 99}
        assert asked == [("foot", [10, 11, 12]), ("res", [10, 11, 12])], asked
        assert h.group_gb(10, {}) == 3.0, "group_gb is the footprint half"
    finally:
        sys.modules.pop("claude_ops.heavy", None)


@pytest.mark.skipif(sys.platform != "darwin", reason="proc_pid_rusage is macOS only")
def test_resident_size_is_read_for_a_real_process():
    """The machine decides how much of a process stays in memory (under pressure a fresh allocation can go to
    swap at once), so only that both readings answer for a live process and neither for a gone one."""
    from claude_ops.footprint import footprint_gb, resident_gb
    p = subprocess.Popen([sys.executable, "-c", "import time; x = b'1' * (300 * 1024 * 1024); time.sleep(20)"],
                         start_new_session=True)
    try:
        for _ in range(100):
            if (footprint_gb(p.pid) or 0) > 0.25:
                break
            time.sleep(0.05)
        assert (footprint_gb(p.pid) or 0) > 0.25 and (resident_gb(p.pid) or 0) > 0
    finally:
        p.kill()
        p.wait()
    assert resident_gb(p.pid) is None and footprint_gb(p.pid) is None
