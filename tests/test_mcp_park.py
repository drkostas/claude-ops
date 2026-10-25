import json
import os
import stat

import pytest

from claude_ops import config, mcp_park


def _files(tmp_path, servers, parked=None, extra=None):
    cfg, prk = tmp_path / "claude.json", tmp_path / "parked.json"
    cfg.write_text(json.dumps({"theme": "dark", "mcpServers": servers, **(extra or {})}, indent=2) + "\n")
    if parked is not None:
        prk.write_text(json.dumps(parked))
    return cfg, prk


def _def(name):
    return {"type": "stdio", "command": "run", "args": [name], "env": {"KEY": f"k-{name}"}}


def test_park_moves_servers_out_of_the_configuration_and_keeps_everything_else(tmp_path):
    cfg, prk = _files(tmp_path, {n: _def(n) for n in ("a", "b", "c")})
    moved = mcp_park.park(["b", "c", "nope"], config_path=cfg, parked_path=prk)
    assert moved == ["b", "c"]
    data = json.loads(cfg.read_text())
    assert list(data["mcpServers"]) == ["a"] and data["theme"] == "dark"
    assert mcp_park.parked(prk) == {"b": _def("b"), "c": _def("c")}
    assert stat.S_IMODE(os.stat(prk).st_mode) == 0o600   # definitions hold keys


def test_park_of_nothing_writes_nothing(tmp_path):
    cfg, prk = _files(tmp_path, {"a": _def("a")})
    before = cfg.stat().st_mtime_ns
    assert mcp_park.park(["nope"], config_path=cfg, parked_path=prk) == []
    assert cfg.stat().st_mtime_ns == before and not prk.exists()


def test_unpark_and_keep_only_and_definitions(tmp_path):
    cfg, prk = _files(tmp_path, {"a": _def("a"), "b": _def("b")}, parked={"c": _def("c"), "a": {"old": 1}})
    # a configured server wins over a parked one of the same name
    assert mcp_park.definitions(cfg, prk) == {"c": _def("c"), "a": _def("a"), "b": _def("b")}
    assert mcp_park.unpark(["c"], config_path=cfg, parked_path=prk) == ["c"]
    assert sorted(mcp_park.configured(cfg)) == ["a", "b", "c"] and "c" not in mcp_park.parked(prk)
    assert mcp_park.keep_only(["a"], config_path=cfg, parked_path=prk) == ["b", "c"]
    assert list(mcp_park.configured(cfg)) == ["a"]
    # unpark also dropped the parked "a", since "a" is configured; keep_only then parked b and c
    assert sorted(mcp_park.parked(prk)) == ["b", "c"]


def test_update_json_starts_again_when_another_program_wrote_and_keeps_a_backup(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"n": 1}) + "\n")
    calls = []

    def change(data):
        calls.append(data["n"])
        if len(calls) == 1:   # another program writes while this one builds its change
            path.write_text(json.dumps({"n": 2, "theirs": True}) + "\n")
        data["mine"] = True
        return data

    out = config.update_json(path, change, backup=tmp_path / "c.bak")
    assert calls == [1, 2] and out["changed"]
    assert json.loads(path.read_text()) == {"n": 2, "theirs": True, "mine": True}   # their write kept
    # the backup holds exactly what this write replaced: their version, not the first one read
    assert json.loads((tmp_path / "c.bak").read_text()) == {"n": 2, "theirs": True}


def test_update_json_gives_up_without_writing_when_the_file_never_holds_still(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"n": 0}))

    def change(data):
        path.write_text(json.dumps({"n": data["n"] + 1}))
        return {**data, "mine": True}

    with pytest.raises(config.ConfigChanged):
        config.update_json(path, change, tries=3)
    assert "mine" not in json.loads(path.read_text())


def test_update_json_refuses_a_file_that_is_not_an_object(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("[1, 2]")
    with pytest.raises(ValueError):
        config.update_json(path, lambda d: d)


def test_cli_lists_parks_and_keeps(tmp_path, capsys):
    cfg, prk = _files(tmp_path, {n: _def(n) for n in ("a", "b", "c")})
    base = ["--config", str(cfg), "--parked", str(prk), "--no-backup"]
    assert mcp_park.main(base + ["keep", "a"]) == 0
    assert json.loads(capsys.readouterr().out)["moved"] == ["b", "c"]
    assert mcp_park.main(base + ["list"]) == 0
    assert json.loads(capsys.readouterr().out) == {"configured": ["a"], "parked": ["b", "c"]}
    assert mcp_park.main(base + ["unpark", "b"]) == 0
    assert sorted(mcp_park.configured(cfg)) == ["a", "b"]
