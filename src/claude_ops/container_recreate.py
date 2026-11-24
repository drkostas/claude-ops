"""Create a container again from a fresh pull of its image, keeping its settings and its data.

A container keeps the image it was created from. `docker restart` starts the same image again, so a
security release published under the container's tag reaches it only when the container is created
again from a fresh pull. Done by hand, that step is easy to get wrong: a container made with a plain
`docker run` and no compose file has nothing that records how it was made, and its data often sits on
an anonymous volume that a new `docker run` would not mount, so the new container starts empty.

This reads how the running container was made and makes the new one the same way:

1. Read the container (`docker inspect`) and the image it runs. Refuse a container a compose project
   manages (recreate it with compose), and one that sets something this does not copy, naming each.
   The check is a list of what is accounted for. Every setting is copied, docker's own default, the
   image's own value, or refused by name, so a setting docker adds later is refused, never dropped.
2. Pull its image reference. When the reference still names the image the container runs, stop:
   nothing is new.
3. Stop the old container and rename it to `<name>-old`, so its name is free and it can come back.
4. Run the new one with the same name, published ports, environment the user set (the image's own
   defaults are left to the new image), shared memory size, restart policy, labels, network, command,
   log settings, tmpfs mounts, `--rm`, `-i` and `-t`, and every mount: a named or anonymous volume by
   its name, a bind mount by its path.
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
#: HostConfig fields plan() copies into the new container's `docker run`
HOST_COPIED = frozenset({"RestartPolicy", "PortBindings", "ShmSize", "NetworkMode", "Binds", "Mounts", "Tmpfs",
                         "LogConfig", "AutoRemove"})
#: HostConfig fields docker fills in itself, with the values it uses when the user set nothing
HOST_DEFAULTS = {"IpcMode": ("private", "shareable"), "CgroupnsMode": ("private", "host"), "Runtime": ("runc",),
                 "MaskedPaths": None, "ReadonlyPaths": None}     # None: any value, docker's own path lists
#: Config fields plan() copies, compares with the image, or that only say how a client attached
CONFIG_HANDLED = frozenset({"Image", "Env", "Labels", "Cmd", "Entrypoint", "User", "WorkingDir", "Hostname",
                            "ExposedPorts", "Volumes", "StopSignal", "Healthcheck", "Tty", "OpenStdin",
                            "AttachStdin", "AttachStdout", "AttachStderr", "StdinOnce", "ArgsEscaped", "OnBuild",
                            "Shell"})


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


def _empty(value) -> bool:
    return value in (None, False, 0, "", [], {}) or value == [0, 0]


def unaccounted(container: dict, image: dict) -> list[str]:
    """Each setting of `container` the new one would not get, in words. Every HostConfig and Config field is
    either copied by plan(), docker's own default, the image's own value, or named here, so a field docker adds
    later is refused rather than dropped without a word."""
    cfg, host = container.get("Config") or {}, container.get("HostConfig") or {}
    icfg = image.get("Config") or {}
    out = []
    for key, value in sorted(host.items()):
        if key in HOST_COPIED or _empty(value):
            continue
        if key in HOST_DEFAULTS and (HOST_DEFAULTS[key] is None or value in HOST_DEFAULTS[key]):
            continue
        out.append(f"it sets {key} ({json.dumps(value)[:80]}), which this does not copy")
    for key, value in sorted(cfg.items()):
        if key not in CONFIG_HANDLED and not _empty(value):
            out.append(f"it sets {key} ({json.dumps(value)[:80]}), which this does not copy")
    for key, words in (("Entrypoint", "its own entrypoint"), ("User", "its own user"),
                       ("WorkingDir", "its own working folder"), ("StopSignal", "its own stop signal"),
                       ("Healthcheck", "its own health check")):
        if (cfg.get(key) or None) != (icfg.get(key) or None):
            out.append(f"it sets {words} ({json.dumps(cfg.get(key))[:80]}), which this does not copy")
    host_name, cid = cfg.get("Hostname") or "", container.get("Id") or ""
    if host_name and not cid.startswith(host_name):
        out.append(f"it sets its own host name ({host_name}), which this does not copy")
    published = set((host.get("PortBindings") or {}))
    extra = sorted(set(cfg.get("ExposedPorts") or {}) - set(icfg.get("ExposedPorts") or {}) - published)
    if extra:
        out.append(f"it exposes ports its image does not ({', '.join(extra)}), which this does not copy")
    dests = {m.get("Destination") for m in container.get("Mounts") or []} | set(host.get("Tmpfs") or {})
    extra = sorted(set(cfg.get("Volumes") or {}) - set(icfg.get("Volumes") or {}) - dests)
    if extra:
        out.append(f"it declares volumes its image does not ({', '.join(extra)}), which this does not copy")
    mode = host.get("NetworkMode") or "default"
    if mode.startswith("container:"):
        out.append(f"it shares another container's network ({mode}), which this does not copy")
    nets = ((container.get("NetworkSettings") or {}).get("Networks") or {})
    if len(nets) > 1:
        out.append(f"it is on {len(nets)} networks ({', '.join(sorted(nets))}), and this copies one")
    own = {(container.get("Name") or "").lstrip("/"), cid[:12]}
    for net, n in sorted(nets.items()):
        if not _empty((n or {}).get("IPAMConfig")):
            out.append(f"it has a fixed address on {net}, which this does not copy")
        aliases = set((n or {}).get("Aliases") or []) - own
        if aliases:
            out.append(f"it has its own names on {net} ({', '.join(sorted(aliases))}), which this does not copy")
    return out


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
    refused += unaccounted(container, image)
    mode = host.get("NetworkMode") or "default"

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
    if host.get("AutoRemove"):
        args.append("--rm")
    if cfg.get("OpenStdin"):
        args.append("-i")
    if cfg.get("Tty"):
        args.append("-t")
    log = host.get("LogConfig") or {}
    if log.get("Type"):
        args += ["--log-driver", log["Type"]]
        for key, value in sorted((log.get("Config") or {}).items()):
            args += ["--log-opt", f"{key}={value}"]
    for dest, opts in sorted((host.get("Tmpfs") or {}).items()):
        args += ["--tmpfs", f"{dest}:{opts}" if opts else dest]
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
            if dest not in (host.get("Tmpfs") or {}):
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
