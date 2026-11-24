"""Create a container again from a fresh pull of its image, keeping its settings and its data.

A container keeps the image it was created from. `docker restart` starts the same image again, so a
security release published under the container's tag reaches it only when the container is created
again from a fresh pull. Done by hand, that step is easy to get wrong: a container made with a plain
`docker run` and no compose file has nothing that records how it was made, and its data often sits on
an anonymous volume that a new `docker run` would not mount, so the new container starts empty.

This reads how the running container was made and makes the new one the same way:

1. Read the container (`docker inspect`) and the image it runs. Refuse a container a compose project
   manages (recreate it with compose), and one that sets something this does not copy, naming each.
2. Pull its image reference. When the reference still names the image the container runs, stop:
   nothing is new.
3. Stop the old container and rename it to `<name>-old`, so its name is free and it can come back.
4. Run the new one with the same name, published ports, environment the user set (the image's own
   defaults are left to the new image), shared memory size, restart policy, labels, network, command
   and every mount: a named or anonymous volume by its name, a bind mount by its path.
5. Check that it answers: its health check when the image has one, else a TCP connection to its first
   published port, else that it is still running after a few seconds.
6. Remove the old container (its volumes stay, since `docker rm` without `-v` keeps them). If the new
   one did not start or did not answer, remove it and start the old one again under its own name.

    from claude_ops import container_recreate
    out = container_recreate.recreate("web")
    print(out["outcome"], out["said"])

    claude-ops container-recreate web          # the same from the command line

`outcome` is one of "unchanged", "recreated", "restored" (the new one failed and the old one runs
again), "refused" (nothing was touched), "failed" (the pull or the read failed, nothing was touched)
and "broken" (the new one failed and the old one could not be put back, which `said` explains).
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: docker's own default shared memory size, which needs no flag
DEFAULT_SHM = 64 * 1024 * 1024
#: the label a compose project puts on each container it manages
COMPOSE_LABEL = "com.docker.compose.project"
#: how long the new container gets to answer, in seconds
WAIT_S = 90.0
#: how long a container with no health check and no published port must keep running
SETTLE_S = 5.0
#: HostConfig fields this does not copy, with the value docker reports when the user did not set them
NOT_COPIED = {"Privileged": False, "CapAdd": None, "CapDrop": None, "Devices": None, "ExtraHosts": None,
              "Dns": None, "SecurityOpt": None, "PidMode": "", "IpcMode": "private", "Runtime": "runc",
              "VolumesFrom": None, "Links": None, "Init": None}


def _docker(args: list[str], run: Runner, env: Mapping[str, str] | None, timeout: float = 120
            ) -> tuple[int, str, str]:
    try:
        r = run(["docker", *args], capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout or "", (r.stderr or "").strip()


def _inspect(what: list[str], run: Runner, env) -> dict | None:
    code, out, _err = _docker([*what], run, env)
    if code != 0:
        return None
    try:
        got = json.loads(out)
    except ValueError:
        return None
    return got[0] if isinstance(got, list) and got else None


def _empty(value, default) -> bool:
    return value in (None, [], {}, "") or value == default


def plan(container: dict, image: dict) -> dict:
    """How to make `container` (one `docker inspect` object) again, given the image it runs (one
    `docker image inspect` object): {name, reference, image_id, args (for `docker run`), check, refused}.
    `refused` names each thing this cannot copy, and is empty when the container can be made again."""
    cfg, host = container.get("Config") or {}, container.get("HostConfig") or {}
    icfg = image.get("Config") or {}
    name = (container.get("Name") or "").lstrip("/")
    refused = []
    labels = cfg.get("Labels") or {}
    if COMPOSE_LABEL in labels:
        refused.append(f"a compose project manages it ({labels[COMPOSE_LABEL]}), so recreate it with compose")
    for key, default in NOT_COPIED.items():
        value = host.get(key)
        if key == "IpcMode" and value in ("private", "shareable", ""):
            continue
        if key == "Runtime" and value in ("runc", "", None):
            continue
        if not _empty(value, default):
            refused.append(f"it sets {key} ({value!r}), which this does not copy")
    if (cfg.get("Entrypoint") or None) != (icfg.get("Entrypoint") or None):
        refused.append(f"it sets its own entrypoint ({cfg.get('Entrypoint')!r}), which this does not copy")
    if (cfg.get("User") or "") != (icfg.get("User") or ""):
        refused.append(f"it runs as its own user ({cfg.get('User')!r}), which this does not copy")
    if (cfg.get("WorkingDir") or "") != (icfg.get("WorkingDir") or ""):
        refused.append(f"it sets its own working folder ({cfg.get('WorkingDir')!r}), which this does not copy")
    mode = host.get("NetworkMode") or "default"
    if mode.startswith("container:"):
        refused.append(f"it shares another container's network ({mode}), which this does not copy")

    args = ["run", "-d", "--name", name]
    policy = host.get("RestartPolicy") or {}
    if policy.get("Name") not in (None, "", "no"):
        count = policy.get("MaximumRetryCount") or 0
        args += ["--restart", f"{policy['Name']}:{count}" if policy["Name"] == "on-failure" and count else policy["Name"]]
    ports = []
    for cport, binds in sorted((host.get("PortBindings") or {}).items()):
        for b in binds or [{}]:
            ip, hport = b.get("HostIp") or "", b.get("HostPort") or ""
            spec = f"{ip}:{hport}:{cport}" if ip else (f"{hport}:{cport}" if hport else cport)
            args += ["-p", spec]
            if hport:
                ports.append((ip if ip not in ("", "0.0.0.0", "::") else "127.0.0.1", int(hport)))
    shm = host.get("ShmSize") or DEFAULT_SHM
    if shm != DEFAULT_SHM:
        args += ["--shm-size", str(shm)]
    image_env = set(icfg.get("Env") or [])
    for item in cfg.get("Env") or []:
        if item not in image_env:
            args += ["-e", item]
    image_labels = icfg.get("Labels") or {}
    for key, value in sorted(labels.items()):
        if image_labels.get(key) != value and key != COMPOSE_LABEL:
            args += ["--label", f"{key}={value}"]
    if mode not in ("default", "bridge"):
        args += ["--network", mode]
    for m in container.get("Mounts") or []:
        dest, ro = m.get("Destination"), "" if m.get("RW", True) else ":ro"
        if m.get("Type") == "volume" and m.get("Name"):
            args += ["-v", f"{m['Name']}:{dest}{ro}"]
        elif m.get("Type") == "bind" and m.get("Source"):
            args += ["-v", f"{m['Source']}:{dest}{ro}"]
        elif m.get("Type") == "tmpfs":
            args += ["--tmpfs", dest]
        else:
            refused.append(f"it has a {m.get('Type')} mount at {dest}, which this does not copy")
    reference = cfg.get("Image") or ""
    args.append(reference)
    cmd = cfg.get("Cmd") or None
    if cmd and cmd != (icfg.get("Cmd") or None):
        args += list(cmd)
    check = "health" if cfg.get("Healthcheck") or icfg.get("Healthcheck") else ("port" if ports else "running")
    return {"name": name, "reference": reference, "image_id": container.get("Image") or "", "args": args,
            "check": check, "port": ports[0] if ports else None, "refused": refused}


def _connects(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def answers(p: dict, run: Runner, env, *, wait_s: float = WAIT_S, sleep=time.sleep, clock=time.monotonic,
            connect=_connects) -> tuple[bool, str]:
    """Whether the new container answers within `wait_s`, by the check plan() chose, and in words why."""
    end, last = clock() + wait_s, "it was not read"
    started = clock()
    while True:
        state = _inspect(["inspect", p["name"]], run, env)
        st = (state or {}).get("State") or {}
        if state is None or not st.get("Running"):
            return False, f"it is not running ({st.get('Status') or 'docker did not read it'}, exit {st.get('ExitCode')})"
        if p["check"] == "health":
            health = (st.get("Health") or {}).get("Status")
            if health == "healthy":
                return True, "its health check says healthy"
            if health == "unhealthy":
                return False, "its health check says unhealthy"
            last = f"its health check says {health or 'nothing yet'}"
        elif p["check"] == "port":
            host, port = p["port"]
            if connect(host, port):
                return True, f"it answers on {host}:{port}"
            last = f"nothing answers on {host}:{port}"
        elif clock() - started >= SETTLE_S:
            return True, f"it kept running for {SETTLE_S:.0f} s"
        if clock() >= end:
            return False, f"after {wait_s:.0f} s {last}"
        sleep(2)


def recreate(name: str, *, run: Runner = subprocess.run, env: Mapping[str, str] | None = None,
             wait_s: float = WAIT_S, sleep=time.sleep, clock=time.monotonic, connect=_connects) -> dict:
    """Create the container `name` again from a fresh pull of its image reference (the module's steps).
    `env` is the environment docker runs with (the caller's when None)."""
    steps: list[str] = []

    def out(outcome: str, said: str, **more) -> dict:
        return {"outcome": outcome, "said": said, "name": name, "steps": steps, **more}

    container = _inspect(["inspect", name], run, env)
    if container is None:
        return out("failed", f"docker could not read a container called {name}")
    image = _inspect(["image", "inspect", container.get("Image") or ""], run, env)
    if image is None:
        return out("failed", f"docker could not read the image {name} runs ({container.get('Image')})")
    p = plan(container, image)
    if p["refused"]:
        return out("refused", "nothing was touched, because " + "; and ".join(p["refused"]), refused=p["refused"])
    if not p["reference"]:
        return out("refused", "nothing was touched, because the container names no image reference")
    old = f"{name}-old"
    if _inspect(["inspect", old], run, env) is not None:
        return out("refused", f"nothing was touched, because a container called {old} is in the way")

    code, _o, err = _docker(["pull", p["reference"]], run, env, timeout=900)
    steps.append(f"pull {p['reference']}: exit {code}")
    if code != 0:
        return out("failed", f"the pull of {p['reference']} failed ({err[-200:] or code}), nothing was touched")
    pulled = _inspect(["image", "inspect", p["reference"]], run, env)
    new_id = (pulled or {}).get("Id") or ""
    if not new_id:
        return out("failed", f"docker could not read {p['reference']} after the pull, nothing was touched")
    if new_id == p["image_id"]:
        return out("unchanged", f"{p['reference']} still names the image {name} runs, so nothing was recreated",
                   image=new_id)

    for args in (["stop", name], ["rename", name, old]):
        code, _o, err = _docker(args, run, env)
        steps.append(f"{' '.join(args)}: exit {code}")
        if code != 0:
            _docker(["start", name], run, env)
            return out("failed", f"docker {args[0]} failed ({err[-200:] or code}), and {name} was started again")

    code, _o, err = _docker(p["args"], run, env)
    steps.append(f"run: exit {code}")
    ok, why = (False, f"docker run failed ({err[-200:] or code})") if code != 0 else \
        answers(p, run, env, wait_s=wait_s, sleep=sleep, clock=clock, connect=connect)
    steps.append(f"check: {why}")
    if ok:
        code, _o, err = _docker(["rm", old], run, env)
        steps.append(f"rm {old}: exit {code}")
        kept = "" if code == 0 else f", but docker did not remove {old} ({err[-200:] or code})"
        return out("recreated", f"{name} runs {new_id[:19]} from {p['reference']} and {why}{kept}",
                   image=new_id, previous=p["image_id"], args=p["args"])

    _docker(["rm", "-f", name], run, env)
    back = [_docker(["rename", old, name], run, env)[0], _docker(["start", name], run, env)[0]]
    steps.append(f"put back: rename exit {back[0]}, start exit {back[1]}")
    if back == [0, 0]:
        return out("restored", f"the new {name} did not answer ({why}), so it was removed and the old one runs again",
                   args=p["args"])
    return out("broken", f"the new {name} did not answer ({why}), and putting the old one back failed "
                         f"(rename exit {back[0]}, start exit {back[1]}); it is stopped as {old} or {name}",
               args=p["args"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops container-recreate",
                                 description="create a container again from a fresh pull of its image")
    ap.add_argument("name")
    ap.add_argument("--wait", type=float, default=WAIT_S, help="seconds the new container gets to answer")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    got = recreate(a.name, wait_s=a.wait)
    print(json.dumps(got, indent=2) if a.json else f"{got['outcome']}: {got['said']}")
    return 0 if got["outcome"] in ("unchanged", "recreated") else 1


if __name__ == "__main__":
    sys.exit(main())
