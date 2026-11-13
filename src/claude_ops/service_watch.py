"""When a service that stopped answering may be restarted, and when it must not be.

A restarter that acts on one refused connection will restart a service that is only busy, and a
restarter that turns on a service a person switched off undoes that person's choice. This module is
the decision between them, with no side effects. It takes the readings of one service, the setting of
the service and the restarts already tried, and answers with one action. The caller reads the service
(see `service_check`), carries the action out and records what it did.

The actions:

- `none`     the service answers, or nothing could be asked of it. Nothing to do.
- `recheck`  the last reading says it does not answer, but that is one reading, or the readings are
             too close together. Ask again at `wait_until`.
- `wait`     a restart was tried a short time ago. Give the service time to start.
- `restart`  at least two readings in a row say it does not answer, `min_gap` seconds apart or more,
             it is switched on, and few restarts have failed. Restart it.
- `off`      it is down but its setting is off (disabled, or not loaded). Do not restart it. The
             caller raises a finding for the person.
- `unknown`  it is down but its setting could not be read. Do not restart it.
- `give_up`  too many restarts failed in the window. Stop trying and raise a finding. The action
             stays `give_up` until a reading says the service answers again.

    from claude_ops import service_watch as w
    d = w.decide(readings, now, setting="on", restarts=tried)
    if d.action == "restart":
        ...

A reading is `Reading(at, answers)` with `answers` True, False or None (nothing could be asked). A
restart is `Restart(at, recovered)`, where `recovered` says whether a later reading answered.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from claude_ops import service_check

NONE, RECHECK, WAIT, RESTART, OFF, UNKNOWN, GIVE_UP = (
    "none", "recheck", "wait", "restart", "off", "unknown", "give_up")


@dataclass(frozen=True)
class Reading:
    at: float                  # seconds since the epoch
    answers: bool | None       # True, False, or None when nothing could be asked


@dataclass(frozen=True)
class Restart:
    at: float
    recovered: bool            # a later reading answered


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    wait_until: float | None = None   # for `recheck` and `wait`


def decide(readings: Sequence[Reading], now: float, setting: str | None,
           restarts: Sequence[Restart] = (), *, min_gap: float = 60.0, window: float = 600.0,
           max_failed: int = 3, settle: float = 30.0) -> Decision:
    """The action for one service. `setting` is "on", "off" or None (could not be read).

    `readings` are in time order. `min_gap` is the least time between the first and the last of the
    readings that count as down. `window` and `max_failed` give up after that many restarts that were
    not followed by an answer, inside `window` seconds. `settle` is how long a restart is given before
    the next one."""
    if not readings:
        return Decision(NONE, "no reading")
    last = readings[-1]
    if last.answers is None:
        return Decision(NONE, "nothing could be asked")
    if last.answers:
        return Decision(NONE, "it answers")

    run: list[Reading] = []
    for r in reversed(readings):
        if r.answers is not False:
            break
        run.append(r)
    run.reverse()
    if len(run) < 2 or run[-1].at - run[0].at < min_gap:
        return Decision(RECHECK, "one reading is not enough", run[0].at + min_gap)

    failed = [t for t in restarts if not t.recovered and now - t.at <= window]
    if len(failed) >= max_failed:
        return Decision(GIVE_UP, f"{len(failed)} restarts in {int(window)} seconds did not bring it back")
    if setting == "off":
        return Decision(OFF, "it is switched off, so it is not restarted")
    if setting != "on":
        return Decision(UNKNOWN, "its setting could not be read, so it is not restarted")
    if restarts and now - restarts[-1].at < settle:
        return Decision(WAIT, "a restart was tried a moment ago", restarts[-1].at + settle)
    return Decision(RESTART, f"{len(run)} readings in a row say it does not answer")


_DISABLED = re.compile(r'"([^"]+)"\s*=>\s*(?:true|disabled)\b')


def setting(spec: service_check.Spec, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run
            ) -> str | None:
    """"on" when launchd has the service enabled and loaded, "off" when it is disabled or not loaded,
    None when launchd could not be read. A service with no launchd label has no switch, so it is "on".
    Reading needs no special rights, and nothing is changed."""
    if not spec.label:
        return "on"
    domain = spec.domain or f"gui/{os.getuid()}"
    try:
        r = run(["launchctl", "print-disabled", domain], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    for label in _DISABLED.findall(r.stdout):
        if label == spec.label:
            return "off"
    state = service_check.launchd_state(spec, run)
    if state is None:
        return None
    return "off" if state == "not loaded" else "on"
