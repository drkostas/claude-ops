"""heavy — one heavy job at a time on a shared machine, taken in turn across chats, and none while
memory or disk is short.

    claude-ops heavy <chat> <what> -- <command...>     wait for the turn, run under the memory cap
    claude-ops heavy <chat> <what> --max-hold <minutes> -- <command...>   the same, ended after that long
    claude-ops heavy <chat> <what> --notify -- <command...>   the same, and the result goes to <chat>
    claude-ops heavy <chat> <what> --need-gb <GB> -- <command...>   the same, for a job that needs that much
    claude-ops heavy hold <chat> <what> --pid <pid>   take the slot for a process that already runs
    claude-ops heavy release --pid <pid>              give a held slot back
    claude-ops heavy status                           the machine, who holds the slot, who goes next

WHY. Several chats on one machine each start builds, emulators, renders, crawls and test suites. Run
together they fill memory and swap, and swap files fill the disk, until everything on the machine
slows down. This is one queue for every heavy job: a job waits for its turn and for room, runs under
a memory cap, and gives the slot back.

THE GATE. A job starts only with HEAVY_MIN_FREE_GB (6) of free and reclaimable memory, macOS not
rating the memory pressure critical, and the data volume over HEAVY_MIN_DISK_PCT (10) free; swap over
HEAVY_MAX_SWAP_GB (14) holds it only while the free disk is also under HEAVY_SWAP_DISK_FACTOR (3)
times the swap in use. Swap is history, and hurts only when its files could fill the disk, so the
room it needs grows with the swap and not with the size of the disk: a share of a large disk counted
a hundred gigabytes free as short. Otherwise it waits and says why.

THE CAP. A gate at the start is not enough, since a job that starts within limits can grow. A job
runs in its own process group, and every HEAVY_CHECK seconds (10) the memory of the whole group (top's
MEM, which counts compressed pages) is added up; past HEAVY_MAX_GB (8) the group is ended and the
caller is told why, in its own output. A job that needs more sets HEAVY_MAX_GB on the same line.
A process the job starts in a group or session of its own (a build tool's worker, a server started
with setsid) is still the job's: it is found by its parent pid, counted with the group, and ended
with it. An ended job gets SIGTERM, and SIGKILL after HEAVY_STOP_GRACE seconds (10). Such a process
is remembered from each sample, so it still counts after its parent in the group has ended. When the
job's own process exits, whatever it left running (in its group or outside it) is named in one line
and ended the same way, before the slot is free. HEAVY_KEEP_STRAYS=1 keeps all of it (a job that means
to leave a server running), and a comma-separated list of program names keeps those (adb,watchman).

A JOB THAT QUEUES ITSELF IS REFUSED. A test runner or a deploy script that waits in a queue of its own
holds the slot for as long as it waits there, and every heavy job behind it waits too. HEAVY_SELF_QUEUED
names such programs (space or comma separated). A job whose command names one, as a word of the command,
inside an inline script given as one word (bash -c '...'), or in the text of a file the command names
(a script it runs, read one level down, its comment lines left out), is refused before it waits: it says which program, and that
claude-ops detach runs it in the background instead, and heavy exits 2. A job inside another heavy job
takes no slot, so it is never refused. Without HEAVY_SELF_QUEUED nothing is refused.

A JOB MAY SAY WHAT IT NEEDS. The gate's HEAVY_MIN_FREE_GB (6) is room for a job nobody has measured, and a
small job waited behind it on a machine with plenty of room for that job. A job that knows its size
declares it with --need-gb <GB> or HEAVY_NEED_GB, held between NEED_FLOOR_GB (0.5) and HEAVY_MAX_GB, and
the gate then asks that much free memory instead. A job that declares nothing asks HEAVY_MIN_FREE_GB as
before. A declaration is not taken on trust. Each job is sampled as it runs, and a peak more than
NEED_SLACK (a quarter) over the need is one line, and it is kept for that job name in HEAVY_DIR/peaks.
The next run of that name asks the larger of its need and that peak, until a run stays within its need
again. Declared needs add up against free memory as every job does: a waiter beside a job past its turn
still counts the room that job may take again.

THE TURN. Each waiter leaves a ticket. The next holder is the oldest ticket of the chat that held the
slot least recently, so a chat that queues eight jobs takes every other turn instead of all eight
before anyone else. Waiters look every HEAVY_POLL seconds (5). A ticket or a slot whose process is
gone is cleared by whoever looks next. A waiter held back by its gate (one big job run with
HEAVY_MIN_FREE_GB=16, or a machine short of room) is passed over until it has room, so it never holds
up the jobs behind it. Once the gate has held it for HEAVY_OWN_LIMIT_NOTE_MIN minutes (60) it says,
once, what holds it in the gate's own words, whether that is the machine or a limit set in the job's
own environment, and the most memory it saw free when memory was short. `status` shows the same
beside it.

A TURN HAS AN END. A job that has held the slot for HEAVY_TURN_MIN minutes (60) is past its turn:
it keeps running and is never stopped for it, but it gives the slot up, so the next waiter starts
beside it as soon as the machine has room. `status` says how long the slot has been held, and lists
a job past its turn that still runs. A job that should end by itself takes `--max-hold <minutes>`,
and it is ended there with a line that says so.

A JOB PAST ITS TURN MAY GROW BACK. A running job writes its sampled memory, now and its peak so far, to
HEAVY_DIR/taking/<pid>.json each time it is sampled. A waiter that would start beside a job past its turn
counts the room that job may take again (its peak less what it uses now) as memory that is not free, so a
build that shrank between two steps cannot let a second job in that the build then pushes into swap. The
waiter says so in the same measured words, and waits until both fit.

WHAT IT TOOK. Every job's finished or ENDED line says its peak memory, and HEAVY_DIR/done/<pid>.json
keeps it with the exit code and the minutes, so a chat knows the cap a job needs before running it
again. The peak is the largest of the group's sampled memory and the biggest child's own maximum
resident size, so a job shorter than one check still has one. `--notify` runs the HEAVY_NOTIFY
command with the chat's name and one line (exit code, peak memory, minutes, the job's last line)
when the job ends; with it, the job's output and errors arrive as one stream.

THE SAME FAILURE AGAIN. A caller that retries a job the cap ends every time (a daemon that rebuilds
on each pass) burns the slot for hours while every run fails the same way, and each run on its own
looks like one ordinary failure. So a job that ends the same way HEAVY_REPEAT_FAILS times (3) in a row
(over the cap, at its --max-hold, or with the same exit code) says so: the line names how it failed,
how many times and since when, and with `--notify` it reaches the chat too, at the third failure and
again at the 6th, 12th, 24th and so on. `status` lists every job failing that way in the last
HEAVY_REPEAT_HOURS (24), and `failing()` gives the same to a reader of HEAVY_DIR.

A call made inside a heavy job runs at once, inside the outer job's slot and memory cap: the job is
given HEAVY_HELD, and a caller whose ancestor holds the slot (a step of a CI job) counts too. A CI
runner can hold the slot for a job from its job-started hook (`hold ... --pid <worker>`) to its
job-completed hook (`release`), and a worker that dies frees it.

ANOTHER RECORD OF THE MACHINE'S JOBS. A machine whose other work is queued somewhere else (a
scheduler that also gives out test slots, say) needs to know when a heavy job waits and when it runs,
so a running heavy job can count against that work too. HEAVY_HOOK names a command that is run with
the event, the chat, the job and the pid that holds or would hold the slot appended: `asked` when a
job starts to wait, `admitted` when it takes the slot, `released` when it gives back a slot it held
(or ends after it was past its turn), and `withdrawn` when it stops waiting without the slot (its
holder ended, or the waiter was stopped). A job run inside another heavy job tells nothing, since it
takes no slot. The hook is told and never asked: it runs for at most HEAVY_HOOK_TIMEOUT seconds (5),
and a hook that fails, hangs or is missing never holds up or stops a job. The first failure is said.
A hook that timed out was ended before it could write anything, and a job's own output is often kept
nowhere, so with HEAVY_HOOK_LOG set every failure is also one line in that file: the time, the event,
the chat, the job, the pid and why.

ONE LOG FOR THE MACHINE. Each job says its waits and decisions on its own stderr, which ends in that
job's log. With HEAVY_LOG set, every such line is also appended to that one file with the date, the
time and the pid, so a reader (or a check that waits for a line) finds every job's in one place.

The state lives in HEAVY_DIR (/tmp/claude-ops-heavy). Every caller on the machine must use the same.
macOS only: the memory readings come from vm_stat, sysctl and proc_pid_rusage.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import killguard
from .footprint import total_gb

DIR = Path(os.environ.get("HEAVY_DIR", "/tmp/claude-ops-heavy"))
SLOT, QUEUE, LAST = DIR / "slot", DIR / "queue", DIR / "last"
#: jobs that held the slot past their turn and gave it up, still running, one file per pid
PAST = DIR / "past"
#: jobs that run now, one file per pid: the memory each uses and its peak so far, as last sampled
TAKING = DIR / "taking"
#: jobs that finished, one file per pid: what each took (peak memory, minutes, exit code)
DONE = DIR / "done"
#: how many finished jobs DONE keeps (about 200 bytes each, so well under its 1 MB)
DONE_KEEP = 200
#: the peaks of jobs that took more than they declared, one file per job name, for that name's next run
PEAKS = DIR / "peaks"
#: how many job names PEAKS keeps
PEAKS_KEEP = 200
#: the least memory a job may declare it needs: a machine with less free than this has no room for anything
NEED_FLOOR_GB = 0.5
#: a sampled peak this share over the declared need is said, and the next run of that name waits for it
NEED_SLACK = 0.25


def _session() -> str | None:
    """The Claude Code session that started this job, when one did (Claude Code sets
    CLAUDE_CODE_SESSION_ID for the commands it runs). It is kept beside the chat name in the ticket,
    the slot and the done record, so a reader can tell which conversation a job belongs to when the
    name it was given is a project's, not a chat's."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def _num(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


