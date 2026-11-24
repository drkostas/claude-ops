"""Recreating a container from a fresh pull. Only a stand-in docker is used, never the real one."""
import copy
import json
import subprocess

from claude_ops import container_recreate as cr

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
REF = "example/app:latest"
VOL = "f" * 64          # an anonymous volume: docker names it by a hash


def _container(**over):
    c = {"Name": "/web", "Image": OLD,
         "State": {"Running": True, "Status": "running", "ExitCode": 0},
         "Config": {"Image": REF, "Env": ["PATH=/usr/bin", "HOME=/root", "TZ=Europe/Paris", "PUID=1000"],
                    "Labels": {"org.example.vendor": "example", "owner": "ops"}, "Cmd": ["serve"],
                    "Entrypoint": ["/init"], "User": "", "WorkingDir": "/app"},
         "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                        "PortBindings": {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8082"}]},
                        "ShmSize": 1073741824, "NetworkMode": "bridge", "Privileged": False,
                        "CapAdd": None, "IpcMode": "private", "Runtime": "runc"},
         "Mounts": [{"Type": "volume", "Name": VOL, "Destination": "/config", "RW": True},
                    {"Type": "bind", "Source": "/srv/shared", "Destination": "/shared", "RW": False}]}
    for k, v in over.items():
        c[k] = v
    return c


def _image(**cfg):
    base = {"Env": ["PATH=/usr/bin", "HOME=/root"], "Labels": {"org.example.vendor": "example"},
            "Cmd": ["serve"], "Entrypoint": ["/init"], "User": "", "WorkingDir": "/app"}
    base.update(cfg)
    return {"Config": base}


class Docker:
    """A stand-in docker holding containers and images by name, answering the calls recreate makes."""

    def __init__(self, container, *, pulled=NEW, new_runs=True, health=None, pull_code=0, env_seen=None):
        self.containers = {"web": container}
        self.images = {OLD: _image(), REF: {"Id": pulled, **_image()}}
        self.calls, self.new_runs, self.health, self.pull_code = [], new_runs, health, pull_code
        self.envs = []

    def __call__(self, argv, **k):
        self.envs.append(k.get("env"))
        a = argv[1:]
        self.calls.append(" ".join(a))

        def ok(out=""):
            return subprocess.CompletedProcess(argv, 0, out, "")

        def no(err="no such object"):
            return subprocess.CompletedProcess(argv, 1, "", err)
        if a[0] == "inspect":
            c = self.containers.get(a[1])
            return ok(json.dumps([c])) if c else no()
        if a[:2] == ["image", "inspect"]:
            i = self.images.get(a[2])
            return ok(json.dumps([i])) if i else no()
        if a[0] == "pull":
            return ok() if self.pull_code == 0 else no("registry down")
        if a[0] == "stop":
            self.containers[a[1]]["State"] = {"Running": False, "Status": "exited", "ExitCode": 0}
            return ok()
        if a[0] == "start":
            if a[1] not in self.containers:
                return no()
            self.containers[a[1]]["State"] = {"Running": True, "Status": "running", "ExitCode": 0}
            return ok()
        if a[0] == "rename":
            if a[1] not in self.containers:
                return no()
            self.containers[a[2]] = self.containers.pop(a[1])
            return ok()
        if a[0] == "rm":
            name = a[-1]
            return ok() if self.containers.pop(name, None) is not None else no()
        if a[0] == "run":
            name = a[a.index("--name") + 1]
            state = {"Running": self.new_runs, "Status": "running" if self.new_runs else "exited",
                     "ExitCode": 0 if self.new_runs else 1}
            if self.health:
                state["Health"] = {"Status": self.health}
            new = copy.deepcopy(self.containers.get(name + "-old") or {})
            new.update({"Name": "/" + name, "Image": self.images[REF]["Id"], "State": state})
            self.containers[name] = new
            return ok("c0ffee\n")
        return no(f"unknown call {a}")


def _recreate(d, **k):
    now = [0.0]                       # a clock that moves only when recreate sleeps

    def sleep(s):
        now[0] += s
    return cr.recreate("web", run=d, sleep=sleep, clock=lambda: now[0], wait_s=10,
                       connect=k.pop("connect", lambda h, p: True), env={"PATH": "/usr/bin"}, **k)


def test_an_unchanged_image_touches_nothing():
    d = Docker(_container(), pulled=OLD)
    out = _recreate(d)
    assert out["outcome"] == "unchanged"
    assert not any(c.split()[0] in ("stop", "rename", "run", "rm") for c in d.calls)


def test_a_new_image_is_run_the_same_way_and_the_old_container_removed():
    d = Docker(_container())
    out = _recreate(d)
    assert out["outcome"] == "recreated", out
    run = next(c for c in d.calls if c.startswith("run "))
    assert run.startswith("run -d --name web --restart unless-stopped -p 127.0.0.1:8082:3000/tcp --shm-size 1073741824")
    assert "-e TZ=Europe/Paris -e PUID=1000" in run and "-e PATH=" not in run and "-e HOME=" not in run
    assert f"-v {VOL}:/config" in run and "-v /srv/shared:/shared:ro" in run
    assert "--label owner=ops" in run and "org.example.vendor" not in run
    assert run.endswith(REF) and "--network" not in run
    assert set(d.containers) == {"web"} and d.containers["web"]["Image"] == NEW
    assert d.calls.index("stop web") < d.calls.index("rename web web-old") < d.calls.index(run) < d.calls.index("rm web-old")


