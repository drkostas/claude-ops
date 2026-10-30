"""Versions, processes and fingerprints of what Claude Code loads."""
import json
import subprocess

from claude_ops import config as cc


def test_versions():
    assert cc.version_of_binary("/Users/x/.local/share/claude/versions/2.1.294") == "2.1.294"
    assert cc.version_of_binary("/usr/bin/node") is None
    assert cc.version_of_binary(None) is None


def test_ps_is_parsed_with_its_five_word_start_time():
    ps = cc.parse_ps("  100     1 Thu Oct  9 12:00:00 2026 claude\n"
                     "  101   100 Thu Oct  9 12:00:01 2026 /bin/zsh\nbroken line\n")
    assert ps[100]["comm"] == "claude" and ps[101]["ppid"] == 100 and set(ps) == {100, 101}
    assert cc.is_claude(ps[100]) and not cc.is_claude(ps[101]) and not cc.is_claude(None)


def test_fingerprints(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text('{"mcpServers": {"x": 1, "y": 2}, "numStartups": 5}')
    b.write_text('{"numStartups": 9, "mcpServers": {"y": 2, "x": 1}}')
    assert cc.digest_json(a, "mcpServers") == cc.digest_json(b, "mcpServers")
    assert cc.digest_json(a) != cc.digest_json(b)
    assert cc.digest_json(tmp_path / "missing.json") is None
    assert cc.digest_files([tmp_path / "missing"]) is None
    assert cc.digest_files([a]) != cc.digest_files([a, b])


def test_instruction_files_follow_at_imports(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("intro\n@RTK.md\n@tools.md\nnot @an import\n")
    assert [p.name for p in cc.instruction_files(md)] == ["CLAUDE.md", "RTK.md", "tools.md"]
    assert cc.instruction_files(tmp_path / "none.md") == [tmp_path / "none.md"]


def test_project_claude_mds_walk_up_and_skip_the_global_one(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("top")
    (tmp_path / "a" / "b" / "CLAUDE.md").write_text("leaf")
    got = cc.project_claude_mds(tmp_path / "a" / "b", global_md=tmp_path / "CLAUDE.md")
    assert got == [tmp_path / "a" / "b" / "CLAUDE.md"]


def test_processes_and_running_version_carry_the_env(monkeypatch):
    seen = []

    def fake(argv, **k):
        seen.append(k.get("env"))
        out = ("  100     1 Thu Oct  9 12:00:00 2026 claude\n" if argv[0] == "/bin/ps"
               else "p100\nn/Users/x/.local/share/claude/versions/2.1.294\n")
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake)
    env = {"PATH": "/usr/bin:/bin"}
    assert 100 in cc.processes(env=env)
    assert cc.running_version(100, env=env) == "2.1.294"
    assert seen == [env, env]


def test_descendants_are_every_process_under_one():
    procs = {1: {"ppid": 0, "comm": "launchd"}, 10: {"ppid": 1, "comm": "claude"},
             11: {"ppid": 10, "comm": "node"}, 12: {"ppid": 11, "comm": "Google Chrome for Testing"},
             13: {"ppid": 12, "comm": "Google Chrome Helper"}, 20: {"ppid": 1, "comm": "Brave Browser"}}
    assert cc.descendants(10, procs) == {11, 12, 13}
    assert cc.descendants(13, procs) == set()
    looped = {5: {"ppid": 6, "comm": "a"}, 6: {"ppid": 5, "comm": "b"}}
    assert cc.descendants(5, looped) == {5, 6}          # a parent loop ends instead of hanging


def test_login_digest_follows_the_account_and_nothing_else(tmp_path):
    f = tmp_path / "claude.json"
    def write(**account):
        f.write_text(json.dumps({"mcpServers": {}, "oauthAccount": account}))
        return cc.login_digest(f)
    one = write(accountUuid="a-1", organizationUuid="o-1", profileFetchedAt="2026-01-01")
    same = write(accountUuid="a-1", organizationUuid="o-1", profileFetchedAt="2026-02-02", seatTier="x")
    other = write(accountUuid="a-2", organizationUuid="o-2")
    assert one and one == same and other != one
    f.write_text(json.dumps({"mcpServers": {}}))
    assert cc.login_digest(f) is None
    assert cc.login_digest(tmp_path / "missing.json") is None


def test_mcp_server_shapes_hold_fingerprints_and_a_package_never_the_arguments(tmp_path):
    p = tmp_path / "claude.json"
    token = "tok-" + "0123456789"
    p.write_text(json.dumps({"mcpServers": {
        "app": {"command": "python3", "args": ["-P", "-m", "app.server"], "env": {"API_TOKEN": token}},
        "web": {"command": "npx", "args": ["-y", "some-server", "--access-token", token]},
    }}))
    shapes = cc.mcp_server_shapes(p)
    assert set(shapes) == {"app", "web"}
    assert shapes["app"]["module"] == "app" and shapes["web"]["module"] is None
    assert shapes["app"]["digest"] != shapes["app"]["without_p"]
    assert shapes["web"]["digest"] == shapes["web"]["without_p"]
    assert token not in json.dumps(shapes)
    assert cc.mcp_server_shapes(tmp_path / "missing.json") is None


def test_an_mcp_change_counts_only_for_a_session_it_changes_something_for(tmp_path):
    p = tmp_path / "claude.json"
    spec = {"command": "python3", "args": ["-m", "app.server"], "env": {"PYTHONPATH": "/srv/app"}}
    p.write_text(json.dumps({"mcpServers": {"app": spec}}))
    then = cc.mcp_server_shapes(p)["app"]
    p.write_text(json.dumps({"mcpServers": {"app": {**spec, "args": ["-P", "-m", "app.server"]}}}))
    now = cc.mcp_server_shapes(p)["app"]
    shadowing, plain = tmp_path / "checkout", tmp_path / "elsewhere"
    (shadowing / "app").mkdir(parents=True)
    plain.mkdir()
    # -P only matters where the working folder holds the package the server imports
    assert "-P" in cc.mcp_change_affects(then, now, shadowing)
    assert cc.mcp_change_affects(then, now, plain) is None
    assert cc.mcp_change_affects(then, now, None) is None
    # a server the session never loaded changes nothing for it, and one removed since does
    assert cc.mcp_change_affects(None, now, shadowing) is None
    assert cc.mcp_change_affects(then, None, plain) == "a server it runs was removed"
    # any other change of a server it runs counts, wherever it works
    p.write_text(json.dumps({"mcpServers": {"app": {**spec, "command": "python3.14"}}}))
    other = cc.mcp_server_shapes(p)["app"]
    assert cc.mcp_change_affects(then, other, plain) == "a server it runs is defined differently"
    assert cc.mcp_change_affects(then, then, shadowing) is None