MIN_FREE_GB = _num("HEAVY_MIN_FREE_GB", 6)
MAX_SWAP_GB = _num("HEAVY_MAX_SWAP_GB", 14)
#: swap over its limit holds a job only while the free disk is under this many times the swap (see reasons)
SWAP_DISK_FACTOR = _num("HEAVY_SWAP_DISK_FACTOR", 3)
MIN_DISK_PCT = _num("HEAVY_MIN_DISK_PCT", 10)
MAX_GB = _num("HEAVY_MAX_GB", 8)
POLL = _num("HEAVY_POLL", 5)
CHECK = _num("HEAVY_CHECK", 10)
STOP_GRACE = _num("HEAVY_STOP_GRACE", 10)
KEEP_STRAYS = os.environ.get("HEAVY_KEEP_STRAYS", "").strip()
#: how much of a script heavy reads for the names of HEAVY_SELF_QUEUED (queues_itself)
SELF_QUEUED_READ = 262144
TURN_MIN = _num("HEAVY_TURN_MIN", 60)
#: how long the gate may hold a waiter before it is told what holds it
OWN_LIMIT_NOTE_MIN = _num("HEAVY_OWN_LIMIT_NOTE_MIN", 60)
#: the settings behind each kind of reason the gate gives, to tell a job's own limit from the machine
LIMIT_VARS = {"memory": ("HEAVY_MIN_FREE_GB", "HEAVY_NEED_GB"), "pressure": (),
              "swap": ("HEAVY_MAX_SWAP_GB", "HEAVY_SWAP_DISK_FACTOR"), "disk": ("HEAVY_MIN_DISK_PCT",)}
#: how many runs in a row of one job that fail the same way are worth saying
REPEAT_FAILS = int(_num("HEAVY_REPEAT_FAILS", 3))
#: how far back `status` and failing() look for such a run of failures
REPEAT_HOURS = _num("HEAVY_REPEAT_HOURS", 24)
#: how long the HEAVY_HOOK command may take before it is left behind
HOOK_TIMEOUT = _num("HEAVY_HOOK_TIMEOUT", 5)
#: what the system tools need, and nothing from the caller's environment
TOOLS_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin"}


def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M')} heavy: {msg}", file=sys.stderr, flush=True)
    log_line(msg)


