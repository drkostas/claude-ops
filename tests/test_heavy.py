"""The heavy-job queue: the turn across chats, the gate, the memory cap, holds for CI jobs, and the
clearing of what died. Every run uses its own state folder and stand-in machine readings."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOMY = "20 2 50"          # free GB, swap GB, disk %: a machine with room


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
             "HEAVY_DIR": str(STATE), "HEAVY_FAKE_MACHINE": ROOMY, "HEAVY_POLL": "0.2"}
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
        for k, v in {"HEAVY_DIR": str(STATE), **extra}.items():
            monkeypatch.setenv(k, v)
        sys.modules.pop("claude_ops.heavy", None)
        from claude_ops import heavy as h
        return h

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

    # --- the gate ---------------------------------------------------------------------------------
    h = module(HEAVY_MIN_FREE_GB="6", HEAVY_MAX_SWAP_GB="14", HEAVY_MIN_DISK_PCT="10")
    check(h.short((20, 2, 50)) == "", "a machine with room is not short")
    why = h.short((3, 18.5, 8))
    check("memory free" in why and "swap at 18.5" in why and "disk at 8" in why,
          f"a short machine says each reason ({why})")
    check(h.short((20, 19, 50)) == "",
          "swap far over its limit with memory free and room on the disk holds nothing: swap is history")
    check("swap at 19" in h.short((20, 19, 11)) and h.short((20, 19, 13)) == "",
          "it holds a job only while the disk is short too (under HEAVY_SWAP_DISK_PCT, 12)")
    # each limit at its edge: a comparison moved by one (`<` to `<=`) must turn a check red
    check(h.short((6, 2, 50)) == "" and "memory free" in h.short((5.9, 2, 50)),
          "exactly HEAVY_MIN_FREE_GB free is enough, a little under is not")
    check(h.short((20, 2, 10)) == "" and "disk at 9.9" in h.short((20, 2, 9.9)),
          "exactly HEAVY_MIN_DISK_PCT free is enough, a little under is not")
    check(h.short((20, 14, 11)) == "" and "swap at 14.1" in h.short((20, 14.1, 11)),
          "swap exactly at HEAVY_MAX_SWAP_GB beside a short disk is allowed, a little over is not")
    check(h.short((20, 19, 12)) == "" and "swap at 19" in h.short((20, 19, 11.9)),
          "high swap beside the disk exactly at HEAVY_SWAP_DISK_PCT free is allowed, a little under is not")
    check("critical" in h.short((20, 2, 50, 4)) and h.short((20, 2, 50, 2)) == "",
          "macOS rating the pressure critical holds a job; raised does not, the free memory decides")
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
    job = ["bash", "-c", f'echo "$0" >> {log}; sleep 0.6']
    first = start("busy", "first", "--", "bash", "-c", f'echo busy-1 >> {log}; sleep 3')
    time.sleep(0.5)                                       # busy-1 holds the slot while the rest queue
    a = []
    for i in (1, 2, 3):                                   # apart, so their tickets keep this order
        a.append(start("chat-a", f"a{i}", "--", *job, f"a{i}"))
        time.sleep(0.3)
    b = start("chat-b", "b1", "--", *job, "b1")           # queued after all three of chat-a
    for q in [first, *a, b]:
        q.wait(timeout=60)
    order = log.read_text().split()
    check(order[0] == "busy-1" and order.index("b1") < order.index("a2"),
          f"⭐ a chat that queues three jobs takes turns with a chat that queued one later: {order}")
    check(order.index("a1") < order.index("a2") < order.index("a3"), "and each chat's jobs keep their order")

    # --- holds for CI jobs, and release -------------------------------------------------------------
    worker = subprocess.Popen(["sleep", "30"])
    rc, out = heavy("hold", "ci-runner", "CI job", "--pid", str(worker.pid))
    rc2, st = heavy("status")
    check(rc == 0 and f"ci-runner: CI job (pid {worker.pid}" in st, "a CI job holds the slot for its worker")
    p = start("t", "behind-ci", "--", "true")
    time.sleep(1)
    check(p.poll() is None, "and a job waits behind it")
    heavy("release", "--pid", str(worker.pid))
    p.wait(timeout=20)
    check(p.returncode == 0, "release gives the slot back, and the next job runs")

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

    # --- ⭐ a waiter held by its own limits does not hold up the others ---------------------------
    big = start("big-chat", "needs 50 GB", "--", "true", HEAVY_MIN_FREE_GB="50")
    time.sleep(1)
    rc, st = heavy("status")
    check("held by its own limits" in st and "needs 50" in st,
          f"status says a waiter is held by its own limits: {st.strip()[-160:]!r}")
    t0 = time.time()
    rc, out = heavy("small-chat", "small", "--", "echo", "small-ran", timeout=30)
    check(rc == 0 and "small-ran" in out and time.time() - t0 < 10,
          "⭐ a job behind it runs at once while the big one waits for room only it needs")
    check(big.poll() is None, "and the big one is still waiting")
    big.terminate(); big.wait(timeout=10)

    # --- a limit out of reach is said once ----------------------------------------------------------
    out_of_reach = start("big-chat", "needs 50 GB", "--", "true", HEAVY_MIN_FREE_GB="50",
                         HEAVY_OWN_LIMIT_NOTE_MIN="0.03", HEAVY_FAKE_MACHINE="10.6 2 50")
    time.sleep(3.5)                                    # past the 1.8 s note threshold, with polls to spare
    rc, st = heavy("status")
    check("held by its own limits" in st and "the most memory free in that time 10.6 GB" in st,
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
    beside = start("b-chat", "next job", "--", "bash", "-c", "echo ran-beside; sleep 4", HEAVY_TURN_MIN="0.02")
    time.sleep(2)
    rc, st = heavy("status", HEAVY_TURN_MIN="0.02")
    check("slot: b-chat: next job" in st and "past its turn, still running: a-chat: emulator session" in st,
          f"⭐ past its turn, the next waiter takes the slot and starts beside it: {st.strip()[-170:]!r}")
    check(session.poll() is None, "and the job past its turn is never stopped for it")
    rc, out = heavy("release", "--pid", str(session.pid))
    rc, st = heavy("status", HEAVY_TURN_MIN="0.02")
    check("slot: b-chat: next job" in st and "a-chat" not in st,
          "its own release gives back nothing the new holder holds, and it leaves the list")
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
