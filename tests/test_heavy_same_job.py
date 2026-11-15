"""A record whose pid now belongs to a newer process is stale.

The system gives an ended job's pid to another process in time. heavy's records name a pid and say when they
were written, and the process they name ran before that, so a live pid whose process started later is not
the job. The holder, a job past its turn, a memory sample and a ticket each go then, and a start that cannot
be read keeps the record.
"""
import importlib
import json
import os
import subprocess
import sys
import time

import pytest


@pytest.fixture
def heavy(tmp_path, monkeypatch):
    monkeypatch.setenv("HEAVY_DIR", str(tmp_path / "heavy"))
    monkeypatch.delenv("HEAVY_FAKE_STARTED", raising=False)
    sys.modules.pop("claude_ops.heavy", None)
    h = importlib.import_module("claude_ops.heavy")
    yield h
    sys.modules.pop("claude_ops.heavy", None)


@pytest.fixture
def fresh():
    """A process started now, the way a reused pid looks to a record written earlier."""
    p = subprocess.Popen(["sleep", "120"])
    for _ in range(50):                      # ps lists it once it has started
        if subprocess.run(["ps", "-o", "lstart=", "-p", str(p.pid)], capture_output=True, text=True).stdout.strip():
            break
        time.sleep(0.05)
    yield p
    p.kill()
    p.wait()


def test_starts_reads_each_process_start(heavy):
    began = heavy.starts().get(os.getpid())
    assert began is not None and time.time() - 86400 < began <= time.time() + heavy.SAME_START_S


def test_same_job_is_a_live_pid_started_before_its_record(heavy, fresh):
    now = time.time()
    assert heavy.same_job(fresh.pid, now + 5)                  # the record came after the process: the job
    assert not heavy.same_job(fresh.pid, now - 3600)           # the process came after the record: a reused pid
    assert heavy.same_job(fresh.pid, None)                     # a record without a time keeps the job
    dead = subprocess.Popen(["true"])
    dead.wait()
    assert not heavy.same_job(dead.pid, now + 5)


def test_a_start_that_cannot_be_read_keeps_the_record(heavy, fresh, monkeypatch):
    monkeypatch.setattr(heavy, "starts", lambda: {})
    assert heavy.same_job(fresh.pid, time.time() - 3600)


def test_a_job_past_its_turn_whose_pid_was_reused_goes(heavy, fresh):
    heavy.PAST.mkdir(parents=True)
    stale = heavy.PAST / f"{fresh.pid}.json"
    stale.write_text(json.dumps({"chat": "a", "what": "old build", "pid": fresh.pid, "at": time.time() - 3600}))
    assert heavy.past_jobs() == [] and not stale.exists()
    stale.write_text(json.dumps({"chat": "a", "what": "build", "pid": fresh.pid, "at": time.time() + 5}))
    assert [j["what"] for j in heavy.past_jobs()] == ["build"] and stale.exists()


def test_a_past_job_is_kept_when_no_start_can_be_read(heavy, fresh, monkeypatch):
    monkeypatch.setattr(heavy, "starts", lambda: {})
    heavy.PAST.mkdir(parents=True)
    (heavy.PAST / f"{fresh.pid}.json").write_text(
        json.dumps({"chat": "a", "what": "build", "pid": fresh.pid, "at": time.time() - 3600}))
    assert [j["what"] for j in heavy.past_jobs()] == ["build"]


def test_a_holder_whose_pid_was_reused_frees_the_slot_and_takes_nothing_back(heavy, fresh):
    heavy.SLOT.mkdir(parents=True)
    rec = {"chat": "a", "what": "old build", "pid": fresh.pid, "since": "03:21", "at": time.time() - 3600}
    (heavy.SLOT / "who.json").write_text(json.dumps(rec))
    heavy.TAKING.mkdir(parents=True)
    sample = heavy.TAKING / f"{fresh.pid}.json"
    sample.write_text(json.dumps({"now_gb": 2.0, "peak_gb": 9.0, "at": time.time() - 3000}))
    assert heavy.may_take_again(rec) == (0.0, 0.0)
    heavy.clear_stale()
    assert not heavy.SLOT.exists() and not sample.exists()


def test_a_holder_that_is_still_the_job_keeps_the_slot(heavy, fresh):
    heavy.SLOT.mkdir(parents=True)
    rec = {"chat": "a", "what": "build", "pid": fresh.pid, "since": "03:21", "at": time.time() + 5}
    (heavy.SLOT / "who.json").write_text(json.dumps(rec))
    heavy.TAKING.mkdir(parents=True)
    (heavy.TAKING / f"{fresh.pid}.json").write_text(json.dumps({"now_gb": 2.0, "peak_gb": 9.0, "at": time.time() + 5}))
    assert heavy.may_take_again(rec) == (7.0, 9.0)
    heavy.clear_stale()
    assert heavy.SLOT.exists() and (heavy.TAKING / f"{fresh.pid}.json").exists()


def test_a_ticket_whose_pid_was_reused_goes(heavy, fresh):
    heavy.QUEUE.mkdir(parents=True)
    old = heavy.QUEUE / f"{time.time_ns() - 3600 * 10**9:020d}.{fresh.pid}.json"
    old.write_text(json.dumps({"chat": "a", "what": "old", "waiter": fresh.pid}))
    assert heavy.waiters() == [] and not old.exists()
    new = heavy.QUEUE / f"{time.time_ns() + 5 * 10**9:020d}.{fresh.pid}.json"
    new.write_text(json.dumps({"chat": "a", "what": "new", "waiter": fresh.pid}))
    assert [w["what"] for w in heavy.waiters()] == ["new"]


def test_several_records_read_the_process_table_once(heavy, monkeypatch):
    calls = []

    def counted():
        calls.append(1)
        return {}

    monkeypatch.setattr(heavy, "starts", counted)
    heavy.PAST.mkdir(parents=True)
    me = os.getpid()
    for i in range(3):
        (heavy.PAST / f"{me}-{i}.json").write_text(json.dumps({"chat": "a", "what": str(i), "pid": me, "at": time.time()}))
    assert len(heavy.past_jobs()) == 3 and len(calls) == 1
    calls.clear()
    dead = subprocess.Popen(["true"])
    dead.wait()
    for f in heavy.PAST.glob("*.json"):
        f.unlink()
    (heavy.PAST / f"{dead.pid}.json").write_text(json.dumps({"chat": "a", "what": "gone", "pid": dead.pid, "at": 0}))
    assert heavy.past_jobs() == [] and calls == []            # no live pid, no ps
