import json
import os
import subprocess
from pathlib import Path

from claude_ops import cli, sessions


def test_install_skills(tmp_path, capsys):
    assert cli.main(["install", "skills", "--dest", str(tmp_path)]) == 0
    for s in cli.SKILLS:
        text = (tmp_path / s / "SKILL.md").read_text()
        assert text.startswith("---\nname: " + s)
    assert len(cli.SKILLS) == 4 and "claude-ops" in cli.SKILLS
    cli.main(["install", "skills", "--dest", str(tmp_path)])
    assert "skip" in capsys.readouterr().out


def test_install_hooks_merges_and_keeps_existing(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}))
    for _ in range(2):  # running it twice adds nothing twice
        assert cli.main(["install", "hooks", "--settings", str(settings), "--write"]) == 0
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    stop = [h["hooks"][0]["command"] for h in data["hooks"]["Stop"]]
    assert stop == ["mine", "claude-ops hook ask-what-you-need"]
    assert len(data["hooks"]["PostToolUse"]) == 1 and len(data["hooks"]["PostCompact"]) == 1
    assert list(tmp_path.glob("settings.json.bak-*"))


def test_install_hooks_only_prints_without_write(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    cli.main(["install", "hooks", "--settings", str(settings), "--only", "Stop"])
    out = capsys.readouterr().out
    assert "ask-what-you-need" in out and "context-check" not in out and not settings.exists()


def test_rover_scripts_ship_and_refuse_without_a_home_network(tmp_path, capsys):
    cli.main(["rover", "path"])
    d = Path(capsys.readouterr().out.strip())
    assert (d / "harness.sh").is_file() and (d / "netwatchdog.plist").is_file()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "ROVER_CONF": str(tmp_path / "none")}
    r = subprocess.run(["bash", str(d / "harness.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 2 and "HOME_SSID" in r.stderr
    assert not (tmp_path / "rover-out").exists()  # refused before touching anything


def test_rover_launch_needs_a_real_payload(tmp_path):
    assert cli.main(["rover", "launch", str(tmp_path / "missing.sh")]) == 2


def test_sessions_command(chat_tree, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sessions.build_index, "__defaults__", (chat_tree, tmp_path / "c.json", False))
    monkeypatch.setattr(sessions, "live_folders", lambda **k: set())
    assert cli.main(["sessions", "login"]) == 0
    out = capsys.readouterr().out
    assert "alpha-login" in out and "claude --resume aaa" in out and "bbb" not in out


def test_chat_end_without_a_session(monkeypatch, capsys):
    from claude_ops import chat
    monkeypatch.setattr(chat, "tmux_sessions", lambda **k: {})
    assert cli.main(["chat", "end", "nothing"]) == 1
    assert '"no-session"' in capsys.readouterr().out
