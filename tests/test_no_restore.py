"""The no-restore hook: a command that writes a file of its own, where git does not track that file,
is refused, and everything else goes through."""
import io
import json
import subprocess

import pytest

from claude_ops import cli
from claude_ops.hooks import no_restore as nr


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = (tmp_path / "home").resolve()           # a temporary folder can itself sit behind a link
    (h / ".config" / "sometool").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    (h / ".config" / "sometool" / "NOTES.md").write_text("hand-written notes\n")
    return h


@pytest.fixture
def config(home, tmp_path):
    path = tmp_path / "no-restore.json"
    path.write_text(json.dumps({"rules": [
        {"command": "sometool init",
         "files": ["~/.config/sometool/NOTES.md", "~/.config/sometool/GUIDE.md"],
         "message": "Copy it aside first, then read what it writes under a throwaway HOME."}]}))
    return ["--config", str(path)]


def call(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/"}


@pytest.mark.parametrize("command", [
    "sometool init",
    "sometool init -g",
    "sometool init --global",
    "/usr/local/bin/sometool init -g",
    "env sometool init -g",
    "env -u HOME sometool init",
    "SOMEVAR=1 sometool init",
    "nohup sometool init --global",
    "cd /tmp && sometool init -g",
    "sometool init | tee log",
    "ls; sometool init -g",
    "echo one\nsometool init -g",
])
def test_refused(config, command):
    why = nr.check(call(command), config)
    assert why and "NOTES.md" in why and "git does not track" in why
    assert "Copy it aside first" in why


@pytest.mark.parametrize("command", [
    "sometool hook claude",                 # another subcommand of the same program writes nothing
    "sometool initialize",                  # a longer word is not the rule's word
    "sometool",
    "sometool gain --history",
    "othertool init -g",
    "echo sometool init",                   # an argument that only names it
    "grep 'sometool init' file",
])
def test_left_alone(config, command):
    assert nr.check(call(command), config) is None


def test_a_file_git_tracks_goes_through(home, tmp_path, monkeypatch):
    repo = home / "tracked"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    kept = repo / "NOTES.md"
    kept.write_text("in a repository\n")
    subprocess.run(["git", "add", "NOTES.md"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "add"],
                   cwd=repo, check=True)
    assert nr.restorable(str(kept)) is True
    path = tmp_path / "tracked.json"
    path.write_text(json.dumps({"rules": [{"command": "sometool init", "files": [str(kept)]}]}))
    assert nr.check(call("sometool init -g"), ["--config", str(path)]) is None
    # and the same rule refuses once the file is no longer tracked
    subprocess.run(["git", "rm", "-q", "--cached", "NOTES.md"], cwd=repo, check=True)
    assert nr.restorable(str(kept)) is False
    assert nr.check(call("sometool init -g"), ["--config", str(path)])


def test_a_folder_that_is_no_repository_is_refused(config, home, tmp_path, monkeypatch):
    """git answers a folder outside any repository with a code, not an error, so the file there is one
    no checkout can bring back. The ceiling keeps the answer the same wherever the test runs."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    notes = home / ".config" / "sometool" / "NOTES.md"
    done = subprocess.run(["git", "-C", str(notes.parent), "ls-files", "--error-unmatch", "--", notes.name],
                          capture_output=True, text=True)
    assert done.returncode == 128 and "repository" in done.stderr
    assert nr.restorable(str(notes)) is False
    assert nr.check(call("sometool init -g"), config)


def test_a_git_that_cannot_be_run_lets_the_command_through(config, home, monkeypatch):
    """The other half of the same sentence. A hook that cannot ask has nothing to say, and a hook that
    cannot run must never stop work."""
    def no_git(*args, **kwargs):
        raise OSError("git is not installed here")
    monkeypatch.setattr(nr.subprocess, "run", no_git)
    assert nr.restorable(str(home / ".config" / "sometool" / "NOTES.md")) is True
    assert nr.check(call("sometool init -g"), config) is None


def test_a_missing_file_is_not_at_risk(config, home):
    (home / ".config" / "sometool" / "NOTES.md").unlink()
    assert nr.check(call("sometool init -g"), config) is None


def test_it_names_every_file_at_risk(config, home):
    (home / ".config" / "sometool" / "GUIDE.md").write_text("also hand-written\n")
    why = nr.check(call("sometool init"), config)
    assert "and 1 more" in why


@pytest.mark.parametrize("words,want", [
    (["sometool", "init"], ["sometool", "init"]),
    (["VAR=1", "sometool", "init"], ["sometool", "init"]),
    (["env", "sometool", "init"], ["sometool", "init"]),
    (["env", "-u", "HOME", "sometool", "init"], ["sometool", "init"]),
    (["nice", "-n", "10", "sometool", "init"], ["sometool", "init"]),
    (["env", "--", "sometool", "init"], ["sometool", "init"]),
    (["sometool"], ["sometool"]),
    ([], []),
])
def test_the_program_is_read_past_its_wrappers(words, want):
    assert nr.program(words) == want


def test_only_bash_is_read(config):
    assert nr.check({"tool_name": "Write", "tool_input": {"command": "sometool init"}}, config) is None
    assert nr.check({"tool_name": "Bash", "tool_input": {}}, config) is None


@pytest.mark.parametrize("text", ["", "not json", json.dumps({"rules": "no"}), json.dumps({"rules": [{"files": []}]}),
                                  json.dumps({"rules": [{"command": "sometool init"}]})])
def test_a_config_it_cannot_read_refuses_nothing(tmp_path, home, text):
    path = tmp_path / "broken.json"
    path.write_text(text)
    assert nr.check(call("sometool init -g"), ["--config", str(path)]) is None
    assert nr.check(call("sometool init -g"), ["--config", str(tmp_path / "missing.json")]) is None


def test_the_environment_names_the_config(config, home, monkeypatch, tmp_path):
    monkeypatch.setenv(nr.CONFIG_ENV, config[1])
    assert nr.check(call("sometool init -g"), [])


def run(payload, argv, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload) if isinstance(payload, dict) else payload))
    code = nr.main(list(argv))
    return code, capsys.readouterr().err


def test_main_refuses_with_the_reason(config, home, monkeypatch, capsys):
    code, err = run(call("sometool init -g"), config, monkeypatch, capsys)
    assert code == 2 and err.startswith("Blocked: ") and "NOTES.md" in err


def test_main_lets_the_rest_through(config, home, monkeypatch, capsys):
    assert run(call("sometool hook claude"), config, monkeypatch, capsys)[0] == 0


def test_a_broken_call_never_stops_work(monkeypatch, capsys):
    assert run("not json", [], monkeypatch, capsys)[0] == 0


def test_the_cli_runs_it(config, home, monkeypatch, capsys):
    monkeypatch.setenv(nr.CONFIG_ENV, config[1])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call("sometool init -g"))))
    assert cli.main(["hook", "no-restore"]) == 2
    assert "Blocked: " in capsys.readouterr().err
