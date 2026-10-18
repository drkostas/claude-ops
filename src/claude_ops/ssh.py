"""One way to run a command on another machine over ssh, and to copy files there.

Code that reaches other machines tends to spell its own ssh command each time, each with its own
timeouts and its own idea of what an unreachable host looks like: one caller treats exit 255 as
unknown, another catches an exception, a third reads a failed connection as "the command failed".
Here a host that could not be reached always comes back the same way: a CompletedProcess whose
return code is UNREACHABLE (255, ssh's own code for a connection that failed before the command
ran), with the reason in stderr. Nothing raises, so no caller has to know which exceptions ssh and
subprocess can throw.

Nothing secret belongs on a command line. Pass a password the remote command reads (`sudo -S`, say)
as `stdin`, where no process listing shows it.

`peer_online` asks Tailscale whether a host is online in its own view, a second witness for telling
"the host is down" apart from "this machine could not reach it just now".

    r = run("server", "uptime", env=env)
    if r.returncode == UNREACHABLE: ...
"""
from __future__ import annotations

import json
import subprocess

#: ssh's exit status when the connection failed before the command ran: nothing ran, so the outcome
#: of the command is unknown, not failed
UNREACHABLE = 255

#: never prompt (a scheduled job has nobody to answer), and give up on a dead host quickly
OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def _unreached(argv: list[str], exc: BaseException) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, UNREACHABLE, "", f"{type(exc).__name__}: {exc}")


def run(host: str, command: str, *, timeout: float = 25, stdin: str | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """`command` on `host` (a name ~/.ssh/config knows, or an address), captured as text. `stdin` is
    fed to the remote command when given; otherwise stdin is closed, so nothing can prompt. A timeout
    or a failure to start ssh comes back as UNREACHABLE with the reason in stderr. `env` is the
    environment ssh runs with (None inherits)."""
    argv = ["ssh", *OPTS, host, command]
    feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, **feed)
    except (subprocess.SubprocessError, OSError) as e:
        return _unreached(argv, e)


def copy(host: str, paths: list[str], dest: str, *, timeout: float = 30,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """`paths` to `host:dest` over scp, quietly. The same contract as `run`."""
    argv = ["scp", "-q", *OPTS, *paths, f"{host}:{dest}"]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)
    except (subprocess.SubprocessError, OSError) as e:
        return _unreached(argv, e)


def peer_online(host: str, *, tailscale: str = "tailscale", env: dict[str, str] | None = None,
                run=subprocess.run) -> bool | None:
    """Whether `host` is online in Tailscale's own view of the network. None when that cannot be
    told: Tailscale is not installed, the host's address (as `ssh -G` resolves it) is not a
    tailnet peer, or the answer did not parse. `tailscale` is the command to ask; `run` stands in
    for subprocess.run in tests."""
    try:
        g = run(["ssh", "-G", host], capture_output=True, text=True, timeout=10, env=env)
        ip = next((ln.split()[1] for ln in g.stdout.splitlines() if ln.startswith("hostname ")), None)
        if not ip:
            return None
        st = run([tailscale, "status", "--json"], capture_output=True, text=True, timeout=15, env=env)
        peers = json.loads(st.stdout or "{}").get("Peer") or {}
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    for peer in peers.values():
        if ip in (peer.get("TailscaleIPs") or []):
            return bool(peer.get("Online"))
    return None
