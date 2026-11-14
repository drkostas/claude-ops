"""The heavy-job queue: the turn across chats, the gate, the memory cap, holds for CI jobs, and the
clearing of what died. Every run uses its own state folder and stand-in machine readings."""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import claude_ops

ROOMY = "20 2 50"          # free GB, swap GB, disk %: a machine with room
#: the package under test, for the child processes: an installed claude-ops elsewhere must not answer
SRC = str(Path(claude_ops.__file__).resolve().parents[1])


def test_a_slot_given_back_while_it_is_looked_at_is_no_error(monkeypatch, tmp_path):
    """clear_stale finds the slot's folder, then reads its age. A holder that gives the slot back in
    between (its record goes first, then the folder) used to end the waiter that looked with
    FileNotFoundError, so that waiter's job never ran."""
    from claude_ops import heavy as h
    slot = tmp_path / "slot"
    slot.mkdir()
    monkeypatch.setattr(h, "SLOT", slot)

    def given_back():
        shutil.rmtree(slot)          # the release, between the look at the folder and the look at its age
        return None
    monkeypatch.setattr(h, "holder", given_back)
    h.clear_stale()
    assert not slot.exists()


def test_a_held_job_says_a_new_waiting_line_only_when_the_kinds_of_reason_change(monkeypatch, tmp_path):
    """The gate's words carry figures that move on almost every poll, and a job the gate held for
    hours printed a waiting line each time one moved. A held job now says a line when a kind of reason
    comes or goes, and the turn's words (who holds the slot) once while they stay the same."""
    for k in [k for k in os.environ if k.startswith("HEAVY_")]:
        monkeypatch.delenv(k)
    for k, v in {"HEAVY_DIR": str(tmp_path), "HEAVY_MIN_FREE_GB": "6", "HEAVY_MAX_SWAP_GB": "14",
                 "HEAVY_SWAP_DISK_FACTOR": "3", "HEAVY_MIN_DISK_PCT": "10"}.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("claude_ops.heavy", None)
    h = importlib.import_module("claude_ops.heavy")
    monkeypatch.setattr(h, "POLL", 0)
    said = []
    monkeypatch.setattr(h, "say", said.append)
    h.SLOT.mkdir(parents=True)
    (h.SLOT / "who.json").write_text(json.dumps(
        {"chat": "other", "what": "build", "pid": os.getpid(), "since": "00:00", "at": time.time()}))
    roomy = (20, 2, 50, 1, 500)
    readings = [roomy, roomy, roomy,              # the slot is held: one line for the three polls
                (5.9, 2, 50, 1, 500),              # the holder is gone and memory is short: a line
                (5.8, 2, 50, 1, 500),              # the same kind with other figures: no line
                (5.7, 2, 50, 1, 500),
                (5.6, 19, 50, 1, 50),              # swap comes too: a line
                (5.5, 19.2, 50, 1, 50),            # the same two kinds: no line
                (5.4, 2, 50, 1, 500),              # swap goes: a line
                roomy]                             # room, and the slot is free: it takes the slot
    calls = []

    def machine():
        calls.append(1)
        if len(calls) == 4:
            shutil.rmtree(h.SLOT)
        return readings[min(len(calls), len(readings)) - 1]
    monkeypatch.setattr(h, "machine", machine)

    assert h.wait_turn("t", "quiet", os.getpid())
    assert len(calls) == len(readings), f"it took the slot at the first reading with room ({len(calls)})"
    assert len(said) == 4, f"one line for each change of what holds it, not for each reading: {said}"
    assert "slot is held by other: build" in said[0]
    assert "5.9 GB of memory free" in said[1] and "swap" not in said[1]
    assert "5.7" not in " ".join(said) and "19.2" not in " ".join(said), \
        f"a figure moving under the same kinds says nothing: {said}"
    assert "5.6 GB of memory free" in said[2] and "swap at 19" in said[2]
    assert "5.4 GB of memory free" in said[3] and "swap" not in said[3]
    assert h.wait_key([("memory", "a"), ("swap", "b")], "") == h.wait_key([("swap", "c"), ("memory", "d")], "")
    assert h.wait_key([], "the slot is held by a: b") != h.wait_key([], "the slot is held by c: d")
    h.release(os.getpid())
    sys.modules.pop("claude_ops.heavy", None)


def test_a_waiter_beside_a_job_past_its_turn_counts_the_room_that_job_may_take_again(tmp_path):
    """A build past its turn that shrank between two steps let a second job in, then grew back, and both
    crawled on swap. A waiter beside it counts the room it may take again (its sampled peak less what it
    uses now) as memory that is not free."""
    state = tmp_path / "heavy"
    holder = subprocess.Popen(["sleep", "120"])
    try:
        (state / "slot").mkdir(parents=True)
        (state / "slot" / "who.json").write_text(json.dumps(
            {"chat": "s-chat", "what": "big build", "pid": holder.pid, "since": "03:21", "at": time.time() - 7200}))
        (state / "taking").mkdir()
        sample = state / "taking" / f"{holder.pid}.json"
        sample.write_text(json.dumps({"now_gb": 2.0, "peak_gb": 9.0, "at": time.time()}))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
               "HEAVY_DIR": str(state), "HEAVY_FAKE_MACHINE": "10 0 50", "HEAVY_POLL": "0.2", "PYTHONPATH": SRC,
               "HEAVY_LOG": str(tmp_path / "heavy.log")}
        waiter = subprocess.Popen([sys.executable, "-m", "claude_ops.heavy", "t-chat", "second", "--", "echo", "ran"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        time.sleep(2)
        assert waiter.poll() is None, "with 10 GB free and 7 GB the build may take again, the second job waits"
        sample.write_text(json.dumps({"now_gb": 8.5, "peak_gb": 9.0, "at": time.time()}))
        out = waiter.communicate(timeout=30)[0]
        assert ("waiting, only 3 GB of memory free once s-chat: big build, past its turn, takes back the 7 GB "
                "under its peak of 9 GB (needs 6)") in out, out
        assert waiter.returncode == 0 and "ran" in out and "past its turn" in out, out
        # ⭐ the same line is in HEAVY_LOG, with the waiter's pid, so a reader of one file sees every job's take-back
        logged = (tmp_path / "heavy.log").read_text().splitlines()
        assert any(f"pid={waiter.pid} waiting, only 3 GB of memory free once s-chat: big build, past its turn, "
                   "takes back the 7 GB" in ln for ln in logged), logged
        # without a sample (a job from before samples were written) it starts beside it as before
        sample.unlink()
        (state / "slot").mkdir(exist_ok=True)
        (state / "slot" / "who.json").write_text(json.dumps(
            {"chat": "s-chat", "what": "big build", "pid": holder.pid, "since": "03:21", "at": time.time() - 7200}))
        r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "t-chat", "third", "--", "echo", "ran3"],
                           capture_output=True, text=True, timeout=30, env=env)
        assert r.returncode == 0 and "ran3" in r.stdout + r.stderr and "takes back" not in r.stdout + r.stderr
    finally:
        holder.kill()
        holder.wait()


