"""The ignores-stdin hook: a pipe into a command that never reads its input is refused.

The shape this exists for printed nothing and was read as "no matches", so the cases below are
mostly about telling that shape apart from the ones that work.
"""
import io
import json

import pytest

from claude_ops.hooks import ignores_stdin as hook

COMMANDS = [["rtk", "proxy"]]


def bash(command, commands=None):
    return hook.check({"tool_name": "Bash", "tool_input": {"command": command}},
                      commands if commands is not None else COMMANDS)


@pytest.mark.parametrize("command", [
    "rbw list --fields name | rtk proxy grep pypi",
    "cat names.txt | rtk proxy grep -i x",
    # the hook has to see this one before any rewriting does: the left side is a candidate itself
    "grep foo file | rtk proxy grep bar",
    # an absolute path is the same program
    "lister | /opt/homebrew/bin/rtk proxy grep x",
    # assignments and wrappers in front of it do not hide it
    "lister | VAR=1 env rtk proxy grep x",
    # the pipe is not always on the first line
    "cd /tmp\nls | rtk proxy grep x",
    # the second pipe of three
    "a | b | rtk proxy grep x",
])
def test_a_pipe_into_it_is_refused(command):
    why = bash(command)
    assert why and "pipe into" in why and "rtk proxy" in why


def test_a_here_document_given_to_it_is_refused():
    why = bash("rtk proxy python3 - <<EOF\nprint(1)\nEOF")
    assert why and "here-document" in why


@pytest.mark.parametrize("command", [
    # ⭐ THE SHAPE THAT WORKS, and the one the refusal suggests: the pipe is inside the argument,
    # so the wrapper runs a shell that does the piping itself
    "rtk proxy bash -c 'rbw list | grep pypi'",
    # its own output piped onward is fine: it writes the pipe, it does not read one
    "rtk proxy git log | head -5",
    "rtk proxy git log | grep x | wc -l",
    # `||` and `&&` are not pipes
    "lister || rtk proxy git status",
    "lister && rtk proxy git status",
    # no pipe at all
    "rtk proxy grep foo file",
    # a here-document inside a quoted argument belongs to the shell it runs, not to the wrapper
    "rtk proxy bash -c 'cat <<EOF\nx\nEOF'",
    # ⭐ A HERE-DOCUMENT BODY IS DATA, NOT WORDS, and this is why the check belongs here rather
    # than in a regular expression over the command text. A first version of this guard, written
    # as one, refused the paragraph that documents the guard, because the body quoted the shape
    # being refused. `commandline.parse` sets the bodies aside before splitting, so these pass.
    'python3 - <<EOF\nprint("a | rtk proxy grep x")\nEOF',
    'cat <<EOF > notes.md\nlister | rtk proxy grep x prints nothing\nEOF',
    # a pipe into something that does read its input
    "rbw list | grep pypi",
    "cat f | rtk grep pypi",
    # an unfinished quote cannot be read as words, and a hook that cannot read says nothing
    "lister | rtk proxy grep 'x",
])
def test_the_shapes_that_work_go_through(command):
    assert bash(command) is None


def test_only_bash_is_judged():
    assert hook.check({"tool_name": "Write",
                       "tool_input": {"file_path": "/tmp/x", "content": "a | rtk proxy grep b"}},
                      COMMANDS) is None


def test_nothing_configured_refuses_nothing():
    assert bash("a | rtk proxy grep b", []) is None


def test_another_command_can_be_named(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_OPS_IGNORES_STDIN_CONFIG", raising=False)
    commands, message = hook.config(["--command", "deaf-tool run"])
    assert commands == [["deaf-tool", "run"]] and message == ""
    assert bash("a | deaf-tool run x", commands)
    assert bash("a | rtk proxy grep b", commands) is None


def test_the_file_names_the_commands_and_an_extra_message(tmp_path, monkeypatch):
    path = tmp_path / "ignores-stdin.json"
    path.write_text(json.dumps({"commands": ["deaf-tool"], "message": "Read the file instead."}),
                    encoding="utf-8")
    monkeypatch.setenv("CLAUDE_OPS_IGNORES_STDIN_CONFIG", str(path))
    commands, message = hook.config([])
    assert commands == [["deaf-tool"]] and message == "Read the file instead."


def test_the_default_stands_when_there_is_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_OPS_IGNORES_STDIN_CONFIG", str(tmp_path / "missing.json"))
    commands, message = hook.config([])
    assert commands == [["rtk", "proxy"]] and message == ""


def test_an_unreadable_file_leaves_the_default(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_OPS_IGNORES_STDIN_CONFIG", str(path))
    commands, _ = hook.config([])
    assert commands == [["rtk", "proxy"]]


def test_main_refuses_with_exit_2_and_says_why(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "a | rtk proxy grep b"}})))
    assert hook.main(["--command", "rtk proxy"]) == 2
    said = capsys.readouterr().err
    assert "prints nothing" in said and "bash -c" in said


def test_main_lets_a_working_shape_through(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "rtk proxy bash -c 'a | grep b'"}})))
    assert hook.main(["--command", "rtk proxy"]) == 0


def test_a_payload_it_cannot_read_lets_the_call_through(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert hook.main(["--command", "rtk proxy"]) == 0
