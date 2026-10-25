"""mcp_containers — stop the MCP server containers a chat left behind.

A Claude Code chat can run an MCP server as `docker run -i --rm <image>`. The docker client runs
under the chat and attaches to the container's stdin. When the chat ends by a signal (a restart, a
crash), the client dies with it, but the container keeps running: `--rm` removes a container only
when it exits, and the server waits on a stdin nobody closes. Over weeks they add up (nine from two
months on one Mac, each holding its memory), and `docker ps` stops telling what is really in use.

Only a container started as `docker run --rm` is looked at: one docker will remove when it exits
(AutoRemove) and that no compose project owns. A service a compose file runs has no client by
design and is never an orphan. ⛔ The first version read every running container, and `--stop` took
nine services down (a wiki, its database, a notifier) for a minute before they were started again.

Such a container is matched to a live `docker run` client of the same image whose start is
within a few seconds of the container's creation. One with no client is an orphan. `ps -o lstart`
prints wall-clock time, and in the hour the clocks go back a wall time names two moments: a client
started then is tried at both. A container younger than `--min-age` minutes is never touched: its
client may still be starting.

    claude-ops mcp-containers                 # list the orphans (id, image, age)
    claude-ops mcp-containers --stop          # and stop them (docker stop; --rm then removes them)
    claude-ops mcp-containers --min-age 120   # the age below which a container is left alone (minutes)
    claude-ops mcp-containers --json          # the same, as one JSON object
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

#: docker ps prints this (the zone name after the offset is ignored)
PS_FORMAT = "{{.ID}}\t{{.CreatedAt}}\t{{.Image}}\t{{.Command}}"
#: docker inspect prints this: whether docker removes the container when it exits (--rm), and the
#: compose project that owns it, if any
INSPECT_FORMAT = "{{.Id}}\t{{.HostConfig.AutoRemove}}\t{{index .Config.Labels \"com.docker.compose.project\"}}"
#: how far apart a container's creation and its client's start may be, in seconds
SLACK_S = 20
DEFAULT_MIN_AGE_MIN = 120

Runner = Callable[..., subprocess.CompletedProcess]


def parse_containers(text: str) -> list[dict]:
    """Running containers from `docker ps --format PS_FORMAT`: {id, created (epoch), image, command}.
    A line that does not parse is skipped."""
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cid, created, image = parts[0].strip(), parts[1].strip(), parts[2].strip()
        when = _docker_time(created)
        if not cid or when is None or not image:
            continue
        out.append({"id": cid, "created": when, "image": image,
                    "command": parts[3].strip() if len(parts) > 3 else ""})
    return out


def _docker_time(text: str) -> float | None:
    """'2026-10-25 05:06:27 +0200 CEST' -> epoch seconds; None when it does not parse."""
    words = text.split()
    if len(words) < 3:
        return None
    try:
        return datetime.strptime(" ".join(words[:3]), "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def parse_inspect(text: str) -> dict[str, dict]:
    """`docker inspect --format INSPECT_FORMAT` -> {id prefix (12): {auto_remove, compose}}."""
    out = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip():
            continue
        compose = parts[2].strip() if len(parts) > 2 else ""
        if compose in ("<no value>", "<nil>"):      # how docker's template prints a label that is not there
            compose = ""
        out[parts[0].strip()[:12]] = {"auto_remove": parts[1].strip().lower() == "true", "compose": compose}
    return out


def run_rm_only(containers: list[dict], inspected: dict[str, dict]) -> list[dict]:
    """The containers started as `docker run --rm` and owned by no compose project. One docker
    inspect did not describe is left out: unknown is never an orphan."""
    out = []
    for c in containers:
        i = inspected.get(c["id"][:12])
        if i and i["auto_remove"] and not i["compose"]:
            out.append({**c, **i})
    return out


def parse_clients(text: str) -> list[dict]:
    """Live `docker run` clients from `ps -Ao lstart=,command=`: {starts (the epochs the wall-clock
    start can mean, usually one), image, command}. The image is the last word of the command, which is
    where `docker run [options] image` puts it when the server takes no arguments of its own."""
    out = []
    for line in text.splitlines():
        if len(line) < 25 or " docker run " not in " " + line[24:]:
            continue
        starts = _ps_times(line[:24])
        command = line[24:].strip()
        if not starts or not command:
            continue
        out.append({"starts": starts, "image": command.split()[-1], "command": command})
    return out


def _ps_times(text: str) -> list[float]:
    """'Sun Oct 25 03:53:22 2026' -> the epoch seconds that wall time can mean in the local zone: one,
    or two an hour apart in the repeated hour of the autumn change (each reading is kept only when
    the clock shows that wall time for it). [] when the text does not parse."""
    try:
        st = time.strptime(text.strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return []
    out = []
    for isdst in (0, 1):
        t = time.mktime(st[:8] + (isdst,))
        if time.localtime(t)[:6] == st[:6]:
            out.append(t)
    return sorted(set(out)) or [time.mktime(st)]


def _gap(created: float, client: dict) -> float:
    return min(abs(created - s) for s in client["starts"])


def orphans(containers: list[dict], clients: list[dict], now: float | None = None, *,
            slack: float = SLACK_S, min_age_s: float = DEFAULT_MIN_AGE_MIN * 60) -> list[dict]:
    """The containers no live client of the same image could have started, each with its `age_s`.
    A client covers every container of its image created within `slack` of any moment its start
    can mean: a client in the repeated hour has two, and a container made an hour before the right
    one must not be read as the orphan while the right one is (a client claiming one container
    did exactly that). An orphan is a container nobody could have started; a real orphan made
    within seconds of a live client is left running, the safe side. A container younger than
    `min_age_s` is left out: its client may still be starting."""
    now = time.time() if now is None else now
    out = []
    for c in sorted(containers, key=lambda c: c["created"]):
        age = now - c["created"]
        if any(k["image"] == c["image"] and _gap(c["created"], k) <= slack for k in clients):
            continue
        if age < min_age_s:
            continue
        out.append({**c, "age_s": round(age)})
    return out


def read(runner: Runner = subprocess.run, env: Mapping[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """(containers, clients) from this machine: only the containers started as `docker run --rm`
    outside any compose project, and the live `docker run` clients. `env` is the environment the
    commands run with (the caller's when None). Raises RuntimeError when docker does not answer."""
    kw = {"capture_output": True, "text": True, "env": None if env is None else dict(env)}
    r = runner(["docker", "ps", "--format", PS_FORMAT], timeout=30, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"docker ps failed: {(r.stderr or '').strip()[:200]}")
    containers = parse_containers(r.stdout)
    if containers:
        i = runner(["docker", "inspect", "--format", INSPECT_FORMAT, *[c["id"] for c in containers]],
                   timeout=60, **kw)
        if i.returncode != 0:
            raise RuntimeError(f"docker inspect failed: {(i.stderr or '').strip()[:200]}")
        containers = run_rm_only(containers, parse_inspect(i.stdout))
    p = runner(["ps", "-Ao", "lstart=,command="], timeout=10, **kw)
    return containers, parse_clients(p.stdout)


def stop(ids: Sequence[str], runner: Runner = subprocess.run, env: Mapping[str, str] | None = None) -> list[str]:
    """Stop these containers; `--rm` then removes them. Returns the ids docker reported stopped."""
    if not ids:
        return []
    r = runner(["docker", "stop", *ids], capture_output=True, text=True, timeout=120,
               env=None if env is None else dict(env))
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def words(found: list[dict]) -> str:
    if not found:
        return "no orphaned MCP containers"
    return "\n".join(f"{c['id']}  {c['image']}  {int(c['age_s'] // 3600)} h old" for c in found)


def main(argv: list[str] | None = None, runner: Runner = subprocess.run, now: float | None = None,
         env: Mapping[str, str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops mcp-containers",
                                description="list or stop the MCP server containers no live chat runs")
    p.add_argument("--stop", action="store_true", help="stop the orphans (docker stop)")
    p.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE_MIN, metavar="MINUTES",
                   help=f"leave a container younger than this alone (default {DEFAULT_MIN_AGE_MIN})")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(argv)
    try:
        containers, clients = read(runner, env)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    found = orphans(containers, clients, now, min_age_s=a.min_age * 60)
    stopped = stop([c["id"] for c in found], runner, env) if a.stop else []
    if a.json:
        print(json.dumps({"running": len(containers), "clients": len(clients), "orphans": found,
                          "stopped": stopped}))
    else:
        print(words(found))
        if a.stop:
            print(f"stopped {len(stopped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