def test_a_job_that_gave_the_slot_up_still_counts_for_the_next_waiter(tmp_path):
    """Once a waiter takes over, the job past its turn moves to past/<pid>.json and the slot's holder is within its
    turn. A third waiter still counts what the job in past/ may take again, and every such job together."""
    state = tmp_path / "heavy"
    old, new, other = (subprocess.Popen(["sleep", "120"]) for _ in range(3))
    try:
        (state / "slot").mkdir(parents=True)
        (state / "slot" / "who.json").write_text(json.dumps(
            {"chat": "n-chat", "what": "newer job", "pid": new.pid, "since": "05:21", "at": time.time() - 60}))
        (state / "past").mkdir()
        (state / "past" / f"{old.pid}.json").write_text(json.dumps(
            {"chat": "s-chat", "what": "big build", "pid": old.pid, "since": "03:21", "at": time.time() - 7200}))
        (state / "taking").mkdir()
        sample = state / "taking" / f"{old.pid}.json"
        sample.write_text(json.dumps({"now_gb": 2.0, "peak_gb": 9.0, "at": time.time()}))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
               "HEAVY_DIR": str(state), "HEAVY_FAKE_MACHINE": "10 0 50", "HEAVY_POLL": "0.2", "PYTHONPATH": SRC,
               "HEAVY_TURN_MIN": "60"}
        os.environ["HEAVY_DIR"] = str(state)   # the words for two jobs, read in this process, with this folder
        sys.modules.pop("claude_ops.heavy", None)
        h = importlib.import_module("claude_ops.heavy")
        (state / "past" / f"{other.pid}.json").write_text(json.dumps(
            {"chat": "o-chat", "what": "other build", "pid": other.pid, "since": "04:00", "at": time.time() - 5000}))
        (state / "taking" / f"{other.pid}.json").write_text(json.dumps({"now_gb": 3.0, "peak_gb": 4.0, "at": time.time()}))
        back, words = h.taking_back(h.holder())
        assert back == 8.0 and words == ("2 jobs past their turn take back 8 GB, the most s-chat: big build with 7 GB "
                                         "under its peak of 9 GB"), (back, words)
        (state / "past" / f"{other.pid}.json").unlink()
        (state / "taking" / f"{other.pid}.json").unlink()
        # the newer job holds the slot within its turn, so the waiter waits for the slot; once it is gone, the
        # waiter would take it, and the job in past/ still counts
        new.kill(); new.wait()
        waiter = subprocess.Popen([sys.executable, "-m", "claude_ops.heavy", "t-chat", "third", "--", "echo", "ran"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        time.sleep(2)
        assert waiter.poll() is None, "with 10 GB free and 7 GB the job in past/ may take again, the third job waits"
        sample.write_text(json.dumps({"now_gb": 9.0, "peak_gb": 9.0, "at": time.time()}))
        out = waiter.communicate(timeout=30)[0]
        assert ("only 3 GB of memory free once s-chat: big build, past its turn, takes back the 7 GB under its "
                "peak of 9 GB (needs 6)") in out, out
        assert waiter.returncode == 0 and "ran" in out, out
    finally:
        os.environ.pop("HEAVY_DIR", None)
        sys.modules.pop("claude_ops.heavy", None)   # the tests after import it again with their own folder
        for p in (old, new, other):
            p.kill()
            p.wait()


def test_a_running_job_writes_its_sample_and_takes_it_away(tmp_path):
    """What a waiter reads: the job's memory now and its peak so far, while it runs, and nothing after."""
    state = tmp_path / "heavy"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "HEAVY_DIR": str(state), "HEAVY_FAKE_MACHINE": ROOMY, "HEAVY_POLL": "0.2", "HEAVY_CHECK": "0.2",
           "PYTHONPATH": SRC}
    job = subprocess.Popen([sys.executable, "-m", "claude_ops.heavy", "t-chat", "sampled", "--", "sleep", "3"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    seen = None
    for _ in range(100):
        files = list((state / "taking").glob("*.json")) if (state / "taking").is_dir() else []
        if files:
            seen = json.loads(files[0].read_text())
            break
        time.sleep(0.05)
    job.communicate(timeout=30)
    assert seen is not None and {"now_gb", "peak_gb", "at"} <= set(seen) and seen["peak_gb"] >= seen["now_gb"], seen
    assert not list((state / "taking").glob("*.json")), "and the sample goes when the job ends"
    # one left by a job killed before it could remove it goes at the next look
    dead = subprocess.Popen(["true"])
    dead.wait()
    (state / "taking" / f"{dead.pid}.json").write_text(json.dumps({"now_gb": 1, "peak_gb": 5, "at": 0}))
    r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "t-chat", "after", "--", "true"],
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0 and not (state / "taking" / f"{dead.pid}.json").exists()


def _fresh(monkeypatch, tmp_path, **extra):
    """claude_ops.heavy imported again with its own state folder and these settings, and nothing else of
    HEAVY_* from the caller's environment."""
    for k in [k for k in os.environ if k.startswith("HEAVY_")]:
        monkeypatch.delenv(k)
    for k, v in {"HEAVY_DIR": str(tmp_path / "heavy"), "HEAVY_POLL": "0.2", **extra}.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("claude_ops.heavy", None)
    return importlib.import_module("claude_ops.heavy")


def test_a_declared_need_is_held_between_its_floor_and_the_cap_and_the_gate_asks_it(monkeypatch, tmp_path):
    """A job that knows its size says so, and the gate asks that much free memory instead of
    HEAVY_MIN_FREE_GB. A need below the floor or over the cap is held to them, and one that is not a number
    is said and left out."""
    h = _fresh(monkeypatch, tmp_path, HEAVY_MIN_FREE_GB="6", HEAVY_MAX_GB="8")
    said = []
    monkeypatch.setattr(h, "say", said.append)
    assert h.declared_need() == (None, None) and h.gate_gb("sheet render", None) == 6
    assert h.declared_need("1.5") == (1.5, ("--need-gb", "--need-gb 1.5"))
    monkeypatch.setenv("HEAVY_NEED_GB", "2")
    assert h.declared_need() == (2.0, ("HEAVY_NEED_GB", "HEAVY_NEED_GB=2"))
    assert h.declared_need("1") == (1.0, ("--need-gb", "--need-gb 1")), "--need-gb is read before HEAVY_NEED_GB"
    assert not said
    assert h.declared_need("0.1")[0] == 0.5 and "held to 0.5 GB" in said[-1]
    assert h.declared_need("20")[0] == 8 and "8 GB memory cap" in said[-1]
    for bad in ("lots", "nan", "inf"):
        assert h.declared_need(bad) == (None, None) and "is not a number of GB" in said[-1], bad
    tight = (4.4, 0, 50, 1, 500)
    assert h.reasons(tight) == [("memory", "only 4.4 GB of memory free (needs 6)")]
    assert h.reasons(tight, h.gate_gb("sheet render", 1.5)) == [], "a job that needs 1.5 GB has room in 4.4"
    assert h.reasons(tight, 5) == [("memory", "only 4.4 GB of memory free (needs 5)")]
    note = h.held_note("only 0.4 GB of memory free (needs 1)", {"memory"}, 61, 0.9, 1.0,
                       ("--need-gb", "--need-gb 1"))
    assert ("That is this job's own limit (--need-gb 1)" in note and "against the 1 GB it asks for" in note
            and "Lower --need-gb to what the job needs" in note), note
    monkeypatch.setenv("HEAVY_MIN_FREE_GB", "16")
    note = h.held_note("only 9 GB of memory free (needs 16)", {"memory"}, 61, 9, 16, None)
    assert "That is this job's own limit (HEAVY_MIN_FREE_GB=16)" in note and "HEAVY_NEED_GB" not in note, note


def test_a_peak_over_the_declared_need_is_said_and_the_next_run_of_that_name_waits_for_it(monkeypatch, tmp_path):
    """A declaration is not trusted blindly. A run that peaks more than a quarter over its need says so in one
    line, and the next run of that job name asks the larger of its need and that peak, never more than the
    cap. A run that stays within its need again drops the kept peak."""
    h = _fresh(monkeypatch, tmp_path, HEAVY_MIN_FREE_GB="6", HEAVY_MAX_GB="8")
    said = []
    monkeypatch.setattr(h, "say", said.append)
    assert h.judge_need("c", "bench parts", None, 3.0) is None and not h.PEAKS.exists(), "no need, no judgement"
    assert h.judge_need("c", "bench parts", 1.0, 1.25) is None and h.gate_gb("bench parts", 1.0) == 1.0, \
        "a quarter over is still within"
    line = h.judge_need("c", "bench parts", 1.0, 1.4)
    assert line == ("bench parts took 1.4 GB, more than a quarter over the 1 GB it declares, so its next run waits "
                    "for 1.4 GB free. Declare what it takes, or make it use less.") and said[-1] == line, line
    assert h.kept_peak("bench parts") == 1.4 and h.gate_gb("bench parts", 1.0) == 1.4
    assert h.gate_gb("bench parts", 2.0) == 2.0, "a need larger than the kept peak is asked as it is"
    assert h.gate_gb("bench sheets", 1.0) == 1.0, "a peak is kept per job name"
    assert h.judge_need("c", "bench parts", 1.0, 0.9) is None and h.kept_peak("bench parts") == 0
    assert "within the 1 GB it declares, so its next run asks 1 GB again" in said[-1]
    h.judge_need("c", "bench parts", 1.0, 20.0)
    assert h.gate_gb("bench parts", 1.0) == 8, "never more than the memory cap"


def test_a_job_that_declares_its_need_starts_where_an_undeclared_one_waits(tmp_path):
    """With 4.4 GB free an undeclared job waits for 6, and the same job declaring 1 GB runs at once, by
    --need-gb or by HEAVY_NEED_GB. What it declared is kept with what it took, and a waiter's ask shows in
    status."""
    state = tmp_path / "heavy"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "HEAVY_DIR": str(state), "HEAVY_FAKE_MACHINE": "4.4 0 50", "HEAVY_POLL": "0.2", "PYTHONPATH": SRC}

    def heavy(*args, **extra):
        return subprocess.run([sys.executable, "-m", "claude_ops.heavy", *args], capture_output=True,
                              text=True, timeout=60, env={**env, **extra})

    undeclared = subprocess.Popen([sys.executable, "-m", "claude_ops.heavy", "t-chat", "small part", "--",
                                   "echo", "ran"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  env=env)
    try:
        time.sleep(1.5)
        assert undeclared.poll() is None, "an undeclared job waits for 6 GB with 4.4 free"
    finally:
        undeclared.terminate()
        out = undeclared.communicate(timeout=30)[0]
    assert "only 4.4 GB of memory free (needs 6)" in out, out
    r = heavy("t-chat", "small part", "--need-gb", "1", "--", "echo", "ran-declared")
    assert r.returncode == 0 and "ran-declared" in r.stdout and "declared need 1 GB" in r.stderr, r.stdout + r.stderr
    r = heavy("t-chat", "small part", "--", "echo", "ran-by-env", HEAVY_NEED_GB="1")
    assert r.returncode == 0 and "ran-by-env" in r.stdout, r.stdout + r.stderr
    done = [json.loads(f.read_text()) for f in (state / "done").glob("*.json")]
    assert sorted(d["need_gb"] for d in done) == [1.0, 1.0], done
    r = heavy("t-chat", "odd need", "--need-gb", "-", "--", "echo", "x", HEAVY_FAKE_MACHINE="20 0 50")
    assert r.returncode == 0 and "is not a number of GB" in r.stderr, "a need that is no number runs as undeclared"
    waiter = subprocess.Popen([sys.executable, "-m", "claude_ops.heavy", "t-chat", "small part", "--need-gb", "1",
                               "--", "echo", "ran"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              env={**env, "HEAVY_FAKE_MACHINE": "0.8 0 50"})
    try:
        for _ in range(100):
            st = heavy("status", HEAVY_FAKE_MACHINE="0.8 0 50").stdout
            if "asks 1 GB free" in st and "(needs 1)" in st:     # the ticket first, then its gate's words
                break
            time.sleep(0.1)
        assert "t-chat: small part" in st and "asks 1 GB free" in st and "(needs 1)" in st, st
    finally:
        waiter.terminate()
        waiter.communicate(timeout=30)


def test_a_run_over_its_need_makes_the_next_run_wait_for_its_peak(monkeypatch, tmp_path):
    """The whole path in one process: a run that declares 1 GB and peaks at 1.4 GB says so and keeps the
    peak, the next run of that name waits for 1.4 GB, and a run within its need asks the need again. The
    sampled memory stands in for a real job's, so the figures are the test's own."""
    import signal
    h = _fresh(monkeypatch, tmp_path, HEAVY_FAKE_MACHINE="20 0 50", HEAVY_CHECK="0.1")
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    said = []
    monkeypatch.setattr(h, "say", said.append)
    peak = [1.4]
    monkeypatch.setattr(h, "group_gb", lambda _pgid, _seen=None: peak[0])
    monkeypatch.setattr(h, "peak_of_children", lambda: peak[0])
    try:
        assert h.run("t-chat", "invented sheet job", ["sleep", "0.4"], need_gb="1") == 0
        assert any("invented sheet job took 1.4 GB, more than a quarter over the 1 GB it declares" in x
                   for x in said), said
        assert h.gate_gb("invented sheet job", 1.0) == 1.4
        monkeypatch.setenv("HEAVY_FAKE_MACHINE", "1.2 0 50")
        held = subprocess.Popen([sys.executable, "-m", "claude_ops.heavy", "t-chat", "invented sheet job",
                                 "--need-gb", "1", "--", "echo", "ran"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                env={**os.environ, "PYTHONPATH": SRC})
        try:
            time.sleep(1.5)
            assert held.poll() is None, "with 1.2 GB free the next run waits for the 1.4 GB its last run took"
        finally:
            held.terminate()
            out = held.communicate(timeout=30)[0]
        assert ("took 1.4 GB on its last run, over the 1 GB it declares, so it waits for 1.4 GB free" in out
                and "(needs 1.4)" in out), out
        monkeypatch.setenv("HEAVY_FAKE_MACHINE", "20 0 50")
        peak[0] = 0.9
        assert h.run("t-chat", "invented sheet job", ["true"], need_gb="1") == 0
        assert h.kept_peak("invented sheet job") == 0 and h.gate_gb("invented sheet job", 1.0) == 1.0
    finally:
        for s, f in handlers.items():
            signal.signal(s, f)
        sys.modules.pop("claude_ops.heavy", None)


def test_heavy_queue(monkeypatch):
    failures = []
    for k in [k for k in os.environ if k.startswith("HEAVY_")]:
        monkeypatch.delenv(k)
    STATE = Path(tempfile.mkdtemp(prefix="heavy-test-"))

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def env(**extra):
        e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
             "HEAVY_DIR": str(STATE), "HEAVY_FAKE_MACHINE": ROOMY, "HEAVY_POLL": "0.2", "PYTHONPATH": SRC}
        e.update(extra)
        return e

    def heavy(*args, timeout=60, **extra):
        r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", *args], capture_output=True,
                           text=True, timeout=timeout, env=env(**extra))
        return r.returncode, r.stdout + r.stderr

    def start(*args, **extra):
        return subprocess.Popen([sys.executable, "-m", "claude_ops.heavy", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env(**extra))

    def module(**extra):
        """claude_ops.heavy loaded again under these settings. `from claude_ops import heavy` would
        hand back the package's attribute, the module an earlier import made, with its folders and
        limits from that time, so it is imported by name."""
        for k, v in {"HEAVY_DIR": str(STATE), **extra}.items():
            monkeypatch.setenv(k, v)
        sys.modules.pop("claude_ops.heavy", None)
        return importlib.import_module("claude_ops.heavy")

    # --- a job runs, its output and exit code pass through, the slot comes back ------------------
    rc, out = heavy("t", "small", "--", "bash", "-c", "echo hello-from-job; exit 3")
    check(rc == 3 and "hello-from-job" in out, f"a job runs and its exit code comes back (rc={rc})")
    rc, out = heavy("status")
    check(rc == 0 and "slot: free" in out, "and the slot is free after it")
    rc, out = heavy("nonsense")
    check(rc == 2 and "usage" in out, "a call it cannot read says how to call it")

    # --- what a job took, and telling its chat --------------------------------------------------------
    rc, out = heavy("t", "alloc", "--", sys.executable, "-c",
                    "b = bytearray(300_000_000); b[::4096] = b'x' * len(b[::4096]); print('allocated')")
    done = sorted((STATE / "done").glob("*.json"))
    rec = json.loads(done[-1].read_text()) if done else {}
    check(rc == 0 and "alloc is done: exit 0, peak memory" in out and rec.get("what") == "alloc"
          and float(rec.get("peak_gb", 0)) >= 0.25 and rec.get("rc") == 0,
          f"⭐ a finished job says its peak memory, and done/<pid>.json keeps it: {out.strip()[-90:]!r} {rec}")
    told = STATE / "told.txt"
    fake = STATE / "later.sh"
    fake.write_text(f'printf "%s|" "$@" >> {told}; echo >> {told}\n')
    rc, out = heavy("n-chat", "render", "--notify", "--", "bash", "-c", "echo first; echo the-last-line; exit 5",
                    HEAVY_NOTIFY=f"bash {fake}")
    said = told.read_text() if told.exists() else ""
    check(rc == 5 and "the-last-line" in out
          and said.startswith("n-chat|From the heavy-job queue: render finished with exit 5")
          and "its last line: the-last-line" in said,
          f"--notify runs HEAVY_NOTIFY with the chat and one line: exit code, peak and last line: {said!r}")
    rc, out = heavy("n-chat", "both", "--max-hold", "5", "--notify", "--", "true", HEAVY_NOTIFY=f"bash {fake}")
    check(rc == 0 and told.read_text().count("n-chat|From") == 2, "--max-hold and --notify go together, in either order")
    rc, out = heavy("n-chat", "x", "--loud", "--", "true")
    check(rc == 2 and "usage" in out, "an option it does not know is refused")

    # --- ⭐ the same failure again and again is said ------------------------------------------------
    h = module(HEAVY_REPEAT_FAILS="3")
    def rec(what, end, rc=0, ended=None, chat="r-chat"):
        return {"chat": chat, "what": what, "rc": rc, "ended": ended, "end": end}
    cap = [rec("sheets", t, 137, f"{gb} GB, over the 8 GB cap") for t, gb in ((1, 9.4), (2, 8.8), (3, 9.9))]
    got = h.streaks(cap)
    check(len(got) == 1 and got[0]["count"] == 3 and got[0]["how"] == "over the 8 GB memory cap"
          and got[0]["first"] == 1 and got[0]["last"] == 3,
          f"⭐ three runs ended over the cap, at different peaks, are one failure three times: {got}")
    check(h.streaks(cap[:2]) == [], "two are not yet a run of failures")
    check(h.streaks([*cap, rec("sheets", 4)]) == [], "a success after them ends the run")
    check(h.streaks([rec("sheets", 0, 3), *cap]) and h.streaks([rec("sheets", 0, 3), *cap])[0]["count"] == 3,
          "a failure of another kind before them does not count")
    mixed = [rec("x", 1, 1), rec("x", 2, 2), rec("x", 3, 1)]
    check(h.streaks(mixed) == [], "exit codes that differ are not the same failure")
    check(h.streaks([rec("x", t, 1, chat=c) for t, c in ((1, "a"), (2, "b"), (3, "a"))]) == [],
          "the same job name from two chats is two jobs")
    check(h.streaks(list(reversed(cap)))[0]["count"] == 3, "the records may come in any order")
    check(h.failed_how(rec("m", 1, 124, "its --max-hold of 5 min")) == "ended at its --max-hold"
          and h.failed_how(rec("m", 1, 0)) is None, "a --max-hold end is its own kind, a success is none")

    # --- ⭐ the session that started a job is kept beside the name it was given ----------------------
    # A script may pass a project's name rather than a chat's; a reader then tells the conversation
    # from CLAUDE_CODE_SESSION_ID, which the job's records carry (an invented id here)
    sid = "0d0e0f10-1111-4222-8333-444455556666"
    held = start("a-project", "deploy build", "--", "sleep", "30", CLAUDE_CODE_SESSION_ID=sid)
    who, end = STATE / "slot" / "who.json", time.time() + 20
    while not who.exists() and time.time() < end:
        time.sleep(0.1)
    try:
        slot_rec = json.loads(who.read_text())
    except (OSError, ValueError):
        slot_rec = {}
    held.kill(); held.wait()
    check(slot_rec.get("chat") == "a-project" and slot_rec.get("session") == sid,
          f"⭐ the slot names the session that started the job beside the name it was given: {slot_rec}")
    rc, out = heavy("a-project", "deploy check", "--", "true", CLAUDE_CODE_SESSION_ID=sid)
    recs = [json.loads(f.read_text()) for f in sorted((STATE / "done").glob("*.json"), key=lambda f: f.stat().st_mtime)]
    mine = [r for r in recs if r.get("what") == "deploy check"]
    check(mine and mine[-1].get("session") == sid, f"and so does its done record: {mine[-1:]}")
    rc, out = heavy("a-project", "no session", "--", "true")
    mine = [json.loads(f.read_text()) for f in (STATE / "done").glob("*.json")]
    mine = [r for r in mine if r.get("what") == "no session"]
    check(mine and mine[-1].get("session") is None, "a job no Claude Code session started records none")
    got = h.streaks([{**r, "session": f"s{r['end']}"} for r in cap])
    check(got and got[0]["session"] == "s3", f"a run of failures names the session of its last run: {got}")
    check([n for n in range(1, 30) if h.worth_saying(n)] == [3, 6, 12, 24],
          "it is said at the third failure and each time the count doubles")
    told2 = STATE / "told-again.txt"
    fake2 = STATE / "later-again.sh"
    fake2.write_text(f'printf "%s|" "$@" >> {told2}; echo >> {told2}\n')
    outs = [heavy("r-chat", "flaky", "--notify", "--", "bash", "-c", "exit 7", HEAVY_NOTIFY=f"bash {fake2}")[1]
            for _ in range(3)]
    said = told2.read_text() if told2.exists() else ""
    check("AGAIN" not in outs[0] + outs[1] and "AGAIN: flaky has failed the same way 3 times in a row (exit 7)" in outs[2],
          f"⭐ the third run that fails the same way says so, the first two do not: {outs[2].strip()[-160:]!r}")
    check("r-chat|From the heavy-job queue: flaky has failed the same way 3 times in a row" in said,
          f"and with --notify its chat is told: {said[-200:]!r}")
    rc, st = heavy("status")
    check("failing the same way: r-chat: flaky (exit 7, 3 times in a row" in st,
          f"status lists it: {st.strip()[-160:]!r}")
    rc, st = heavy("status", HEAVY_REPEAT_HOURS="0")
    check("failing the same way" not in st, "only within HEAVY_REPEAT_HOURS")
    heavy("r-chat", "flaky", "--", "true")
    rc, st = heavy("status")
    check("failing the same way: r-chat: flaky" not in st, "and a success takes it off the list")
    recs = [j for j in h.done_jobs() if j["what"] == "flaky"]
    check(h.failing(folder=STATE / "done", within_hours=1, at_least=1)
          and h.failing(folder=STATE / "nowhere") == [], "a reader may name the done folder")
    check(len(recs) == 4 and all("end" in j and "how" in j for j in recs)
          and sum(j["how"] == "exit 7" for j in recs) == 3,
          f"each record keeps when it ended and how it failed (read from {h.DONE}): {recs}")

    # --- the gate ---------------------------------------------------------------------------------
    h = module(HEAVY_MIN_FREE_GB="6", HEAVY_MAX_SWAP_GB="14", HEAVY_MIN_DISK_PCT="10")
    check(h.short((20, 2, 50, 1, 500)) == "", "a machine with room is not short")
    why = h.short((3, 18.5, 8, 1, 40))
    check("memory free" in why and "swap at 18.5" in why and "disk at 8" in why,
          f"a short machine says each reason ({why})")
    check([k for k, _w in h.reasons((3, 18.5, 8, 4, 40))] == ["memory", "pressure", "swap", "disk"],
          "and each reason has its kind")
    check(h.short((20, 19, 50, 1, 500)) == "",
          "swap far over its limit with memory free and room on the disk holds nothing: swap is history")
    # ⭐ the room swap needs grows with the swap, not with the size of the disk: 115 GB free of a 1 TB
    # disk is 11.5%, and a share of the disk held every job there while 19 GB of swap could never fill it
    check(h.short((20, 17.3, 11.5, 1, 115)) == "",
          "⭐ high swap with the disk above 3 times the swap passes, whatever share of the disk that is")
    why = h.short((20, 19, 30, 1, 50))
    check("swap at 19" in why and "50 GB of disk free" in why and "needs 3 times the swap, 57 GB" in why,
          f"⭐ high swap with the disk under 3 times the swap holds, and says both sizes ({why})")
    check("disk at 9.9" in h.short((20, 2, 9.9, 1, 400)) and "disk at 9.9" in h.short((20, 19, 9.9, 1, 400)),
          "⭐ under HEAVY_MIN_DISK_PCT free always holds, whatever the swap and however many GB that is")
    check(h.short((20, 19, 11)) == "" and h.short((20, 19, 11, 1)) == "",
          "a reading without the disk's GB has nothing to compare the swap with, and holds nothing for swap")
    # each limit at its edge: a comparison moved by one (`<` to `<=`) must turn a check red
    check(h.short((6, 2, 50, 1, 500)) == "" and "memory free" in h.short((5.9, 2, 50, 1, 500)),
          "exactly HEAVY_MIN_FREE_GB free is enough, a little under is not")
    check(h.short((20, 2, 10, 1, 500)) == "" and "disk at 9.9" in h.short((20, 2, 9.9, 1, 500)),
          "exactly HEAVY_MIN_DISK_PCT free is enough, a little under is not")
    check(h.short((20, 14, 30, 1, 20)) == "" and "swap at 14.1" in h.short((20, 14.1, 30, 1, 20)),
          "swap exactly at HEAVY_MAX_SWAP_GB beside a short disk is allowed, a little over is not")
    check(h.short((20, 19, 30, 1, 57)) == "" and "swap at 19" in h.short((20, 19, 30, 1, 56.9)),
          "high swap beside exactly 3 times the swap free is allowed, a little under is not")
    check("critical" in h.short((20, 2, 50, 4, 500)) and h.short((20, 2, 50, 2, 500)) == "",
          "macOS rating the pressure critical holds a job; raised does not, the free memory decides")
    h = module(HEAVY_MAX_SWAP_GB="14", HEAVY_SWAP_DISK_FACTOR="2")
    check(h.short((20, 19, 30, 1, 38)) == "" and "needs 2 times the swap, 38 GB" in h.short((20, 19, 30, 1, 37.9)),
          "⭐ HEAVY_SWAP_DISK_FACTOR sets how many times the swap must be free, at its edge too")
    h = module()
    if sys.platform == "darwin":        # the memory readings come from macOS's own tools
        m = h.machine()
        check(len(m) == 5 and isinstance(m[4], float) and m[4] > 0,
              f"the machine's own reading has the data volume's free GB ({m})")
    # ⭐ the note after a long hold names what held the job, in the gate's own words. module() only sets, so the
    # limits the checks above set are taken away first: a setting in the environment is the job's own limit
    for k in ("HEAVY_MIN_FREE_GB", "HEAVY_MAX_SWAP_GB", "HEAVY_MIN_DISK_PCT", "HEAVY_SWAP_DISK_FACTOR"):
        monkeypatch.delenv(k, raising=False)
    h = module()
    swap_why = h.short((20, 19, 30, 1, 50))
    note = h.held_note(swap_why, {"swap"}, 400, 20)
    check(note.startswith("the gate has held this job for") and swap_why in note
          and "That is the machine, not a limit this job set" in note and "more free disk" in note
          and "memory free in that time" not in note and "HEAVY_MIN_FREE_GB" not in note,
          f"⭐ a job the machine's swap and disk held is told so, with no advice about memory ({note})")
    h = module(HEAVY_MIN_FREE_GB="50")
    note = h.held_note(h.short((10.6, 2, 50, 1, 500)), {"memory"}, 70, 10.6)
    check("this job's own limit (HEAVY_MIN_FREE_GB=50)" in note
          and "the most memory free in that time was 10.6 GB against the 50 GB it asks for" in note
          and "Lower HEAVY_MIN_FREE_GB" in note,
          f"⭐ a job held by a limit set in its own environment is told that, and which setting to lower ({note})")
    monkeypatch.delenv("HEAVY_MIN_FREE_GB")
    h = module()
    note = h.held_note(h.short((3, 2, 50, 1, 500)), {"memory"}, 70, 3.4)
    check("That is the machine" in note and "against the 6 GB it asks for" in note and "more free memory" in note,
          f"and a machine short of memory with the default limit is the machine ({note})")
    rc, out = heavy("t", "swap-history", "--", "echo", "ran-with-swap", HEAVY_FAKE_MACHINE="20 19 50")
    check(rc == 0 and "ran-with-swap" in out, "and such a job runs")
    p = start("t", "gated", "--", "true", HEAVY_FAKE_MACHINE="3 2 50")
    time.sleep(1.5)
    check(p.poll() is None, "⛔ a job does not start while memory is short")
    p.terminate(); p.wait(timeout=10)
    check(not list((STATE / "queue").glob("*.json")), "and a waiter that is stopped takes its ticket with it")

    # --- the cap: a job that grows past it is ended, and told why -----------------------------------
    grow = ("import time\nb=[]\nfor i in range(60):\n    b.append(bytearray(64*1024*1024)); "
            "b[-1][::4096]=b'x'*len(b[-1][::4096]); time.sleep(0.25)\nprint('finished')")
    if sys.platform == "darwin":        # the cap reads proc_pid_rusage
        rc, out = heavy("t", "grow", "--", sys.executable, "-c", grow, HEAVY_MAX_GB="0.4", HEAVY_CHECK="1",
                        timeout=120)
        check(rc == 137 and "ENDED grow" in out and "finished" not in out,
              f"a job that passes the memory cap is ended and told why (rc={rc}: {out.strip()[-160:]})")
        check(heavy("status")[1].count("slot: free") == 1, "and the slot is free after it")

    # --- ⭐ the turn: round robin across chats, oldest first within a chat ------------------------
    log = STATE / "order.log"
    go = STATE / "go"
    job = ["bash", "-c", f'echo "$0" >> {log}; sleep 0.6']

    def soon(cond, timeout=30.0):
        end = time.time() + timeout
        while time.time() < end and not cond():
            time.sleep(0.05)
        return cond()

    def waiting():
        return len(list((STATE / "queue").glob("*.json")))     # a job that took the slot has no ticket

    # busy-1 holds the slot until every job below is in the queue, however slow the machine is
    first = start("busy", "first", "--", "bash", "-c",
                  f'echo busy-1 >> {log}; until [ -e {go} ]; do sleep 0.1; done')
    check(soon(lambda: log.exists() and "busy-1" in log.read_text()), "busy-1 holds the slot")
    a = []
    for i in (1, 2, 3):
        # each only once the one before is in the queue: started 0.3 s apart on a slow runner,
        # a2's ticket was written before a1's, and a2 ran first
        a.append(start("chat-a", f"a{i}", "--", *job, f"a{i}"))
        check(soon(lambda i=i: waiting() == i), f"a{i} waits in the queue")
    b = start("chat-b", "b1", "--", *job, "b1")           # queued after all three of chat-a
    check(soon(lambda: waiting() == 4), "b1 waits in the queue")
    go.touch()
    jobs = {"busy-1": first, "a1": a[0], "a2": a[1], "a3": a[2], "b1": b}
    for q in jobs.values():
        try:
            q.wait(timeout=120)
        except subprocess.TimeoutExpired:
            q.kill(); q.wait()
    order = log.read_text().split()
    # a job that never ran is named, with its exit code and what it printed, rather than a bare
    # ValueError from order.index() (it failed about one full run in three on a loaded machine)
    missing = {n: (q.returncode, q.stdout.read()[-400:]) for n, q in jobs.items() if n not in order}
    check(not missing, f"every job of the turn ran: the log holds {order}; these did not: {missing}")
    if not missing:
        check(order[0] == "busy-1" and order.index("b1") < order.index("a2"),
              f"⭐ a chat that queues three jobs takes turns with a chat that queued one later: {order}")
        check(order.index("a1") < order.index("a2") < order.index("a3"), "and each chat's jobs keep their order")

    # --- holds for CI jobs, and release -------------------------------------------------------------
    worker = subprocess.Popen(["sleep", "30"])
    rc, out = heavy("hold", "ci-runner", "CI job", "--pid", str(worker.pid))
    rc2, st = heavy("status")
    check(rc == 0 and f"ci-runner: CI job (pid {worker.pid}" in st, "a CI job holds the slot for its worker")
    p = start("t", "behind-ci", "--", "true")
    # its ticket in the queue, not a second on the clock: under load the waiter took longer than a
    # second to start, so "it has not finished" was no proof that it waited (a deploy gate, 2026-10-27)
    check(soon(lambda: waiting() == 1) and p.poll() is None, "and a job waits behind it")
    heavy("release", "--pid", str(worker.pid))
    try:
        said = p.communicate(timeout=60)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        said = p.communicate()[0]
    check(p.returncode == 0, f"release gives the slot back, and the next job runs "
                             f"(rc={p.returncode}: {said.strip()[-200:]!r})")

    rc, out = heavy("hold", "ci-runner", "CI job", "--pid", str(worker.pid))
    worker.kill(); worker.wait()
    rc, st = heavy("status")
    check("slot: free" in st, "⛔ a held slot whose worker died is free to the next caller")

    # --- a heavy job inside a heavy job runs at once -------------------------------------------------
    rc, out = heavy("t", "outer", "--", "bash", "-c", "echo held=$HEAVY_HELD", timeout=30)
    check(rc == 0 and any(l.startswith("held=") and l[5:].isdigit() for l in out.splitlines()),
          f"a job is told the slot is held for it (HEAVY_HELD): {out.strip()[-80:]!r}")
    rc, out = heavy("t", "outer", "--", sys.executable, "-m", "claude_ops.heavy", "t", "inner", "--", "bash", "-c",
                    "echo inner-ran", timeout=30)
    check(rc == 0 and "inner-ran" in out and "inside t: outer" in out,
          f"⭐ a heavy.sh called inside a heavy job runs at once instead of waiting on itself (rc={rc})")
    worker = subprocess.Popen(["bash", "-c", f"sleep 1; {sys.executable} -m claude_ops.heavy t in-ci -- echo ci-nested"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              env=env(HEAVY_HELD=""))
    rc, _ = heavy("hold", "ci-runner", "CI job", "--pid", str(worker.pid))
    wout, _ = worker.communicate(timeout=30)
    check(rc == 0 and "ci-nested" in wout,
          f"and so does one called by a process whose ancestor holds the slot (a step of a CI job): {wout.strip()[-80:]!r}")
    heavy("release", "--pid", str(worker.pid))
    holder_proc = subprocess.Popen(["sleep", "30"])
    heavy("hold", "other", "unrelated", "--pid", str(holder_proc.pid))
    p = start("t", "stranger", "--", "true")
    time.sleep(1)
    check(p.poll() is None, "a job outside the holder still waits its turn")
    heavy("release", "--pid", str(holder_proc.pid)); holder_proc.kill(); holder_proc.wait()
    p.wait(timeout=20)

    def status_until(cond, timeout=30.0, **extra):
        """status, read again until `cond` holds for it or `timeout` passes. A waiter writes its
        ticket at its first poll, which a loaded machine reaches later than a fixed sleep allowed."""
        end = time.time() + timeout
        while True:
            st = heavy("status", **extra)[1]
            if cond(st) or time.time() > end:
                return st
            time.sleep(0.2)

    def held_since(what):
        """When a waiter's own limits began to hold it, as its ticket says; None before it says."""
        for t in (STATE / "queue").glob("*.json"):
            try:
                w = json.loads(t.read_text())
            except (OSError, ValueError):
                continue
            if w.get("what") == what and (w.get("held") or {}).get("at"):
                return float(w["held"]["at"])
        return None

    # --- ⭐ a waiter held by its own limits does not hold up the others ---------------------------
    big = start("big-chat", "needs 50 GB", "--", "true", HEAVY_MIN_FREE_GB="50")
    st = status_until(lambda s: "held by its gate" in s and "needs 50" in s)
    check("held by its gate" in st and "needs 50" in st,
          f"status says a waiter is held by its gate: {st.strip()[-160:]!r}")
    t0 = time.time()
    rc, out = heavy("small-chat", "small", "--", "echo", "small-ran", timeout=30)
    check(rc == 0 and "small-ran" in out and time.time() - t0 < 10,
          "⭐ a job behind it runs at once while the big one waits for room only it needs")
    check(big.poll() is None, "and the big one is still waiting")
    big.terminate(); big.wait(timeout=10)

    # --- a limit out of reach is said once ----------------------------------------------------------
    out_of_reach = start("big-chat", "needs 50 GB", "--", "true", HEAVY_MIN_FREE_GB="50",
                         HEAVY_OWN_LIMIT_NOTE_MIN="0.03", HEAVY_FAKE_MACHINE="10.6 2 50")
    st = status_until(lambda s: "the most memory free in that time 10.6 GB" in s)
    # the note is said at the first poll 1.8 s (0.03 min) after its limits began to hold it, which
    # its ticket names: timed from its start instead, a loaded machine had not reached it yet
    at = held_since("needs 50 GB") or time.time()
    time.sleep(max(0.0, at + 1.8 + 4.0 - time.time()))
    check("held by its gate" in st and "the most memory free in that time 10.6 GB" in st,
          f"status shows how long and the most memory free beside the waiter: {st.strip()[-190:]!r}")
    out_of_reach.terminate()
    said = out_of_reach.communicate(timeout=10)[0]
    check(said.count("the most memory free in that time was 10.6 GB against the 50 GB it asks for") == 1
          and "Lower HEAVY_MIN_FREE_GB" in said,
          f"⭐ the waiter is told once the most memory it saw free and how to act: {said.strip()[-230:]!r}")

    # --- what died is cleared ------------------------------------------------------------------------
    gone = subprocess.Popen(["true"]); gone.wait()
    (STATE / "queue").mkdir(exist_ok=True)
    (STATE / "queue" / f"{time.time_ns():020d}.{gone.pid}.json").write_text(json.dumps(
        {"chat": "dead", "what": "x", "waiter": gone.pid, "pid": gone.pid, "since": "00:00"}))
    rc, st = heavy("status")
    check("dead: x" not in st and not list((STATE / "queue").glob("*.json")),
          "a ticket whose waiter died leaves the queue")

    # --- a turn has an end ---------------------------------------------------------------------------
    session = subprocess.Popen(["sleep", "120"])
    rc, out = heavy("hold", "a-chat", "emulator session", "--pid", str(session.pid))
    check(rc == 0, "a long session holds the slot")
    rc, st = heavy("status")
    check("slot: a-chat: emulator session" in st and "held 0 min" in st and "past its turn" not in st,
          f"status says how long the slot has been held: {st.splitlines()[1][:110]!r}")
    early = start("b-chat", "next job", "--", "echo", "ran-beside", HEAVY_TURN_MIN="1")
    time.sleep(1.5)
    check(early.poll() is None, "before its turn ends, the holder keeps the next job waiting")
    early.terminate(); early.wait(timeout=10)
    time.sleep(2.5)                                   # the hold is now about three seconds old
    rc, st = heavy("status", HEAVY_TURN_MIN="0.02")
    check("past its turn of 0.02 min" in st, f"status marks a holder past its turn: {st.splitlines()[1][:140]!r}")
    beside = start("b-chat", "next job", "--", "bash", "-c", f"echo ran-beside; until [ -e {STATE}/stop-beside ]; do sleep 0.2; done",
                       HEAVY_TURN_MIN="0.02")   # runs until the checks below are done, however slow the machine
    st = status_until(lambda s: "slot: b-chat: next job" in s, HEAVY_TURN_MIN="0.02")
    check("slot: b-chat: next job" in st and "past its turn, still running: a-chat: emulator session" in st,
          f"⭐ past its turn, the next waiter takes the slot and starts beside it: {st.strip()[-170:]!r}")
    check(session.poll() is None, "and the job past its turn is never stopped for it")
    rc, out = heavy("release", "--pid", str(session.pid))
    rc, st = heavy("status", HEAVY_TURN_MIN="0.02")
    check("slot: b-chat: next job" in st and "a-chat" not in st,
          "its own release gives back nothing the new holder holds, and it leaves the list")
    (STATE / "stop-beside").touch()
    out = beside.communicate(timeout=30)[0]
    check(beside.returncode == 0 and "ran-beside" in out and "past its turn" in out,
          f"the job that started beside it says why: {out.strip()[:150]!r}")
    session.kill(); session.wait()
    t0 = time.time()
    rc, out = heavy("c-chat", "forgotten session", "--max-hold", "0.03", "--", "sleep", "60", timeout=60,
                    HEAVY_CHECK="0.3")
    check(rc == 124 and "--max-hold of 0.03 min" in out and time.time() - t0 < 20,
          f"⭐ a job given --max-hold is ended there and told why (rc={rc}): {out.strip()[-80:]!r}")
    rc, out = heavy("c-chat", "x", "--max-hold", "soon", "--", "true")
    check(rc == 2 and "usage" in out, "a --max-hold that is not a number of minutes is refused")
    rc, st = heavy("status")
    check("slot: free" in st, "and the slot is free after it")

    assert not failures, failures


def test_heavy_hook(monkeypatch):
    """HEAVY_HOOK is told asked, admitted, released and withdrawn, and never holds up a job."""
    failures = []
    for k in [k for k in os.environ if k.startswith("HEAVY_")]:
        monkeypatch.delenv(k)
    STATE = Path(tempfile.mkdtemp(prefix="heavy-hook-test-"))
    told = STATE / "hook.txt"
    fake = STATE / "hook.sh"
    fake.write_text(f'printf "%s|" "$@" >> {told}; echo >> {told}\n')

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def env(**extra):
        e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
             "HEAVY_DIR": str(STATE), "HEAVY_FAKE_MACHINE": ROOMY, "HEAVY_POLL": "0.2", "PYTHONPATH": SRC,
             "HEAVY_HOOK": f"bash {fake}"}
        e.update(extra)
        return e

    def heavy(*args, timeout=60, **extra):
        r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", *args], capture_output=True,
                           text=True, timeout=timeout, env=env(**extra))
        return r.returncode, r.stdout + r.stderr

    def start(*args, **extra):
        return subprocess.Popen([sys.executable, "-m", "claude_ops.heavy", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env(**extra))

    def lines():
        got = told.read_text().splitlines() if told.exists() else []
        told.unlink(missing_ok=True)
        return [ln.rstrip("|").split("|") for ln in got]

    def wait_for(cond, s=20):
        t0 = time.time()
        while time.time() - t0 < s and not cond():
            time.sleep(0.1)
        return cond()

    # --- a job it runs: asked, admitted, released, all for the same pid ---------------------------
    rc, out = heavy("h-chat", "job one", "--", "true")
    got = lines()
    check(rc == 0 and [g[:3] for g in got] == [["asked", "h-chat", "job one"], ["admitted", "h-chat", "job one"],
                                                ["released", "h-chat", "job one"]]
          and len({g[3] for g in got}) == 1, f"⭐ a job it runs is asked, admitted and released, in that order: {got}")

    # --- a hold for a process that already runs, and its release -----------------------------------
    worker = subprocess.Popen(["sleep", "60"])
    rc, _ = heavy("hold", "ci-runner", "CI job", "--pid", str(worker.pid))
    got = lines()
    check(rc == 0 and got == [["asked", "ci-runner", "CI job", str(worker.pid)],
                              ["admitted", "ci-runner", "CI job", str(worker.pid)]],
          f"a hold is asked and admitted for the pid it holds for, not the waiter's: {got}")
    heavy("release", "--pid", str(worker.pid))
    heavy("release", "--pid", str(worker.pid))
    got = lines()
    check(got == [["released", "ci-runner", "CI job", str(worker.pid)]],
          f"⭐ and released once, however many times it is given back: {got}")

    # --- a waiter that stops without the slot is withdrawn -------------------------------------------
    heavy("hold", "ci-runner", "first", "--pid", str(worker.pid))
    lines()
    second = subprocess.Popen(["sleep", "60"])
    waiter = start("hold", "other", "second", "--pid", str(second.pid))
    check(wait_for(lambda: told.exists() and "asked|other|second" in told.read_text()), "the second waiter asks")
    second.kill()
    second.wait()
    out = waiter.communicate(timeout=30)[0]
    got = lines()
    check(waiter.returncode == 1 and got == [["asked", "other", "second", str(second.pid)],
                                              ["withdrawn", "other", "second", str(second.pid)]],
          f"⭐ a waiter whose holder ended is withdrawn: {got} {out[-80:]!r}")
    third = subprocess.Popen(["sleep", "60"])
    waiter = start("hold", "other", "third", "--pid", str(third.pid))
    check(wait_for(lambda: told.exists() and "asked|other|third" in told.read_text()), "the third waiter asks")
    waiter.terminate()
    waiter.communicate(timeout=30)
    got = lines()
    check(got[-1:] == [["withdrawn", "other", "third", str(third.pid)]],
          f"and so is a waiter that was stopped, as a CI hook stops one that waited too long: {got}")
    third.kill()
    heavy("release", "--pid", str(worker.pid))
    worker.kill()
    lines()

    # --- a job past its turn is released when it ends --------------------------------------------------
    for k, v in {"HEAVY_DIR": str(STATE), "HEAVY_HOOK": f"bash {fake}"}.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("claude_ops.heavy", None)
    from claude_ops import heavy as h
    h.PAST.mkdir(parents=True, exist_ok=True)
    (h.PAST / "424242.json").write_text(json.dumps({"chat": "p-chat", "what": "long job", "pid": 424242}))
    check(h.release(424242) is False and lines() == [["released", "p-chat", "long job", "424242"]]
          and not (h.PAST / "424242.json").exists(),
          "a job past its turn holds no slot, and is still released when it ends")

    # --- a job inside a heavy job takes no slot and tells nothing ------------------------------------
    inner = f"{sys.executable} -m claude_ops.heavy h-chat inner -- true"
    rc, out = heavy("h-chat", "outer", "--", "bash", "-c", inner)
    got = lines()
    check(rc == 0 and [g[:3] for g in got] == [["asked", "h-chat", "outer"], ["admitted", "h-chat", "outer"],
                                                ["released", "h-chat", "outer"]],
          f"a job run inside a heavy job tells nothing: {got}")

    # --- a hook that fails or hangs never holds up a job ------------------------------------------------
    bad = STATE / "bad.sh"
    bad.write_text("echo nobody home >&2; exit 3\n")
    rc, out = heavy("h-chat", "job two", "--", "bash", "-c", "echo ran-anyway", HEAVY_HOOK=f"bash {bad}")
    check(rc == 0 and "ran-anyway" in out and out.count("was not told") == 1 and "nobody home" in out,
          f"⭐ a hook that fails is said once, and the job runs: {out.strip()[-200:]!r}")
    slow = STATE / "slow.sh"
    slow.write_text("sleep 30\n")
    t0 = time.time()
    rc, out = heavy("h-chat", "job three", "--", "true", HEAVY_HOOK=f"bash {slow}", HEAVY_HOOK_TIMEOUT="0.3")
    check(rc == 0 and time.time() - t0 < 15 and "longer than 0.3 s" in out,
          f"and one that hangs is left behind after HEAVY_HOOK_TIMEOUT ({time.time() - t0:.1f} s): {out.strip()[-120:]!r}")
    rc, out = heavy("h-chat", "job four", "--", "true", HEAVY_HOOK="")
    check(rc == 0 and not told.exists() and "HEAVY_HOOK" not in out, "without HEAVY_HOOK nobody is told and nothing is said")

    # --- ⭐ every failed hook call is a line in HEAVY_HOOK_LOG -------------------------------------------
    # a hook that timed out was ended before it could write anything, and the job's own output is
    # often kept nowhere, so its stderr line alone left no trace anyone could read
    log = STATE / "hook-failures.log"
    rc, out = heavy("h-chat", "job five", "--", "true", HEAVY_HOOK=f"bash {slow}", HEAVY_HOOK_TIMEOUT="0.3",
                    HEAVY_HOOK_LOG=str(log))
    said = log.read_text().splitlines() if log.exists() else []
    check(rc == 0 and [ln.split()[1] for ln in said] == ["asked", "admitted", "released"]
          and all('what="job five"' in ln and "chat=h-chat" in ln and "longer than 0.3 s" in ln for ln in said),
          f"⭐ each failed call is one line with the event, the chat, the job and why, not only the first: {said}")
    rc, out = heavy("h-chat", "job six", "--", "true", HEAVY_HOOK=f"bash {bad}", HEAVY_HOOK_LOG=str(log))
    said = log.read_text().splitlines()
    check(len(said) == 6 and "exit 3: nobody home" in said[-1],
          f"and a hook that exits with an error says its own words there: {said[-1:]}")
    rc, out = heavy("h-chat", "job seven", "--", "true", HEAVY_HOOK=f"bash {bad}",
                    HEAVY_HOOK_LOG=str(STATE / "no-such-folder" / "x.log"))
    check(rc == 0, "a log that cannot be written never stops the job")

    assert not failures, "\n".join(failures)



# --- a job's children in a group or session of their own ----------------------------------------------
# A build tool can start its worker in a session of its own, so the worker leaves the job's process group.
# The stand-in here is a python child that calls setsid under a bash job that waits for it. It gives up
# the caller's output pipes at once, so a child that outlives the job cannot hold the test's run until its
# own sleep ends (which reads as ended).
_OWN_SESSION = ("import os, signal, sys, time\n"
                "if sys.argv[2] == 'deaf':\n    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "os.setsid()\n"
                "n = os.open(os.devnull, os.O_WRONLY); os.dup2(n, 1); os.dup2(n, 2)\n"
                "open(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(float(sys.argv[4]))\n"
                "b = bytearray(int(float(sys.argv[3]) * 1024 ** 3)); b[::4096] = b'x' * len(b[::4096])\n"
                "time.sleep(60)")


def _job_with_own_session_child(pidfile, deaf="hears", gb="0", wait_s="0", shape="{child} & wait"):
    """bash -c that starts the setsid child (which waits `wait_s` before it takes `gb`) in the shape given,
    as argv for a job. The default shape waits for the child."""
    child = '"$5" -c "$0" "$1" "$2" "$3" "$4"'
    return ["bash", "-c", shape.format(child=child), _OWN_SESSION, str(pidfile), deaf, gb, wait_s,
            sys.executable]


def _pid_in(pidfile, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text())
        time.sleep(0.05)
    raise AssertionError(f"the child never wrote {pidfile}")


def _gone(pid, timeout=10.0):
    """the process has ended (a zombie waiting for the init process to reap it counts as ended)"""
    end = time.time() + timeout
    while time.time() < end:
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not stat or stat.startswith("Z"):
            return True
        time.sleep(0.1)
    return False


def _kill(pid):
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


def _heavy_env(state, **extra):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
            "HEAVY_DIR": str(state), "HEAVY_FAKE_MACHINE": ROOMY, "HEAVY_CHECK": "0.2", "PYTHONPATH": SRC, **extra}


def test_a_child_in_a_session_of_its_own_is_still_the_jobs(tmp_path):
    """job_tree finds a child that left the job's process group by its parent pid, with the group it is in."""
    import claude_ops.heavy as h
    pidfile = tmp_path / "child.pid"
    job = subprocess.Popen(_job_with_own_session_child(pidfile), start_new_session=True)
    stray = None
    try:
        stray = _pid_in(pidfile)
        members, outside = h.job_tree(job.pid)
        assert job.pid in members and stray not in members, (members, outside)
        assert outside.get(stray) == stray, f"a session leader leads a group of its own: {outside}"
        assert h.job_tree(1) == ([], {}), "the init process's group is never a job"
    finally:
        os.killpg(job.pid, 9)
        job.wait()
        if stray:
            _kill(stray)


def test_a_job_ended_at_its_max_hold_takes_its_children_in_their_own_session_with_it(tmp_path):
    """The job's group gets SIGTERM, and so does its child in a session of its own. A child that ignores
    SIGTERM gets SIGKILL after HEAVY_STOP_GRACE, and heavy says so."""
    pidfile = tmp_path / "child.pid"
    r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "t-chat", "worker build", "--max-hold", "0.03",
                        "--", *_job_with_own_session_child(pidfile, deaf="deaf")],
                       capture_output=True, text=True, timeout=90,
                       env=_heavy_env(tmp_path / "heavy", HEAVY_STOP_GRACE="1"))
    stray = _pid_in(pidfile, timeout=1)
    try:
        assert r.returncode == 124 and "ENDED worker build" in r.stderr, r.stdout + r.stderr
        assert _gone(stray), f"the child in its own session outlived the job: {stray}"
        assert f"ended 1 of worker build's processes outside its group that outlived SIGTERM: [{stray}]" in r.stderr, \
            r.stderr
    finally:
        _kill(stray)