def test_every_docker_call_runs_with_the_environment_given():
    d = Docker(_container())
    _recreate(d)
    assert d.envs and all(e == {"PATH": "/usr/bin"} for e in d.envs)


def test_a_new_container_that_does_not_answer_is_removed_and_the_old_one_comes_back():
    d = Docker(_container())
    out = _recreate(d, connect=lambda h, p: False)
    assert out["outcome"] == "restored", out
    assert "nothing answers on 127.0.0.1:8082" in out["said"]
    assert set(d.containers) == {"web"} and d.containers["web"]["Image"] == OLD
    assert d.containers["web"]["State"]["Running"] is True


def test_a_new_container_that_exits_is_removed_and_the_old_one_comes_back():
    d = Docker(_container(), new_runs=False)
    out = _recreate(d)
    assert out["outcome"] == "restored" and "it is not running" in out["said"]
    assert d.containers["web"]["Image"] == OLD


def test_a_health_check_decides_when_the_image_has_one():
    hc = {"Test": ["CMD", "true"]}
    for health, connect, want in (("healthy", lambda h, p: False, "recreated"), ("unhealthy", lambda h, p: True, "restored")):
        c = _container()
        c["Config"]["Healthcheck"] = hc
        d = Docker(c, health=health)
        d.images[OLD]["Config"]["Healthcheck"] = hc
        out = _recreate(d, connect=connect)
        assert out["outcome"] == want, out
    assert "unhealthy" in out["said"]


def test_a_health_check_the_user_set_is_refused():
    c = _container()
    c["Config"]["Healthcheck"] = {"Test": ["CMD", "true"]}
    out = _recreate(Docker(c))
    assert out["outcome"] == "refused" and "its own health check" in out["said"]


def _refused_for(**host):
    c = _container()
    c["HostConfig"].update(host)
    d = Docker(c)
    out = _recreate(d)
    assert out["outcome"] == "refused", out
    assert not any(x.split()[0] in ("pull", "stop") for x in d.calls)
    return out["said"]


def test_settings_a_new_container_would_lose_are_refused_by_name():
    assert "Memory" in _refused_for(Memory=4294967296, MemorySwap=8589934592)
    assert "Ulimits" in _refused_for(Ulimits=[{"Name": "core", "Soft": 0, "Hard": 0}])
    assert "ExtraHosts" in _refused_for(ExtraHosts=["db.example.org:192.0.2.10"])


def test_a_field_docker_adds_later_is_refused_not_dropped():
    assert "SomeFutureLimit" in _refused_for(SomeFutureLimit=7)


def test_docker_s_own_defaults_are_not_refused():
    c = _container()
    c["HostConfig"].update({"CgroupnsMode": "private", "MaskedPaths": ["/proc/acpi"], "ReadonlyPaths": ["/proc/bus"],
                            "ConsoleSize": [0, 0], "OomScoreAdj": 0, "Dns": []})
    assert cr.plan(c, _image())["refused"] == []


def test_a_stop_timeout_the_user_set_is_refused():
    c = _container()
    c["Config"]["StopTimeout"] = 30
    assert "StopTimeout" in " ".join(cr.plan(c, _image())["refused"])


def test_a_second_network_and_a_fixed_address_are_refused():
    c = _container()
    c["NetworkSettings"] = {"Networks": {"bridge": {}, "back": {"IPAMConfig": {"IPv4Address": "192.0.2.5"}}}}
    said = " ".join(cr.plan(c, _image())["refused"])
    assert "2 networks" in said and "fixed address on back" in said


def test_log_settings_tmpfs_and_rm_are_copied():
    c = _container()
    c["HostConfig"].update({"LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
                            "Tmpfs": {"/scratch": "rw,size=2g"}, "AutoRemove": True})
    c["Config"].update({"OpenStdin": True, "Tty": True})
    p = cr.plan(c, _image())
    run = " ".join(p["args"])
    assert p["refused"] == []
    assert "--rm -i -t --log-driver json-file --log-opt max-file=3 --log-opt max-size=10m --tmpfs /scratch:rw,size=2g" in run


def test_a_compose_container_is_refused_untouched():
    c = _container()
    c["Config"]["Labels"]["com.docker.compose.project"] = "stack"
    d = Docker(c)
    out = _recreate(d)
    assert out["outcome"] == "refused" and "compose" in out["said"]
    assert not any(x.split()[0] in ("pull", "stop", "run") for x in d.calls)


def test_a_setting_that_is_not_copied_is_named_and_nothing_is_touched():
    c = _container()
    c["HostConfig"]["Privileged"] = True
    c["HostConfig"]["CapAdd"] = ["NET_ADMIN"]
    d = Docker(c)
    out = _recreate(d)
    assert out["outcome"] == "refused" and "Privileged" in out["said"] and "CapAdd" in out["said"]
    assert not any(x.split()[0] in ("pull", "stop") for x in d.calls)


def test_a_leftover_old_container_is_refused_before_anything_stops():
    d = Docker(_container())
    d.containers["web-old"] = _container()
    out = _recreate(d)
    assert out["outcome"] == "refused" and "web-old" in out["said"]
    assert "stop web" not in d.calls


def test_a_failed_pull_touches_nothing():
    d = Docker(_container(), pull_code=1)
    out = _recreate(d)
    assert out["outcome"] == "failed" and "registry down" in out["said"]
    assert "stop web" not in d.calls


def test_a_command_the_user_gave_follows_the_reference():
    c = _container()
    c["Config"]["Cmd"] = ["serve", "--port", "3000"]
    p = cr.plan(c, _image())
    assert p["args"][-4:] == [REF, "serve", "--port", "3000"]
