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