def test_the_memory_cap_counts_and_ends_a_child_in_a_session_of_its_own(tmp_path):
    """A job whose own group holds almost nothing, with a child in a session of its own holding 0.4 GB, passes a
    0.2 GB cap and is ended, child and all. Before job_tree the cap saw only the group's few MB."""
    if sys.platform != "darwin":        # the cap reads proc_pid_rusage
        return
    pidfile = tmp_path / "child.pid"
    r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "t-chat", "worker build", "--",
                        *_job_with_own_session_child(pidfile, gb="0.4")],
                       capture_output=True, text=True, timeout=90,
                       env=_heavy_env(tmp_path / "heavy", HEAVY_MAX_GB="0.2", HEAVY_STOP_GRACE="2"))
    stray = _pid_in(pidfile, timeout=1)
    try:
        assert r.returncode == 137 and "ENDED worker build" in r.stderr and "over the 0.2 GB cap" in r.stderr, \
            r.stdout + r.stderr
        assert _gone(stray), f"the child in its own session outlived the cap: {stray}"
    finally:
        _kill(stray)


def test_a_child_whose_parent_in_the_group_ended_is_still_ended_with_the_job(tmp_path):
    """A child in a session of its own, seen in a sample while its parent in the job's group lived, is ended
    at --max-hold after that parent has gone. By then it is the init process's child, and no walk from the
    group finds it."""
    pidfile = tmp_path / "child.pid"
    r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "t-chat", "worker build", "--max-hold", "0.05",
                        "--", *_job_with_own_session_child(pidfile, shape="( {child} & sleep 1 ); sleep 60")],
                       capture_output=True, text=True, timeout=90,
                       env=_heavy_env(tmp_path / "heavy", HEAVY_STOP_GRACE="1"))
    stray = _pid_in(pidfile, timeout=1)
    try:
        assert r.returncode == 124 and "ENDED worker build" in r.stderr, r.stdout + r.stderr
        assert _gone(stray), f"the child whose parent had ended outlived the job: {stray}"
    finally:
        _kill(stray)


