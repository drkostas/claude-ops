"""Whether a service on this Mac is up, read from launchd and from a real connection.

A launchd label that says "running" can still be a service that does not answer, and a service that
answers can have no process launchd calls running (a socket-activated daemon starts on demand). So
a reading here has two parts, what launchd says and whether the service answers when it is asked,
and the state follows the answer:

- `up`       the service answered a connection.
- `down`     it was asked and did not answer (refused, timed out, no such socket).
- `unknown`  nothing could be asked (the spec names no port or socket, or the address is not one
             this check may connect to).

A caller that restarts services acts only on `down`, never on `up` or `unknown`, so a restart is
never aimed at a service that is working.

    from claude_ops import service_check
    spec = service_check.Spec("web", label="com.example.web", port=8080)
    print(service_check.check(spec).state)

Only a connection is made. Nothing is sent to the service and nothing is signalled or restarted.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

UP, DOWN, UNKNOWN = "up", "down", "unknown"


@dataclass(frozen=True)
class Spec:
    """One service to read. `label` is a launchd label and `domain` the launchd domain it lives in
    (for example `gui/501` or `system`). `port` is a TCP port on `host`, `socket_path` a unix socket.
    Give at most one of port and socket_path."""
    name: str
    label: str | None = None
    domain: str | None = None
    host: str = "127.0.0.1"
    port: int | None = None
    socket_path: str | None = None


@dataclass(frozen=True)
class Reading:
    name: str
    state: str
    launchd: str | None      # running, waiting, not loaded, or None when launchd was not asked
    answers: bool | None     # True, False, or None when nothing could be asked
    detail: str = ""


def parse_launchd(text: str) -> str:
    """`launchctl print` output as "running" (it shows a pid), "waiting" (loaded, no process now)."""
    if re.search(r"^\s*pid\s*=\s*\d+", text, re.M):
        return "running"
    return "waiting"


def launchd_state(spec: Spec, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> str | None:
    if not spec.label:
        return None
    domain = spec.domain or f"gui/{os.getuid()}"
    try:
        r = run(["launchctl", "print", f"{domain}/{spec.label}"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return "not loaded"
    return parse_launchd(r.stdout)


def _connect(spec: Spec, timeout: float) -> bool:
    if spec.socket_path:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        target: object = spec.socket_path
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target = (spec.host, spec.port)
    s.settimeout(timeout)
    try:
        s.connect(target)
        return True
    except OSError:
        return False
    finally:
        s.close()


def answers(spec: Spec, timeout: float = 3.0, connect: Callable[[Spec, float], bool] = _connect) -> bool | None:
    """True when a connection is accepted, False when it is not, None when the spec names no address."""
    if spec.port is None and not spec.socket_path:
        return None
    if spec.port is not None and spec.socket_path:
        raise ValueError("a spec names a port or a socket, not both")
    return connect(spec, timeout)


def check(spec: Spec, timeout: float = 3.0, run=subprocess.run, connect=_connect) -> Reading:
    state_l = launchd_state(spec, run)
    ok = answers(spec, timeout, connect)
    if ok is None:
        return Reading(spec.name, UNKNOWN, state_l, None, "no port or socket to ask")
    return Reading(spec.name, UP if ok else DOWN, state_l, ok, "" if ok else "no answer")
