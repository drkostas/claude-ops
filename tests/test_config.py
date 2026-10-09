"""Versions, processes and fingerprints of what Claude Code loads."""
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
