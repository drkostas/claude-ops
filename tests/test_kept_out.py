"""The kept-out hook: text a rule keeps out of a set of files is refused in a Write, an Edit, a
MultiEdit, a NotebookEdit and in text a Bash command writes, and everything else goes through."""
import io
import json

import pytest

from claude_ops import cli
from claude_ops.hooks import chain
from claude_ops.hooks import kept_out as ko

RULE = {"files": ["~/notes/*.md", "~/skills/**/*.md"],
        "text": r"~/scratch/([A-Za-z0-9_.\-]+)",
        "allow": ["README.md"],
        "message": "Name the script by what it does."}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path.resolve()          # a temporary folder can itself sit behind a link
    monkeypatch.setenv("HOME", str(h))
    (h / "notes").mkdir()
    (h / "skills" / "a" / "b").mkdir(parents=True)
    return h


@pytest.fixture
def config(home):
    path = home / "kept-out.json"
    path.write_text(json.dumps({"rules": [RULE]}))
    return path


def rules():
    return [ko.Rule(RULE)]


def write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}, "cwd": "/"}


def bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def test_a_glob_keeps_star_inside_one_folder_and_double_star_takes_any_number():
    assert ko.glob_regex("/h/notes/*.md").match("/h/notes/a.md")
    assert not ko.glob_regex("/h/notes/*.md").match("/h/notes/deep/a.md")
    for p in ("/h/skills/a.md", "/h/skills/x/a.md", "/h/skills/x/y/a.md"):
        assert ko.glob_regex("/h/skills/**/*.md").match(p)
    assert not ko.glob_regex("/h/skills/**/*.md").match("/h/skills/a.txt")
    assert ko.glob_regex("/h/a?.md").match("/h/ab.md") and not ko.glob_regex("/h/a?.md").match("/h/a/.md")
    assert ko.glob_regex("/h/logs/**").match("/h/logs/x/y")


def test_a_write_that_names_the_text_in_a_covered_file_is_refused_with_its_line(home):
    why = ko.check(write(home / "notes" / "n.md", "first\nrun ~/scratch/tidy.py nightly\n"), rules())
    assert why and "'~/scratch/tidy.py'" in why and "line 2" in why and "Name the script by what it does." in why
    assert ko.check(write(home / "skills" / "a" / "b" / "s.md", "~/scratch/x ~/scratch/y"), rules()).count("1 more") == 1


def test_a_file_the_rule_does_not_cover_and_text_it_does_not_name_go_through(home):
    assert ko.check(write(home / "elsewhere.md", "~/scratch/tidy.py"), rules()) is None
    assert ko.check(write(home / "notes" / "deep" / "n.md", "~/scratch/tidy.py"), rules()) is None
    assert ko.check(write(home / "notes" / "n.md", "run the nightly tidy step"), rules()) is None
    assert ko.check(write(home / "notes" / "n.md", "see ~/scratch/README.md"), rules()) is None   # allowed by name


def test_an_edit_is_judged_by_its_new_text_only(home):
    note = home / "notes" / "n.md"
    edit = {"tool_name": "Edit", "cwd": "/", "tool_input": {
        "file_path": str(note), "old_string": "run ~/scratch/tidy.py", "new_string": "run the tidy step"}}
    assert ko.check(edit, rules()) is None
    edit["tool_input"]["new_string"] = "run ~/scratch/tidy.py daily"
    assert "tidy.py" in ko.check(edit, rules())
    multi = {"tool_name": "MultiEdit", "cwd": "/", "tool_input": {"file_path": str(note), "edits": [
        {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "~/scratch/two.sh"}]}}
    assert "two.sh" in ko.check(multi, rules())
    nb = {"tool_name": "NotebookEdit", "cwd": "/", "tool_input": {
        "notebook_path": str(home / "skills" / "n.md"), "new_source": "!~/scratch/cell.py"}}
    assert "cell.py" in ko.check(nb, rules())


def test_a_relative_path_is_read_against_the_call_folder(home):
    call = {"tool_name": "Write", "cwd": str(home / "notes"), "tool_input": {"file_path": "n.md", "content": "~/scratch/x"}}
    assert ko.check(call, rules())


def test_text_a_bash_command_writes_into_a_covered_file_is_refused(home):
    assert ko.check(bash("cat > ~/notes/n.md <<'EOF'\nuse ~/scratch/tidy.py\nEOF", home), rules())
    assert ko.check(bash("echo '~/scratch/tidy.py' >> notes/n.md", home), rules())
    assert ko.check(bash("printf 'use ~/scratch/tidy.py' | tee ~/skills/a/s.md", home), rules())
    assert ko.check(bash("echo '~/scratch/tidy.py' > ~/other.md", home), rules()) is None
    assert ko.check(bash("cat ~/notes/n.md", home), rules()) is None


def test_a_file_reached_through_a_link_is_judged_where_it_really_is(home):
    (home / "elsewhere").mkdir()
    (home / "elsewhere" / "notes").symlink_to(home / "notes")
    assert ko.check(write(home / "elsewhere" / "notes" / "n.md", "~/scratch/tidy.py"), rules())


def test_no_rules_a_bad_rule_and_a_broken_config_refuse_nothing(home, tmp_path):
    assert ko.check(write(home / "notes" / "n.md", "~/scratch/tidy.py"), []) is None
    assert ko.load(str(tmp_path / "missing.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ko.load(str(bad)) == []
    mixed = tmp_path / "mixed.json"
    mixed.write_text(json.dumps({"rules": [{"files": ["~/x"]}, {"files": ["~/y"], "text": "("}, RULE]}))
    assert len(ko.load(str(mixed))) == 1
    mixed.write_text(json.dumps(["not", "a", "dict"]))
    assert ko.load(str(mixed)) == []
    assert ko.check({}, rules()) is None and ko.check({"tool_name": "Bash"}, rules()) is None


def test_main_reads_the_config_and_says_blocked(home, config, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(write(home / "notes" / "n.md", "~/scratch/tidy.py"))))
    assert ko.main(["--config", str(config)]) == 2
    assert capsys.readouterr().err.startswith("Blocked: the new text for ")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(write(home / "notes" / "n.md", "plain"))))
    assert ko.main(["--config", str(config)]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert ko.main(["--config", str(config)]) == 0
    monkeypatch.setenv(ko.CONFIG_ENV, str(config))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(write(home / "notes" / "n.md", "~/scratch/tidy.py"))))
    assert ko.main([]) == 2


def test_the_cli_and_the_chain_offer_the_hook(home, config, monkeypatch):
    assert any(h.get("matcher") == "Bash|Write|Edit|MultiEdit|NotebookEdit"
               and h["hooks"][0]["command"] == "claude-ops hook kept-out" for h in cli.HOOKS["PreToolUse"])
    monkeypatch.setenv(ko.CONFIG_ENV, str(config))
    call = json.dumps(write(home / "notes" / "n.md", "~/scratch/tidy.py"))
    monkeypatch.setattr("sys.stdin", io.StringIO(call))
    assert cli.main(["hook", "kept-out"]) == 2
    rc, out, err = chain.run_module("claude_ops.hooks.kept_out", ["--config", str(config)], call)
    assert rc == 2 and err.startswith("Blocked:") and out == ""
    rc, out, err = chain.run_module("claude_ops.hooks.kept_out", ["--config", str(config)],
                                    json.dumps(write(home / "notes" / "n.md", "plain")))
    assert rc == 0 and err == ""