def test_the_cap_counts_a_child_that_grew_after_its_parent_in_the_group_ended(tmp_path):
    """The child is seen small while its parent lives, its parent ends, and then it takes 0.4 GB. The cap
    of 0.2 GB still counts it and ends the job, child and all."""
    if sys.platform != "darwin":        # the cap reads proc_pid_rusage
        return
    pidfile = tmp_path / "child.pid"
    r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "t-chat", "worker build", "--max-hold", "1", "--",
                        *_job_with_own_session_child(pidfile, gb="0.4", wait_s="2",
                                                     shape="( {child} & sleep 1 ); sleep 60")],
                       capture_output=True, text=True, timeout=120,
                       env=_heavy_env(tmp_path / "heavy", HEAVY_MAX_GB="0.2", HEAVY_STOP_GRACE="2"))
    stray = _pid_in(pidfile, timeout=1)
    try:
        assert r.returncode == 137 and "over the 0.2 GB cap" in r.stderr, r.stdout + r.stderr
        assert _gone(stray), f"the child outlived the cap: {stray}"
    finally:
        _kill(stray)


def test_what_a_job_leaves_running_is_named_and_ended_unless_it_asks_to_keep_it(tmp_path):
    """A job that exits on its own and leaves a child in a session of its own and a background process in
    its group has both named in one line and ended before the slot is free. One that ignores SIGTERM gets
    SIGKILL after HEAVY_STOP_GRACE. With HEAVY_KEEP_STRAYS=1 they are named and kept."""
    shape = "{child} & sleep 30 & sleep 1"
    pidfile = tmp_path / "child.pid"
    r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "t-chat", "daemon build", "--",
                        *_job_with_own_session_child(pidfile, deaf="deaf", shape=shape)],
                       capture_output=True, text=True, timeout=90,
                       env=_heavy_env(tmp_path / "heavy", HEAVY_STOP_GRACE="1"))
    stray = _pid_in(pidfile, timeout=1)
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        line = next((x for x in r.stderr.splitlines() if "left 2 process(es) running when it ended" in x), "")
        assert f"{stray} python" in line.lower() and " sleep 30" in line and "HEAVY_KEEP_STRAYS=1" in line, \
            r.stderr
        assert "and SIGKILL for what outlived SIGTERM by 1 s" in r.stderr, r.stderr
        assert _gone(stray), f"the child left running outlived the job: {stray}"
    finally:
        _kill(stray)

    # a process kept running must not hold the caller's output pipes, or the caller waits for it to end
    kept = tmp_path / "kept.pid"
    r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "t-chat", "server start", "--",
                        *_job_with_own_session_child(kept, shape="{child} >/dev/null 2>&1 & sleep 1")],
                       capture_output=True, text=True, timeout=90,
                       env=_heavy_env(tmp_path / "heavy", HEAVY_KEEP_STRAYS="1"))
    server = _pid_in(kept, timeout=1)
    try:
        assert r.returncode == 0 and "left 1 process(es) running, kept as HEAVY_KEEP_STRAYS=1 asks" in r.stderr, \
            r.stderr
        assert not _gone(server, timeout=0.5), "a job that asks to keep its server keeps it"
    finally:
        _kill(server)

    named_kept = tmp_path / "named.pid"
    r = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "t-chat", "device session", "--",
                        *_job_with_own_session_child(named_kept, shape="{child} & sleep 30 >/dev/null 2>&1 & sleep 1")],
                       capture_output=True, text=True, timeout=90,
                       env=_heavy_env(tmp_path / "heavy", HEAVY_KEEP_STRAYS="sleep", HEAVY_STOP_GRACE="1"))
    worker = _pid_in(named_kept, timeout=1)
    try:
        keep_line = next((x for x in r.stderr.splitlines() if "kept as HEAVY_KEEP_STRAYS=sleep asks" in x), "")
        end_line = next((x for x in r.stderr.splitlines() if "left 1 process(es) running when it ended" in x), "")
        assert r.returncode == 0 and " sleep 30" in keep_line and f"{worker} " in end_line, r.stderr
        assert _gone(worker), "the program not on the list is ended"
        sleeper = int(keep_line.split("asks: ", 1)[1].split()[0])
        assert not _gone(sleeper, timeout=0.5), "and the one named on the list is kept"
        _kill(sleeper)
    finally:
        _kill(worker)