def log_line(msg: str) -> None:
    """One line in HEAVY_LOG, when it is set, for every line a job says: the date and time, the pid that
    said it and the words. Each job's own output goes to its own log or nowhere, so without it the
    machine's waits and decisions could only be read job by job. A log that cannot be written is left
    alone, since the log only tells."""
    path = os.environ.get("HEAVY_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def _out(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, env=TOOLS_ENV).stdout


def machine() -> tuple[float, float, float, float, float | None]:
    """(free and reclaimable memory GB, swap used GB, data volume free %, macOS's memory pressure
    level: 1 normal, 2 warn, 4 critical, data volume free GB). HEAVY_FAKE_MACHINE ("free swap disk
    [level [disk GB]]") stands in for the readings in tests; without the disk's GB the swap rule has
    nothing to compare with, and holds nothing."""
    fake = os.environ.get("HEAVY_FAKE_MACHINE")
    if fake:
        vals = [float(x) for x in fake.split()]
        return (vals[0], vals[1], vals[2], (vals[3] if len(vals) > 3 else 1.0),
                (vals[4] if len(vals) > 4 else None))
    vm = _out("/usr/bin/vm_stat")
    page = int(re.search(r"page size of (\d+)", vm).group(1)) if "page size of" in vm else 16384
    pages = {k: int(v) for k, v in re.findall(r"Pages (free|inactive|speculative):\s+(\d+)", vm)}
    free = sum(pages.values()) * page / 1e9
    m = re.search(r"used = ([\d.]+)M", _out("/usr/sbin/sysctl", "-n", "vm.swapusage"))
    swap = float(m.group(1)) / 1024 if m else 0.0
    st = os.statvfs("/System/Volumes/Data" if Path("/System/Volumes/Data").exists() else "/")
    disk = st.f_bavail * 100 / st.f_blocks
    disk_gb = st.f_bavail * st.f_frsize / 1e9
    level = _out("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level").strip()
    return (round(free, 1), round(swap, 1), round(disk, 1), float(level) if level.isdigit() else 1.0,
            round(disk_gb, 1))


def reasons(m: tuple, ask_gb: float | None = None) -> list[tuple[str, str]]:
    """What holds a heavy job on a machine read as `m` (machine()'s tuple), each as (kind, words), the kind
    being memory, pressure, swap or disk. Empty when it has room. `ask_gb` is the free memory this job asks
    (gate_gb), MIN_FREE_GB when it is not given."""
    free, swap, disk = m[:3]
    level = m[3] if len(m) > 3 else 1.0
    disk_gb = m[4] if len(m) > 4 else None
    ask = MIN_FREE_GB if ask_gb is None else ask_gb
    why = []
    if free < ask:
        why.append(("memory", f"only {free} GB of memory free (needs {ask:g})"))
    if level >= 4:
        why.append(("pressure", "macOS rates the memory pressure critical"))
    # Swap counts only beside a short disk. Swap used is history: pages pushed out earlier stay in
    # swap until something touches them, so a machine can show many gigabytes of swap with plenty
    # of memory free and macOS rating the pressure normal. Swap hurts when its files fill the disk,
    # so it holds a job only while the free disk is under a few times the swap. A share of the disk
    # was the measure before, and on a large disk it held jobs with far more room than swap could fill.
    if swap > MAX_SWAP_GB and disk_gb is not None and disk_gb < SWAP_DISK_FACTOR * swap:
        why.append(("swap", f"swap at {swap} GB (limit {MAX_SWAP_GB:g}) with {disk_gb:g} GB of disk free "
                            f"(needs {SWAP_DISK_FACTOR:g} times the swap, {round(SWAP_DISK_FACTOR * swap, 1):g} GB)"))
    if disk < MIN_DISK_PCT:
        why.append(("disk", f"disk at {disk}% free (needs {MIN_DISK_PCT:g}%)"))
    return why


def short(m: tuple) -> str:
    """Why the machine is too short to start a heavy job, or ""."""
    return "; ".join(words for _kind, words in reasons(m))


def declared_need(given: str | None = None) -> tuple[float | None, tuple[str, str] | None]:
    """(the memory this job says it needs, where that came from and its words), from `given` (--need-gb) or
    else HEAVY_NEED_GB, held between NEED_FLOOR_GB and MAX_GB. (None, None) when it declares nothing, and its
    gate then asks MIN_FREE_GB. A value that is not a number is said and left out, as if none was declared."""
    name = "--need-gb" if given is not None else "HEAVY_NEED_GB"
    raw = given if given is not None else os.environ.get("HEAVY_NEED_GB", "")
    if not str(raw).strip():
        return None, None
    try:
        need = float(raw)
    except ValueError:
        need = math.nan
    if not math.isfinite(need):
        say(f"{name} {raw!r} is not a number of GB, so this job asks {MIN_FREE_GB:g} GB free like one that "
            "declares nothing")
        return None, None
    held = round(min(max(need, NEED_FLOOR_GB), MAX_GB), 2)
    if held != need:
        say(f"{name} {need:g} is held to {held:g} GB, between the floor of {NEED_FLOOR_GB:g} GB and the "
            f"{MAX_GB:g} GB memory cap")
    return held, (name, f"{name} {raw}" if name == "--need-gb" else f"{name}={raw}")


def _peak_file(what: str) -> Path:
    return PEAKS / f"{hashlib.sha256(what.encode()).hexdigest()[:20]}.json"


def kept_peak(what: str) -> float:
    """The peak the last run of `what` took over its declared need, kept for its next run, or 0."""
    try:
        return float(json.loads(_peak_file(what).read_text()).get("peak_gb", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def gate_gb(what: str, need: float | None) -> float:
    """The free memory the gate asks for a run of `what`: MIN_FREE_GB when it declares no need, else the larger
    of its need and the peak its last run took over that need (kept_peak), never above MAX_GB."""
    if need is None:
        return MIN_FREE_GB
    return round(min(max(need, kept_peak(what)), MAX_GB), 2)


def judge_need(chat: str, what: str, need: float | None, peak: float) -> str | None:
    """After a run that declared `need`: a peak more than NEED_SLACK over it is kept for the next run of `what`
    and said, in one line that is returned. A run that stayed within its need drops a peak kept before, and its
    next run asks the need again. Nothing for a run that declared nothing."""
    if need is None:
        return None
    f = _peak_file(what)
    if peak > need * (1 + NEED_SLACK):
        try:
            PEAKS.mkdir(parents=True, exist_ok=True)
            for old in sorted(PEAKS.glob("*.json"), key=lambda x: x.stat().st_mtime)[:-(PEAKS_KEEP - 1) or None]:
                if old != f:
                    old.unlink(missing_ok=True)
            tmp = f.with_name(f".{f.name}")
            tmp.write_text(json.dumps({"chat": chat, "what": what, "need_gb": need, "peak_gb": peak,
                                       "at": time.strftime("%Y-%m-%d %H:%M"), "session": _session()}))
            os.replace(tmp, f)
        except OSError:
            pass
        line = (f"{what} took {peak:g} GB, more than a quarter over the {need:g} GB it declares, so its next "
                f"run waits for {min(peak, MAX_GB):g} GB free. Declare what it takes, or make it use less.")
        say(line)
        return line
    if f.exists():
        f.unlink(missing_ok=True)
        say(f"{what} took {peak:g} GB, within the {need:g} GB it declares, so its next run asks {need:g} GB "
            "again")
    return None


def held_note(why: str, kinds: set[str], minutes: float, most_free: float, ask_gb: float | None = None,
              declared: tuple[str, str] | None = None) -> str:
    """What a waiter says once its gate has held it for OWN_LIMIT_NOTE_MIN: for how long, what holds it in the
    gate's own words, whether that is a limit set in the job's own environment or the machine, and what would
    let it start. `ask_gb` is the free memory the job asks (MIN_FREE_GB when not given), and `declared` is
    where its declared need came from and its words, ("--need-gb", "--need-gb 2") say, when it declared one:
    its memory limit is then that need and not HEAVY_MIN_FREE_GB."""
    own: list[tuple[str, str]] = []
    for k in sorted(kinds):
        if k == "memory" and declared:
            own.append(declared)
            continue
        own += [(v, f"{v}={os.environ[v]}") for v in LIMIT_VARS.get(k, ())
                if v in os.environ and v != "HEAVY_NEED_GB"]
    note = (f"the gate has held this job for {_ago(minutes)}: {why}. "
            + (f"That is this job's own limit ({', '.join(words for _name, words in own)})"
               if own else "That is the machine, not a limit this job set"))
    if "memory" in kinds:
        note += (f", and the most memory free in that time was {most_free:g} GB against the "
                 f"{MIN_FREE_GB if ask_gb is None else ask_gb:g} GB it asks for")
    if own:
        return note + f". Lower {' or '.join(name for name, _words in own)} to what the job needs, or stop the job"
    needs = {"memory": "free memory", "pressure": "free memory", "swap": "free disk", "disk": "free disk"}
    room = " and ".join(dict.fromkeys(needs[k] for k in sorted(kinds) if k in needs)) or "room"
    return note + f". It starts when the machine has more {room}, or stop the job"


def alive(pid: int) -> bool:
    """Whether `pid` is a live process; another user's process counts as alive, and a pid that
    cannot be told is taken as gone."""
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def holder() -> dict | None:
    try:
        return json.loads((SLOT / "who.json").read_text())
    except (OSError, ValueError):
        return None


def held_min(h: dict) -> float:
    """How long a holder has held the slot, in minutes."""
    return (time.time() - float(h.get("at", time.time()))) / 60


def past_turn(h: dict | None) -> bool:
    return h is not None and held_min(h) > TURN_MIN


def taking(pid: int, now_gb: float, peak_gb: float) -> None:
    """Write what the job of slot pid `pid` uses now and its peak so far, for a waiter beside it."""
    try:
        TAKING.mkdir(parents=True, exist_ok=True)
        tmp = TAKING / f".{pid}.json"
        tmp.write_text(json.dumps({"now_gb": round(now_gb, 2), "peak_gb": round(peak_gb, 2), "at": time.time()}))
        os.replace(tmp, TAKING / f"{pid}.json")
    except OSError:
        pass


def may_take_again(h: dict | None) -> tuple[float, float]:
    """(GB the job of holder `h` may take again, its peak so far): its sampled peak less what it uses now,
    (0, 0) when it has written no sample or has ended."""
    if not h:
        return 0.0, 0.0
    try:
        pid = int(h["pid"])
        rec = json.loads((TAKING / f"{pid}.json").read_text())
        now, peak = float(rec.get("now_gb", 0)), float(rec.get("peak_gb", 0))
    except (KeyError, TypeError, ValueError, OSError):
        return 0.0, 0.0
    if not alive(pid):
        return 0.0, 0.0
    return round(max(0.0, peak - now), 1), peak


def taking_back(h: dict | None) -> tuple[float, str]:
    """(GB every running job past its turn may take again, the words for it): the slot's holder when it is past
    its turn, and each job that gave the slot up and still runs (past_jobs). A job that took over the slot is
    within its turn, and the one it took over from keeps running in PAST, so both are read."""
    jobs = ([h] if past_turn(h) else []) + [j for j in past_jobs() if not h or int(j["pid"]) != int(h["pid"])]
    got = [(j, *may_take_again(j)) for j in jobs]
    got = [(j, back, top) for j, back, top in got if back > 0]
    if not got:
        return 0.0, ""
    total = round(sum(back for _j, back, _top in got), 1)
    j, back, top = max(got, key=lambda g: g[1])
    if len(got) == 1:
        return total, (f"{j['chat']}: {j['what']}, past its turn, takes back the {back:g} GB under its peak "
                       f"of {top:g} GB")
    return total, (f"{len(got)} jobs past their turn take back {total:g} GB, the most {j['chat']}: {j['what']} "
                   f"with {back:g} GB under its peak of {top:g} GB")


def _ago(minutes: float) -> str:
    m = int(minutes)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def past_jobs() -> list[dict]:
    """The jobs that gave the slot up past their turn and still run; a file whose job ended goes."""
    out = []
    for f in sorted(PAST.glob("*.json")) if PAST.is_dir() else []:
        try:
            j = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if alive(int(j["pid"])):
            out.append(j)
        else:
            f.unlink(missing_ok=True)
    return out


def clear_stale() -> None:
    """A slot whose holder is gone is free; one made but never filled is free after a minute.

    A holder that gives the slot back removes its record and then the folder, so a look that finds
    the folder but no record may find no folder a moment later. That is a slot given back, not an
    error: it raised FileNotFoundError here, and the waiter that looked ended without running its job
    (about 1 job in 45 when twelve short jobs queued at once)."""
    for f in TAKING.glob("*.json") if TAKING.is_dir() else ():
        try:
            if not alive(int(f.stem)):
                f.unlink(missing_ok=True)       # a job killed before it could remove its own sample
        except (ValueError, OSError):
            pass
    if not SLOT.is_dir():
        return
    h = holder()
    if h is None:
        try:
            made = SLOT.stat().st_mtime
        except OSError:
            return
        if time.time() - made > 60:
            shutil.rmtree(SLOT, ignore_errors=True)
    elif not alive(int(h["pid"])):
        shutil.rmtree(SLOT, ignore_errors=True)


def waiters() -> list[dict]:
    """The live tickets, oldest first; a ticket whose waiter is gone is removed."""
    out = []
    for t in sorted(QUEUE.glob("*.json")) if QUEUE.is_dir() else []:
        try:
            w = json.loads(t.read_text())
        except (OSError, ValueError):
            continue
        if alive(int(w["waiter"])):
            out.append({**w, "ticket": t.name})
        else:
            t.unlink(missing_ok=True)
    return out


def last_held(chat: str) -> float:
    try:
        return (LAST / chat).stat().st_mtime
    except OSError:
        return 0.0


def turn_order(ws: list[dict]) -> list[dict]:
    """The waiters in the order they will get the slot: round robin across chats, the chat that
    held it least recently first, and each chat's own tickets oldest first."""
    by_chat: dict[str, list[dict]] = {}
    for w in ws:
        by_chat.setdefault(w["chat"], []).append(w)
    rounds = max((len(v) for v in by_chat.values()), default=0)
    chats = sorted(by_chat, key=lambda c: (last_held(c), by_chat[c][0]["ticket"]))
    return [by_chat[c][i] for i in range(rounds) for c in chats if i < len(by_chat[c])]


def _ticket(chat: str, what: str, holder_pid: int, need_gb: float | None = None) -> Path:
    """A waiter's ticket. `need_gb` is the free memory a job that declared its need asks, None for one that
    declared nothing."""
    QUEUE.mkdir(parents=True, exist_ok=True)
    t = QUEUE / f"{time.time_ns():020d}.{os.getpid()}.json"
    t.write_text(json.dumps({"chat": chat, "what": what, "waiter": os.getpid(),
                             "pid": holder_pid, "since": time.strftime("%H:%M"), "session": _session(),
                             "need_gb": need_gb}))
    return t


def _mark(ticket: Path, why: str, held: dict | None = None) -> None:
    """Write into this waiter's ticket what holds it back by its own gate ("" for nothing), so the
    others' turn order can pass over it, and since when its gate has held it and the most memory
    free in that time ({"at", "most_free_gb"}), for status. Written whole and renamed, so no reader
    sees half a file, and only when something changed."""
    try:
        w = json.loads(ticket.read_text())
    except (OSError, ValueError):
        return
    held = held or {}
    if w.get("short", "") == why and w.get("held") == held:
        return
    w["short"], w["held"] = why, held
    tmp = ticket.with_name(ticket.name + ".tmp")
    tmp.write_text(json.dumps(w))
    tmp.replace(ticket)


def wait_key(held: list[tuple[str, str]], why: str) -> tuple:
    """What a waiting line is about, so a waiter says a new one only when this changes. The gate's
    words carry figures that move on almost every poll (the memory free, the swap), and a job the gate
    held for hours printed a line each time one of them moved, thousands in one CI log. So while the
    gate holds a job, a line is about the kinds of reason (memory, pressure, swap, disk), and a new one
    is said when a kind comes or goes. The turn's words (who holds the slot, how many jobs go first)
    are said whenever they change."""
    if held:
        return ("gate", tuple(sorted({kind for kind, _words in held})))
    return ("turn", why)


def wait_turn(chat: str, what: str, holder_pid: int, need: float | None = None,
              declared: tuple[str, str] | None = None) -> bool:
    """Wait for this caller's turn, then take the slot for `holder_pid`. False when `holder_pid`
    ended while it waited (a CI worker that was cancelled). `need` is the memory the job declares
    (declared_need), and `declared` where that came from: the gate then asks gate_gb instead of
    MIN_FREE_GB."""
    ask = gate_gb(what, need)
    if need is not None and ask > need:
        say(f"{what} took {kept_peak(what):g} GB on its last run, over the {need:g} GB it declares, so it "
            f"waits for {ask:g} GB free")
        if declared:
            declared = (declared[0], f"{declared[1]}, raised to {ask:g} GB by its last run's peak")
    ticket = _ticket(chat, what, holder_pid, ask if need is not None else None)
    hook("asked", chat, what, holder_pid)
    took = False
    said: tuple | None = None
    # A long hold is said, once: a job that asks for more free memory than the machine ever has, or
    # waits on a machine short of room, would otherwise wait for hours with only its first waiting
    # line. While its gate holds it, the waiter keeps since when, what kinds of reason held it and
    # the most memory it saw free, says them once after OWN_LIMIT_NOTE_MIN, and status shows them
    # beside it. A moment with room starts the count again.
    held_since, most_free, noted, kinds = None, 0.0, False, set()
    try:
        while True:
            if not alive(holder_pid):
                return False
            clear_stale()
            m = machine()
            held = reasons(m, ask)
            # beside jobs past their turn, the room they may take again is not free
            back, words = taking_back(holder())
            if not held and back and m[0] - back < ask:
                held = [("memory", f"only {round(m[0] - back, 1):g} GB of memory free once {words} "
                                   f"(needs {ask:g})")]
            why = "; ".join(words for _kind, words in held)
            if why:
                held_since = held_since or time.time()
                most_free = max(most_free, m[0])
                kinds |= {kind for kind, _words in held}
                mins = (time.time() - held_since) / 60
                if mins >= OWN_LIMIT_NOTE_MIN and not noted:
                    say(held_note(why, kinds, mins, most_free, ask, declared))
                    noted = True
            else:
                held_since, most_free, noted, kinds = None, 0.0, False, set()
            # A waiter held by its own gate does not hold up the others. The gate's limits come from
            # each caller's own environment (one big job asks for HEAVY_MIN_FREE_GB=16), so a job at
            # the head of the queue that waits for room only it needs would stop every job behind
            # it. The turn goes to the first waiter that could start now.
            _mark(ticket, why, {"at": held_since, "most_free_gb": most_free} if held_since else None)
            order = turn_order([w for w in waiters() if not w.get("short")])
            h = holder()
            if not why and (not SLOT.exists() or past_turn(h)) and order and order[0]["ticket"] == ticket.name:
                if SLOT.exists():       # its holder is past its turn: it keeps running, and moves aside
                    PAST.mkdir(parents=True, exist_ok=True)
                    try:
                        os.replace(SLOT / "who.json", PAST / f"{int(h['pid'])}.json")
                    except OSError:
                        pass
                    shutil.rmtree(SLOT, ignore_errors=True)
                    say(f"{h['chat']}: {h['what']} has held the slot for {_ago(held_min(h))}, past its "
                        f"turn of {TURN_MIN:g} min; it keeps running, and this job starts beside it")
                try:
                    SLOT.mkdir(parents=True)
                except FileExistsError:
                    continue
                (SLOT / "who.json").write_text(json.dumps(
                    {"chat": chat, "what": what, "pid": holder_pid, "since": time.strftime("%H:%M"),
                     "at": time.time(), "session": _session(), "need_gb": need}))
                LAST.mkdir(parents=True, exist_ok=True)
                (LAST / chat).touch()
                took = True
                hook("admitted", chat, what, holder_pid)
                return True
            if not why:
                h = holder()
                names = [w["ticket"] for w in order]
                ahead = names.index(ticket.name) if ticket.name in names else 0
                why = (f"the slot is held by {h['chat']}: {h['what']}" if h else
                       f"{ahead} job(s) go first" if ahead else "the slot is being taken")
            key = wait_key(held, why)
            if key != said:
                say(f"waiting, {why}")
                said = key
            time.sleep(POLL)
    finally:
        ticket.unlink(missing_ok=True)
        if not took:
            hook("withdrawn", chat, what, holder_pid)


def release(pid: int) -> bool:
    """Give back the slot `pid` holds. True when it held it. A job past its turn holds nothing to give
    back, and its record goes; the HEAVY_HOOK command is told `released` in both cases, once."""
    past = PAST / f"{pid}.json"
    try:
        was = json.loads(past.read_text())
    except (OSError, ValueError):
        was = None
    past.unlink(missing_ok=True)
    h = holder()
    if h is not None and int(h["pid"]) == pid:
        shutil.rmtree(SLOT, ignore_errors=True)
        hook("released", h.get("chat", ""), h.get("what", ""), pid)
        return True
    if was is not None:
        hook("released", was.get("chat", ""), was.get("what", ""), pid)
    return False


def job_tree(pgid: int, table: dict | None = None) -> tuple[list[int], dict[int, int]]:
    """A job's processes: the members of its process group `pgid`, and every descendant of them in a
    group of its own, as pid -> that group. A child that makes a group or a session of its own (a build
    tool's worker process, a server started with setsid) leaves the job's group but is still the job's,
    found by its parent pid. Both are empty when the process table cannot be read. `table` is one
    already read (killguard.process_table), so a caller that reads more than this reads it once."""
    if pgid <= 1:
        return [], {}
    if table is None:
        table = killguard.process_table() or {}
    kids: dict[int, list[int]] = {}
    for pid, (ppid, _group, _cmd) in table.items():
        kids.setdefault(ppid, []).append(pid)
    members = sorted(pid for pid, (_ppid, group, _cmd) in table.items() if group == pgid)
    outside: dict[int, int] = {}
    todo = list(members)
    while todo:
        for child in kids.get(todo.pop(), ()):
            group = table[child][1]
            if group != pgid and child not in outside and child > 1:
                outside[child] = group
                todo.append(child)
    return members, outside


def still_running(strays: dict[int, int], table: dict | None = None) -> list[int]:
    """The pids of `strays` (pid -> the group each was seen in) that still run in that group. A pid that
    ended and came back as another process is in another group, and is not one of them."""
    if table is None:
        table = killguard.process_table() or {}
    return sorted(pid for pid, group in strays.items() if pid in table and table[pid][1] == group)


def group_gb(pgid: int, seen: dict[int, int] | None = None) -> float:
    """The memory of a job in GB: every process in its process group and every descendant of them in a
    group of its own (job_tree), as top's MEM (compressed included), read for each with proc_pid_rusage
    (footprint.py), far cheaper than asking `top`. With `seen` (pid -> group, kept by the caller from one
    sample to the next), each process found outside the group is added to it, and every one seen before
    that still runs in its group counts too: a worker whose parent in the job's group has ended is the
    init process's child, and a walk from the group no longer finds it."""
    table = killguard.process_table() or {}
    members, outside = job_tree(pgid, table)
    if seen is None:
        return round(total_gb(members + list(outside)), 1)
    seen.update(outside)
    return round(total_gb(sorted(set(members) | set(still_running(seen, table)))), 1)


def program(pid: int, table: dict) -> str:
    """The name of the program a process runs (its command's first word, without the path), or ''."""
    cmd = table.get(pid, (0, 0, ""))[2].split()
    return Path(cmd[0]).name if cmd else ""


def kept(pids: list[int], table: dict, keep: str = "") -> list[int]:
    """Which of a job's leftover processes HEAVY_KEEP_STRAYS keeps: all of them for 1, the ones whose
    program is named for a comma-separated list of names (adb,watchman), none when it is unset."""
    if keep == "1":
        return list(pids)
    names = {n.strip() for n in keep.split(",") if n.strip()}
    return [pid for pid in pids if program(pid, table) in names]


def named(pids: list[int], table: dict | None = None) -> str:
    """Each pid with the start of its command line, the program's path cut to its name, for one line."""
    if table is None:
        table = killguard.process_table() or {}
    said = []
    for pid in pids:
        cmd = table.get(pid, (0, 0, ""))[2].split()
        words = " ".join([Path(cmd[0]).name, *cmd[1:]]) if cmd else "?"
        said.append(f"{pid} {words[:60]}")
    return ", ".join(said)


def _send(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def signal_strays(strays: dict[int, int], sig: int) -> list[int]:
    """Send `sig` to each of a job's processes outside its group (pid -> the group it was seen in) that
    still runs in that group, and return those pids. A pid that ended and came back as another process
    is in another group, and is left alone. Signal 0 only asks which still run."""
    live = still_running(strays)
    if live:
        try:
            killguard.kill(live, sig, max_targets=len(live), send=_send)
        except killguard.Refused as e:
            say(f"did not signal {live}: {e}")
            return []
    return live


def _parent(pid: int) -> int | None:
    out = _out("/bin/ps", "-o", "ppid=", "-p", str(pid)).strip()
    return int(out) if out.isdigit() else None


def inside_holder() -> dict | None:
    """The slot's holder when this caller runs inside it, else None: the job exported HEAVY_HELD as
    the holder's pid, or the holder is this process or one of its ancestors (a step of a CI job that
    holds the slot for its worker)."""
    clear_stale()
    h = holder()
    if h is None:
        return None
    held = int(h["pid"])
    if os.environ.get("HEAVY_HELD", "") == str(held):
        return h
    pid: int | None = os.getpid()
    for _ in range(64):
        if pid is None or pid <= 1:
            break
        if pid == held:
            return h
        pid = _parent(pid)
    return None


def peak_of_children() -> float:
    """The biggest child's maximum resident size so far, in GB (macOS counts bytes, Linux KB)."""
    import resource   # noqa: PLC0415
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return round(rss / (1e9 if sys.platform == "darwin" else 1e6), 2)


def remember(chat: str, what: str, rc: int, peak: float, minutes: float, ended: str | None,
             need: float | None = None) -> Path:
    """Keep what a finished job took, in DONE/<pid>.json, with the need it declared (None for none)."""
    DONE.mkdir(parents=True, exist_ok=True)
    for old in sorted(DONE.glob("*.json"), key=lambda x: x.stat().st_mtime)[:-DONE_KEEP]:
        old.unlink(missing_ok=True)
    took = {"chat": chat, "what": what, "rc": rc, "peak_gb": peak, "minutes": round(minutes, 1),
            "ended": ended, "at": time.strftime("%Y-%m-%d %H:%M"), "end": round(time.time(), 2),
            "session": _session(), "need_gb": need}
    took["how"] = failed_how(took)
    (DONE / f"{os.getpid()}.json").write_text(json.dumps(took))
    return DONE / f"{os.getpid()}.json"


def failed_how(rec: dict) -> str | None:
    """Pure. How a finished job failed, in words that are the same each time it fails the same way:
    over the memory cap (whatever it reached past it), at its --max-hold, or its exit code. None
    when it succeeded."""
    ended = rec.get("ended") or ""
    cap = re.search(r"over the ([0-9.]+) GB cap", ended)
    if cap:
        return f"over the {cap.group(1)} GB memory cap"
    if "--max-hold" in ended:
        return "ended at its --max-hold"
    rc = int(rec.get("rc", 0))
    return None if rc == 0 else f"exit {rc}"


def done_jobs(folder: Path | None = None) -> list[dict]:
    """The finished jobs DONE (or `folder`) keeps, each with `end` (seconds since the epoch; a
    record written before `end` was kept has its file's time)."""
    folder = DONE if folder is None else Path(folder)
    out = []
    for f in folder.glob("*.json") if folder.is_dir() else []:
        try:
            j = json.loads(f.read_text())
            j.setdefault("end", f.stat().st_mtime)
            str(j["chat"]), str(j["what"]), float(j["end"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        out.append(j)
    return out


def streaks(records: list[dict], at_least: int = REPEAT_FAILS) -> list[dict]:
    """Pure. The jobs (one chat, one `what`) whose latest runs all failed the same way, at least
    `at_least` times in a row, newest first: {chat, what, how, count, first, last, session}, with
    `first` and `last` the end times of the first and last of those runs, and `session` the Claude
    Code session that started the last one (None when none did). A success, or a failure of another
    kind, ends a run of failures."""
    by: dict[tuple[str, str], list[dict]] = {}
    for r in sorted(records, key=lambda r: float(r["end"])):
        by.setdefault((str(r["chat"]), str(r["what"])), []).append(r)
    out = []
    for (chat, what), rs in by.items():
        how = failed_how(rs[-1])
        if how is None:
            continue
        n = 0
        for r in reversed(rs):
            if failed_how(r) != how:
                break
            n += 1
        if n >= at_least:
            out.append({"chat": chat, "what": what, "how": how, "count": n,
                        "first": float(rs[-n]["end"]), "last": float(rs[-1]["end"]),
                        "session": rs[-1].get("session")})
    return sorted(out, key=lambda s: -s["last"])


def failing(within_hours: float | None = None, at_least: int | None = None,
            folder: Path | None = None) -> list[dict]:
    """The jobs failing the same way again and again (streaks) whose last failure is within
    `within_hours` (HEAVY_REPEAT_HOURS, 24). `folder` is a done folder other than DONE, for a
    reader whose HEAVY_DIR is not set."""
    hours = REPEAT_HOURS if within_hours is None else within_hours
    since = time.time() - hours * 3600
    return [s for s in streaks(done_jobs(folder), REPEAT_FAILS if at_least is None else at_least)
            if s["last"] >= since]


def worth_saying(count: int, at_least: int = REPEAT_FAILS) -> bool:
    """Pure. A run of failures is said at its `at_least`-th failure and again each time it doubles,
    so a loop that retries all night is said a few times, not every run."""
    if at_least < 1 or count < at_least:
        return False
    n = at_least
    while n < count:
        n *= 2
    return n == count


def repeat_line(s: dict) -> str:
    """Pure. What a job failing the same way again and again says."""
    hint = (" Run it with HEAVY_MAX_GB set to what it needs, or make it use less." if "memory cap" in s["how"]
            else " Running it again unchanged will fail the same way.")
    return (f"{s['what']} has failed the same way {s['count']} times in a row ({s['how']}), for "
            f"{_ago((s['last'] - s['first']) / 60)}.{hint}")


def result_line(what: str, rc: int, peak: float, minutes: float, ended: str | None, last: str) -> str:
    """Pure. The one line --notify sends: what, how it ended, what it took, its last line."""
    how = f"was ended ({ended})" if ended else f"finished with exit {rc}"
    tail = f"; its last line: {last.strip()[:200]}" if last.strip() else ""
    return (f"From the heavy-job queue: {what} {how}, peak memory {peak:g} GB, {minutes:.1f} min{tail}. "
            "Nothing needed back.")


def notify(chat: str, text: str) -> bool:
    """Run HEAVY_NOTIFY with `chat` and `text` appended (for example a command that hands the line
    to the chat when its turn ends). Without HEAVY_NOTIFY there is nobody to tell, which is said."""
    cmd = os.environ.get("HEAVY_NOTIFY", "").split()
    if not cmd:
        say(f"no HEAVY_NOTIFY command is set, so {chat} was not told")
        return False
    try:
        r = subprocess.run([*cmd, chat, text], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        say(f"could not tell {chat}: {e}")
        return False
    if r.returncode != 0:
        say(f"could not tell {chat}: {(r.stderr or r.stdout).strip()[-200:]}")
    return r.returncode == 0


def self_queued() -> list[str]:
    """The program names in HEAVY_SELF_QUEUED, space or comma separated."""
    return [n for n in re.split(r"[\s,]+", os.environ.get("HEAVY_SELF_QUEUED", "")) if n]


def queues_itself(cmd: list[str], names: list[str] | None = None) -> str | None:
    """The first of `names` (HEAVY_SELF_QUEUED) that `cmd` runs, or None. A name counts as a word of the
    command (its last path part), inside one word that holds an inline script, and in the text of each word
    that names a file (the first SELF_QUEUED_READ bytes, one level down, and not a binary), leaving out its
    comment lines (the first non-space character is #). A name only counts whole: queue.sh is not in
    my-queue.sh or queue.sh.bak."""
    names = self_queued() if names is None else names
    if not names:
        return None
    pat = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(n) for n in names) + r")(?![\w.-])")
    for word in cmd:
        if os.path.basename(word) in names:
            return os.path.basename(word)
        found = pat.search(word)
        if found:
            return found.group(1)
        try:
            if not os.path.isfile(word):
                continue
            with open(word, "rb") as f:
                head = f.read(SELF_QUEUED_READ)
        except OSError:
            continue
        if b"\0" in head[:1024]:
            continue                        # a program, not a script
        # a comment that names a runner runs nothing: a line whose first non-space character is # is left out
        code = "\n".join(ln for ln in head.decode("utf-8", "replace").splitlines() if not ln.lstrip().startswith("#"))
        found = pat.search(code)
        if found:
            return found.group(1)
    return None


def run(chat: str, what: str, cmd: list[str], max_hold: float | None = None,
        notify_chat: bool = False, need_gb: str | None = None) -> int:
    """Run `cmd` as a heavy job: wait for the turn and for room, run it under the memory cap, and say what it
    took. `need_gb` is --need-gb's value; without it HEAVY_NEED_GB is read (declared_need)."""
    # A heavy job inside a heavy job runs at once. A job that calls the queue again (a script that
    # wraps its own steps) would wait for the slot its own parent holds, and nothing could give it
    # back. It runs in the outer job's process group (no new session), so the outer job's memory
    # cap still counts it and its stop still reaches it.
    inside = inside_holder()
    if inside is not None:
        say(f"running {what} inside {inside['chat']}: {inside['what']}, which holds the slot")
        try:
            # the caller's own command, in the caller's environment, as run() does below
            return subprocess.run(cmd, env=dict(os.environ)).returncode
        except OSError as e:
            say(f"could not run {cmd[0]}: {e}")
            return 127
    blocked = queues_itself(cmd)
    if blocked:
        say(f"REFUSED {what}: it runs {blocked}, which waits in a queue of its own, so it would hold the slot "
            f"while it waits there. This queues itself, so run it with claude-ops detach instead (HEAVY_SELF_QUEUED)")
        return 2
    need, declared = declared_need(need_gb)
    wait_turn(chat, what, os.getpid(), need, declared)
    say(f"running {what} for {chat} (memory cap {MAX_GB:g} GB"
        + (f", declared need {need:g} GB)" if need is not None else ")"))
    # the job is the caller's own command: it runs in the caller's environment, by design, plus
    # HEAVY_HELD, so a call it makes to the queue knows it already holds the slot
    last = [""]
    if notify_chat:
        # one stream through this process, so the job's last line can be told
        job = subprocess.Popen(cmd, env={**os.environ, "HEAVY_HELD": str(os.getpid())},
                               start_new_session=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
        import threading   # noqa: PLC0415

        def relay():
            for line in job.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                if line.strip():
                    last[0] = line
        threading.Thread(target=relay, daemon=True).start()
    else:
        job = subprocess.Popen(cmd, env={**os.environ, "HEAVY_HELD": str(os.getpid())},
                               start_new_session=True)

    # the job's processes in groups of their own, kept from each sample and each stop: once their parent
    # in the job's group has ended they are the init process's children, and no walk from the group finds them
    strays: dict[int, int] = {}

    def stop(sig=signal.SIGTERM):
        strays.update(job_tree(job.pid)[1])
        try:
            os.killpg(job.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        signal_strays(strays, sig)

    def end_job():
        """the cap's and --max-hold's end: SIGTERM, and SIGKILL after STOP_GRACE seconds for the job's
        group and for its processes outside it alike"""
        stop()
        try:
            job.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            stop(signal.SIGKILL)
            job.wait()
        end = time.time() + STOP_GRACE
        while time.time() < end and signal_strays(strays, 0):
            time.sleep(0.2)
        left = signal_strays(strays, signal.SIGKILL)
        if left:
            say(f"ended {len(left)} of {what}'s processes outside its group that outlived SIGTERM: {left}")

    def end_leftovers():
        """after the job's own process exits: what it left running (its group's other members, and its
        processes outside the group) still holds memory once the slot is free, so it is named and ended
        like a job over the cap, SIGTERM and then SIGKILL, except what HEAVY_KEEP_STRAYS keeps"""
        table = killguard.process_table() or {}
        members, outside = job_tree(job.pid, table)
        strays.update(outside)
        group_of = {**{pid: job.pid for pid in members}, **strays}
        left = sorted(set(members) | set(still_running(strays, table)))
        keep = kept(left, table, KEEP_STRAYS)
        if keep:
            say(f"{what} left {len(keep)} process(es) running, kept as HEAVY_KEEP_STRAYS={KEEP_STRAYS} asks: "
                f"{named(keep, table)}")
        ending = {pid: group_of[pid] for pid in left if pid not in keep}
        if not ending:
            return
        say(f"{what} left {len(ending)} process(es) running when it ended, so they are ended too "
            f"(HEAVY_KEEP_STRAYS=1, or a list of program names, keeps them): {named(sorted(ending), table)}")
        signal_strays(ending, signal.SIGTERM)
        end = time.time() + STOP_GRACE
        while time.time() < end and signal_strays(ending, 0):
            time.sleep(0.2)
        if signal_strays(ending, signal.SIGKILL):
            say(f"and SIGKILL for what outlived SIGTERM by {STOP_GRACE:g} s")

    def forward(signum, _frame):
        stop()
        release(os.getpid())
        raise SystemExit(128 + signum)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, forward)
    over, ended, began, peak = None, False, time.time(), 0.0
    try:
        while True:
            try:
                rc = job.wait(timeout=CHECK)
                end_leftovers()
                break
            except subprocess.TimeoutExpired:
                pass
            if max_hold is not None and time.time() - began > max_hold * 60:
                ended = True
                end_job()
                rc = 124
                break
            used = group_gb(job.pid, strays)
            peak = max(peak, used)
            taking(os.getpid(), used, peak)
            if used > MAX_GB:
                over = used
                end_job()
                rc = 137
                break
    finally:
        release(os.getpid())
        (TAKING / f"{os.getpid()}.json").unlink(missing_ok=True)
    time.sleep(0.2 if notify_chat else 0)      # the relay prints the job's last lines first
    peak = round(max(peak, peak_of_children()), 2)
    minutes = (time.time() - began) / 60
    why = None
    if ended:
        why = f"its --max-hold of {max_hold:g} min"
        say(f"ENDED {what}: it ran for its --max-hold of {max_hold:g} min (peak memory {peak:g} GB)")
    if over is not None:
        why = f"{over} GB, over the {MAX_GB:g} GB cap"
        say(f"ENDED {what}: it used {over} GB, over the {MAX_GB:g} GB cap. Rework it to use less "
            "(stream, one part at a time), or run it with HEAVY_MAX_GB set to what it needs.")
    if why is None:
        say(f"{what} is done: exit {rc}, peak memory {peak:g} GB, {minutes:.1f} min")
    remember(chat, what, rc, peak, minutes, why, need)
    judge_need(chat, what, need, peak)
    again = next((s for s in streaks(done_jobs()) if (s["chat"], s["what"]) == (chat, what)), None)
    if again is not None and not worth_saying(again["count"]):
        again = None
    if again is not None:
        say(f"AGAIN: {repeat_line(again)}")
    if notify_chat:
        notify(chat, result_line(what, rc, peak, minutes, why, last[0]))
        if again is not None:
            notify(chat, f"From the heavy-job queue: {repeat_line(again)}")
    return rc


def status() -> int:
    m = machine()
    free, swap, disk, level, disk_gb = m
    rated = {1.0: "normal", 2.0: "raised", 4.0: "critical"}.get(level, f"level {level:g}")
    room = f" ({disk_gb:g} GB)" if disk_gb is not None else ""
    print(f"machine: {free} GB memory free, swap {swap} GB, disk {disk}% free{room}, pressure {rated}")
    clear_stale()
    h = holder()
    if h:
        late = (f", past its turn of {TURN_MIN:g} min: the next waiter starts beside it"
                if past_turn(h) else "")
        need = f", declared need {float(h['need_gb']):g} GB" if h.get("need_gb") is not None else ""
        print(f"slot: {h['chat']}: {h['what']} (pid {h['pid']}, since {h['since']}, held "
              f"{_ago(held_min(h))}{need}{late})")
    else:
        print("slot: free")
    for j in past_jobs():
        print(f"past its turn, still running: {j['chat']}: {j['what']} (pid {j['pid']}, since "
              f"{j['since']}, {_ago(held_min(j))})")
    order = turn_order(waiters())
    if order:
        print("next, in turn:")
        for i, w in enumerate(order, 1):
            held = f"; held by its gate: {w['short']}" if w.get("short") else ""
            gate = w.get("held") or {}
            if w.get("short") and gate.get("at"):
                held += (f", for {_ago((time.time() - float(gate['at'])) / 60)}, the most memory free in "
                         f"that time {float(gate.get('most_free_gb', 0)):g} GB")
            asks = f", asks {float(w['need_gb']):g} GB free" if w.get("need_gb") is not None else ""
            print(f"  {i}. {w['chat']}: {w['what']} (pid {w['pid']}, waiting since {w['since']}{asks}{held})")
    for s in failing():
        print(f"failing the same way: {s['chat']}: {s['what']} ({s['how']}, {s['count']} times in a row, "
              f"the last at {time.strftime('%H:%M', time.localtime(s['last']))})")
    why = short(m)
    if why:
        print(f"short: {why}")
    return 0


USAGE = ("usage: claude-ops heavy <chat> <what> [--max-hold <minutes>] [--notify] [--need-gb <GB>] -- <command...> |"
         " claude-ops heavy hold <chat> <what> --pid <pid> |"
         " claude-ops heavy release --pid <pid> | claude-ops heavy status")


_hook_said = [False]


def hook(event: str, chat: str, what: str, pid: int) -> bool:
    """Tell the HEAVY_HOOK command that this job was `asked`, `admitted`, `released` or `withdrawn`,
    with the chat, the job and the pid that holds or would hold the slot. Without HEAVY_HOOK nobody is
    told. It is only told: a hook that fails, hangs past HOOK_TIMEOUT or cannot start returns False
    and the job goes on, and the first such failure in a process is said."""
    cmd = os.environ.get("HEAVY_HOOK", "").split()
    if not cmd:
        return False
    why = ""
    try:
        r = subprocess.run([*cmd, event, chat, what, str(pid)], capture_output=True, text=True,
                           timeout=HOOK_TIMEOUT)
        if r.returncode != 0:
            why = f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[-160:]}"
    except subprocess.TimeoutExpired:
        why = f"it took longer than {HOOK_TIMEOUT:g} s"
    except (OSError, subprocess.SubprocessError) as e:
        why = str(e)
    if why:
        hook_log(event, chat, what, pid, why)
    if why and not _hook_said[0]:
        _hook_said[0] = True
        say(f"the HEAVY_HOOK command was not told that {what} was {event} ({why}); the job goes on")
    return not why


def hook_log(event: str, chat: str, what: str, pid: int, why: str) -> None:
    """One line in HEAVY_HOOK_LOG, when it is set, for a hook call that failed: a hook that timed out was
    ended before it could write anything, and the job's own output is often kept nowhere. A log that
    cannot be written is left alone, since the hook only tells."""
    path = os.environ.get("HEAVY_HOOK_LOG")
    if not path:
        return
    line = (f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {event} chat={chat} pid={pid} "
            f"what={json.dumps(what)} why={json.dumps(why)}\n")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _quit(signum, _frame):
    raise SystemExit(128 + signum)     # so a stopped waiter still takes its ticket with it


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, _quit)
    if argv[:1] == ["status"]:
        return status()
    if argv[:1] == ["hold"] and len(argv) == 5 and argv[3] == "--pid":
        return 0 if wait_turn(argv[1], argv[2], int(argv[4]), *declared_need()) else 1
    if argv[:1] == ["release"] and len(argv) == 3 and argv[1] == "--pid":
        release(int(argv[2]))
        return 0
    if len(argv) >= 4 and "--" in argv[2:]:
        cut = argv.index("--", 2)
        opts, cmd, minutes, tell, need = argv[2:cut], argv[cut + 1:], None, False, None
        while opts and cmd:
            if opts[0] == "--notify":
                tell, opts = True, opts[1:]
            elif opts[0] == "--need-gb" and len(opts) >= 2:
                need, opts = opts[1], opts[2:]
            elif opts[0] == "--max-hold" and len(opts) >= 2:
                try:
                    minutes = float(opts[1])
                except ValueError:
                    minutes = -1.0
                if minutes <= 0:
                    break
                opts = opts[2:]
            else:
                break
        if not opts and cmd:
            return run(argv[0], argv[1], cmd, max_hold=minutes, notify_chat=tell, need_gb=need)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
