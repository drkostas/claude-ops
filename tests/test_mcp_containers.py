"""mcp_containers: the MCP server containers a chat left behind are found by the client that is gone."""
from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta, timezone

from claude_ops import mcp_containers as mc

IMAGE = "ghcr.io/example/mcp-server"
TZ = timezone(timedelta(hours=2))


def docker_line(cid: str, when: datetime, image: str = IMAGE) -> str:
    return f"{cid}\t{when.strftime('%Y-%m-%d %H:%M:%S %z')} CEST\t{image}\t\"/server/mcp…\""


def ps_line(when: float, image: str = IMAGE) -> str:
    local = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(when))
    return f"{local}     docker run -i --rm -e SOME_TOKEN {image}"


def test_docker_ps_lines_parse_to_epochs():
    when = datetime(2026, 10, 25, 5, 6, 27, tzinfo=TZ)
    got = mc.parse_containers(docker_line("6bbcd2ad966e", when) + "\nbroken line\n")
    assert [c["id"] for c in got] == ["6bbcd2ad966e"]
    assert got[0]["created"] == when.timestamp() and got[0]["image"] == IMAGE


def test_ps_lines_give_only_docker_run_clients():
    started = time.mktime(time.strptime("Sun Oct 25 05:53:22 2026", "%a %b %d %H:%M:%S %Y"))
    text = (ps_line(started) + "\n"
            "Sun Oct 25 03:00:00 2026     /bin/zsh -c grep docker run\n"
            "Sun Oct 25 03:00:00 2026     sleep 120\n")
    got = mc.parse_clients(text)
    assert len(got) == 1 and got[0]["starts"] == [started] and got[0]["image"] == IMAGE


def test_a_start_in_the_repeated_hour_means_two_moments(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        two = mc._ps_times("Sun Oct 25 02:30:00 2026")      # the clocks went back at 03:00 that night
        one = mc._ps_times("Sun Oct 25 05:30:00 2026")
        assert len(two) == 2 and two[1] - two[0] == 3600 and len(one) == 1
        assert mc._ps_times("not a time") == []
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_a_container_with_a_live_client_is_kept_and_one_without_is_an_orphan():
    now = time.time()
    kept = {"id": "aaa", "created": now - 5 * 3600, "image": IMAGE, "command": ""}
    orphan = {"id": "bbb", "created": now - 6 * 3600, "image": IMAGE, "command": ""}
    clients = [{"starts": [kept["created"] + 3], "image": IMAGE, "command": "docker run"}]
    found = mc.orphans([kept, orphan], clients, now)
    assert [c["id"] for c in found] == ["bbb"] and found[0]["age_s"] == 6 * 3600


def test_a_client_started_in_the_repeated_hour_claims_its_container_at_either_moment():
    now = time.time()
    c = {"id": "ccc", "created": now - 5 * 3600, "image": IMAGE, "command": ""}
    both = [{"starts": [c["created"] + 2 - 3600, c["created"] + 2], "image": IMAGE, "command": ""}]
    assert mc.orphans([c], both, now) == []
    one = [{"starts": [c["created"] + 1800], "image": IMAGE, "command": ""}]
    assert [x["id"] for x in mc.orphans([c], one, now)] == ["ccc"]


def test_each_client_claims_one_container_and_images_must_match():
    now = time.time()
    a = {"id": "a", "created": now - 5 * 3600, "image": IMAGE, "command": ""}
    b = {"id": "b", "created": now - 5 * 3600 + 4, "image": IMAGE, "command": ""}
    other = {"id": "o", "created": now - 5 * 3600, "image": "ghcr.io/example/other", "command": ""}
    clients = [{"starts": [a["created"]], "image": IMAGE, "command": ""}]
    assert sorted(c["id"] for c in mc.orphans([a, b, other], clients, now)) == ["b", "o"]


def test_a_young_container_is_never_an_orphan():
    now = time.time()
    young = {"id": "y", "created": now - 60, "image": IMAGE, "command": ""}
    assert mc.orphans([young], [], now) == []
    assert [c["id"] for c in mc.orphans([young], [], now, min_age_s=30)] == ["y"]


class FakeRun:
    def __init__(self, docker_ps: str, ps: str, fail_docker: bool = False):
        self.docker_ps, self.ps, self.fail_docker, self.calls = docker_ps, ps, fail_docker, []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 1 if self.fail_docker else 0, "" if self.fail_docker else self.docker_ps,
                                               "Cannot connect to the Docker daemon" if self.fail_docker else "")
        if args[:2] == ["docker", "stop"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(args[2:]) + "\n", "")
        return subprocess.CompletedProcess(args, 0, self.ps, "")


def test_main_lists_and_stops_only_with_the_flag(capsys):
    now = time.time()
    orphan_at = datetime.fromtimestamp(now - 5 * 3600, TZ)
    kept_at = datetime.fromtimestamp(now - 4 * 3600, TZ)
    fake = FakeRun(docker_line("orphan1", orphan_at) + "\n" + docker_line("kept1", kept_at) + "\n",
                   ps_line(kept_at.timestamp() + 2) + "\n")
    assert mc.main([], runner=fake, now=now) == 0
    out = capsys.readouterr().out
    assert "orphan1" in out and "kept1" not in out and "5 h old" in out
    assert not any(c[:2] == ["docker", "stop"] for c in fake.calls)
    assert mc.main(["--stop", "--json"], runner=fake, now=now) == 0
    out = capsys.readouterr().out
    assert '"stopped": ["orphan1"]' in out and ["docker", "stop", "orphan1"] in fake.calls


def test_main_says_when_docker_does_not_answer(capsys):
    assert mc.main([], runner=FakeRun("", "", fail_docker=True)) == 2
    assert "docker ps failed" in capsys.readouterr().err