def test_heavy_log_holds_every_line_a_job_says_and_never_stops_the_job(tmp_path):
    """Each job's waits and decisions went only to its own stderr, so the machine's could be read only job by
    job. With HEAVY_LOG set every line a job says is also one line in that file, with the date, the time and
    the pid, and a log that cannot be written leaves the job alone."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "HEAVY_DIR": str(tmp_path / "heavy"), "HEAVY_FAKE_MACHINE": "50 0 50", "PYTHONPATH": SRC}
    log = tmp_path / "heavy.log"
    held = tmp_path / "heavy" / "slot"
    held.mkdir(parents=True)
    holder = subprocess.Popen(["sleep", "60"])
    try:
        (held / "who.json").write_text(json.dumps(
            {"chat": "a-chat", "what": "first", "pid": holder.pid, "since": "03:21", "at": time.time()}))
        waiter = subprocess.Popen([sys.executable, "-m", "claude_ops.heavy", "b-chat", "second", "--", "true"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  env={**env, "HEAVY_LOG": str(log), "HEAVY_POLL": "0.2"})
        time.sleep(2)
        holder.terminate()
        holder.wait(timeout=10)
        out = waiter.communicate(timeout=30)[0]
    finally:
        holder.kill()
    lines = log.read_text().splitlines() if log.exists() else []
    assert any(f"pid={waiter.pid} waiting, the slot is held by a-chat: first" in ln for ln in lines), (lines, out)
    assert all(ln[:4].isdigit() and "T" in ln.split()[0] for ln in lines), f"each line starts with its date and time: {lines}"
    rc = subprocess.run([sys.executable, "-m", "claude_ops.heavy", "b-chat", "third", "--", "true"],
                        capture_output=True, text=True,
                        env={**env, "HEAVY_LOG": str(tmp_path / "no-such-folder" / "x.log")}).returncode
    assert rc == 0, "a log that cannot be written never stops the job"
