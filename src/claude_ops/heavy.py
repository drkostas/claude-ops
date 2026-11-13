"""heavy — one heavy job at a time on a shared machine, taken in turn across chats, and none while
memory or disk is short.

    claude-ops heavy <chat> <what> -- <command...>     wait for the turn, run under the memory cap
    claude-ops heavy <chat> <what> --max-hold <minutes> -- <command...>   the same, ended after that long
    claude-ops heavy <chat> <what> --notify -- <command...>   the same, and the result goes to <chat>
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

The state lives in HEAVY_DIR (/tmp/claude-ops-heavy). Every caller on the machine must use the same.
macOS only: the memory readings come from vm_stat, sysctl and proc_pid_rusage.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .footprint import total_gb

DIR = Path(os.environ.get("HEAVY_DIR", "/tmp/claude-ops-heavy"))
SLOT, QUEUE, LAST = DIR / "slot", DIR / "queue", DIR / "last"
#: jobs that held the slot past their turn and gave it up, still running, one file per pid
PAST = DIR / "past"
#: jobs that finished, one file per pid: what each took (peak memory, minutes, exit code)
DONE = DIR / "done"
#: how many finished jobs DONE keeps (about 200 bytes each, so well under its 1 MB)
DONE_KEEP = 200


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
TURN_MIN = _num("HEAVY_TURN_MIN", 60)
#: how long the gate may hold a waiter before it is told what holds it
OWN_LIMIT_NOTE_MIN = _num("HEAVY_OWN_LIMIT_NOTE_MIN", 60)
#: the settings behind each kind of reason the gate gives, to tell a job's own limit from the machine
LIMIT_VARS = {"memory": ("HEAVY_MIN_FREE_GB",), "pressure": (),
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


def reasons(m: tuple) -> list[tuple[str, str]]:
    """What holds a heavy job on a machine read as `m` (machine()'s tuple), each as (kind, words), the kind
    being memory, pressure, swap or disk. Empty when it has room."""
    free, swap, disk = m[:3]
    level = m[3] if len(m) > 3 else 1.0
    disk_gb = m[4] if len(m) > 4 else None
    why = []
    if free < MIN_FREE_GB:
        why.append(("memory", f"only {free} GB of memory free (needs {MIN_FREE_GB:g})"))
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


def held_note(why: str, kinds: set[str], minutes: float, most_free: float) -> str:
    """What a waiter says once its gate has held it for OWN_LIMIT_NOTE_MIN: for how long, what holds it in the
    gate's own words, whether that is a limit set in the job's own environment or the machine, and what would
    let it start."""
    own = [v for k in sorted(kinds) for v in LIMIT_VARS.get(k, ()) if v in os.environ]
    note = (f"the gate has held this job for {_ago(minutes)}: {why}. "
            + (f"That is this job's own limit ({', '.join(f'{v}={os.environ[v]}' for v in own)})"
               if own else "That is the machine, not a limit this job set"))
    if "memory" in kinds:
        note += (f", and the most memory free in that time was {most_free:g} GB against the "
                 f"{MIN_FREE_GB:g} GB it asks for")
    if own:
        return note + f". Lower {' or '.join(own)} to what the job needs, or stop the job"
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


def _ticket(chat: str, what: str, holder_pid: int) -> Path:
    QUEUE.mkdir(parents=True, exist_ok=True)
    t = QUEUE / f"{time.time_ns():020d}.{os.getpid()}.json"
    t.write_text(json.dumps({"chat": chat, "what": what, "waiter": os.getpid(),
                             "pid": holder_pid, "since": time.strftime("%H:%M"), "session": _session()}))
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


def wait_turn(chat: str, what: str, holder_pid: int) -> bool:
    """Wait for this caller's turn, then take the slot for `holder_pid`. False when `holder_pid`
    ended while it waited (a CI worker that was cancelled)."""
    ticket = _ticket(chat, what, holder_pid)
    hook("asked", chat, what, holder_pid)
    took = False
    said = ""
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
            held = reasons(m)
            why = "; ".join(words for _kind, words in held)
            if why:
                held_since = held_since or time.time()
                most_free = max(most_free, m[0])
                kinds |= {kind for kind, _words in held}
                mins = (time.time() - held_since) / 60
                if mins >= OWN_LIMIT_NOTE_MIN and not noted:
                    say(held_note(why, kinds, mins, most_free))
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
                     "at": time.time(), "session": _session()}))
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
            if why != said:
                say(f"waiting, {why}")
                said = why
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


def group_gb(pgid: int) -> float:
    """The memory of every process in a process group, in GB: top's MEM (compressed included), read
    for each member with proc_pid_rusage (footprint.py), far cheaper than asking `top`."""
    members = []
    for line in _out("/bin/ps", "-A", "-o", "pid=,pgid=").splitlines():
        p = line.split()
        if len(p) == 2 and p[1] == str(pgid) and p[0].isdigit():
            members.append(int(p[0]))
    return round(total_gb(members), 1)


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


def remember(chat: str, what: str, rc: int, peak: float, minutes: float, ended: str | None) -> Path:
    """Keep what a finished job took, in DONE/<pid>.json."""
    DONE.mkdir(parents=True, exist_ok=True)
    for old in sorted(DONE.glob("*.json"), key=lambda x: x.stat().st_mtime)[:-DONE_KEEP]:
        old.unlink(missing_ok=True)
    took = {"chat": chat, "what": what, "rc": rc, "peak_gb": peak, "minutes": round(minutes, 1),
            "ended": ended, "at": time.strftime("%Y-%m-%d %H:%M"), "end": round(time.time(), 2),
            "session": _session()}
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


def run(chat: str, what: str, cmd: list[str], max_hold: float | None = None,
        notify_chat: bool = False) -> int:
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
    wait_turn(chat, what, os.getpid())
    say(f"running {what} for {chat} (memory cap {MAX_GB:g} GB)")
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

    def stop(sig=signal.SIGTERM):
        try:
            os.killpg(job.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

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
                break
            except subprocess.TimeoutExpired:
                pass
            if max_hold is not None and time.time() - began > max_hold * 60:
                ended = True
                stop()
                try:
                    job.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    stop(signal.SIGKILL)
                    job.wait()
                rc = 124
                break
            used = group_gb(job.pid)
            peak = max(peak, used)
            if used > MAX_GB:
                over = used
                stop()
                try:
                    job.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    stop(signal.SIGKILL)
                    job.wait()
                rc = 137
                break
    finally:
        release(os.getpid())
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
    remember(chat, what, rc, peak, minutes, why)
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
        print(f"slot: {h['chat']}: {h['what']} (pid {h['pid']}, since {h['since']}, held "
              f"{_ago(held_min(h))}{late})")
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
            print(f"  {i}. {w['chat']}: {w['what']} (pid {w['pid']}, waiting since {w['since']}{held})")
    for s in failing():
        print(f"failing the same way: {s['chat']}: {s['what']} ({s['how']}, {s['count']} times in a row, "
              f"the last at {time.strftime('%H:%M', time.localtime(s['last']))})")
    why = short(m)
    if why:
        print(f"short: {why}")
    return 0


USAGE = ("usage: claude-ops heavy <chat> <what> [--max-hold <minutes>] [--notify] -- <command...> |"
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
        return 0 if wait_turn(argv[1], argv[2], int(argv[4])) else 1
    if argv[:1] == ["release"] and len(argv) == 3 and argv[1] == "--pid":
        release(int(argv[2]))
        return 0
    if len(argv) >= 4 and "--" in argv[2:]:
        cut = argv.index("--", 2)
        opts, cmd, minutes, tell = argv[2:cut], argv[cut + 1:], None, False
        while opts and cmd:
            if opts[0] == "--notify":
                tell, opts = True, opts[1:]
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
            return run(argv[0], argv[1], cmd, max_hold=minutes, notify_chat=tell)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
