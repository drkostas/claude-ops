"""The stale-status hook: a shell script that reads `$?` after a command substitution in the same command
is refused in a Write, an Edit, a MultiEdit and in text a Bash command writes into it, and everything
else goes through."""
import io
import json
import shutil
import subprocess

import pytest

from claude_ops import cli
from claude_ops.hooks import chain
from claude_ops.hooks import stale_status as ss

# Each line prints "rc <n>" after `false`. The hook must refuse exactly the lines bash reads wrong
# (rc 0, the status of the substitution) and let through the ones it reads right (rc 1).
WRONG = [
    'echo "$(true) rc $?"',
    'echo "`true` rc $?"',
    'echo "rc $(( $(true; echo 0) + $? ))"',
    'x=$(true)$?; echo "rc $x"',
    'cat <<EOF\n$(true)\nrc $?\nEOF',
    # later words of the same simple command, which bash expands from left to right
    'echo $(true) rc $?',
    'echo "$(true)" "rc $?"',
    'printf "%s rc %s\\n" "$(true)" "$?"',
    'a=$(true) b=$?; echo "rc $b"',
    'f() { local a=$(true) b=$?; echo "rc $b"; }; f',
    'echo $(true) \\\nrc $?',
    # a here-document's body is expanded after the words of the command that opened it
    'cat $(echo /dev/null) - <<EOF\nrc $?\nEOF',
]
RIGHT = [
    'rc=$?; echo "$(true) rc $rc"',
    'echo "rc $?"',
    'echo "rc $? $(true)"',
    'echo "rc $(( $? + 0 ))"',
    'echo "rc $(echo $?)"',
    'echo rc $? $(true)',
    'echo rc $? `true`',
    'echo "rc $(echo $?)" $(true)',
    'rc=$?; echo $(true) rc $rc',
    'cat <<EOF; true $(true)\nrc $?\nEOF',
]
# Text, not a read of the status: nothing to run, nothing to refuse.
TEXT = [
    "echo '$(date) rc $?'",
    'echo "\\$(date) rc \\$?"',
    '# echo "$(date) rc $?"',
    "cat <<'EOF'\n$(date)\nrc $?\nEOF",
    'cat <<"EOF"\n$(date) rc $?\nEOF',
    '(( x << 2 ))\necho "$(date)"\necho "rc $?"',
    'out=$(cmd) || rc=$?\nif [ $? -ne 0 ]; then echo failed; fi',
    'echo $(date) && echo "rc $?"',
    '[ -n "$(date)" ]; echo $?',
    'for f in $(ls); do echo $?; done',
]


def write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}, "cwd": "/"}


def bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize("line,expected", [(w, "rc 0") for w in WRONG] + [(r, "rc 1") for r in RIGHT])
def test_bash_reads_the_status_as_the_hook_says(line, expected):
    out = subprocess.run(["bash", "-c", f"false; {line}"], capture_output=True, text=True).stdout
    assert out.strip().splitlines()[-1] == expected
    assert bool(ss.stale_lines(line)) == (expected == "rc 0")


@pytest.mark.parametrize("text", TEXT)
def test_quotes_escapes_comments_and_other_statements_go_through(text):
    assert ss.stale_lines(text) == []


def test_text_with_no_status_read_is_not_scanned(monkeypatch):
    def no_scan(*a, **k):
        raise AssertionError("scanned")
    monkeypatch.setattr(ss.Scanner, "script", no_scan)
    assert ss.stale_lines('echo "$(date) done"\n') == []
    assert ss.new_hits('echo "$(date) $?"\n', 'echo "$(date) done"\n') == []
    with pytest.raises(AssertionError):
        ss.stale_lines('echo "$(date) $?"')


def test_an_edit_that_puts_a_substitution_before_an_old_status_read_is_refused(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text('run_job\necho "rc $?"\n')
    edit = {"tool_name": "Edit", "cwd": "/", "tool_input": {
        "file_path": str(script), "old_string": 'echo "rc', "new_string": 'echo "$(date) rc'}}
    assert ss.check(edit).startswith("line 2 of ")


def test_a_finding_names_the_line_it_is_on():
    script = "#!/bin/bash\nrun_job\ncat <<EOF\nstarted $(date)\nended with $?\nEOF\nother\n"
    assert ss.stale_lines(script) == [(5, "ended with $?")]
    assert ss.stale_lines('a\nb "`date`\n$?"') == [(3, '$?"')]


def test_a_write_of_a_shell_script_is_refused_and_the_reason_gives_the_fix(tmp_path):
    why = ss.check(write(tmp_path / "job.sh", 'run_job\necho "$(date +%H:%M) job rc $?"\n'))
    assert why and why.startswith("line 2 of ") and "cmd; rc=$?" in why
    two = ss.check(write(tmp_path / "job.sh", 'echo "$(date) $?"\necho "`date` $?"\n'))
    assert "(and 1 more)" in two


def test_a_file_is_a_shell_script_by_its_name_or_its_first_line(tmp_path):
    bad = 'echo "$(date) rc $?"\n'
    for first in ("#!/bin/sh\n", "#!/bin/bash -e\n", "#!/usr/bin/env bash\n", "#! /usr/bin/env -S bash -eu\n"):
        assert ss.check(write(tmp_path / "job", first + bad)), first
    for first in ("#!/usr/bin/env python3\n", "#!/bin/zshell\n", ""):
        assert ss.check(write(tmp_path / "job", first + bad)) is None, first
    assert ss.check(write(tmp_path / "notes.md", bad)) is None
    assert ss.check(write(tmp_path / "job.py", bad)) is None


def test_an_edit_is_refused_only_for_a_line_the_file_did_not_have(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text('run_job\necho "$(date) rc $?"\necho done\n')
    edit = {"tool_name": "Edit", "cwd": "/", "tool_input": {
        "file_path": str(script), "old_string": "echo done", "new_string": "echo finished"}}
    assert ss.check(edit) is None
    edit["tool_input"]["new_string"] = 'echo "finished at `date`, rc $?"'
    assert ss.check(edit).startswith("line 3 of ")
    multi = {"tool_name": "MultiEdit", "cwd": "/", "tool_input": {"file_path": str(script), "edits": [
        {"old_string": "run_job", "new_string": "run_job --all"},
        {"old_string": "echo done", "new_string": 'echo "$(date) done $?"'}]}}
    assert ss.check(multi).startswith("line 3 of ")


def test_an_edit_of_a_script_named_by_its_first_line_is_read_too(tmp_path):
    script = tmp_path / "job"
    script.write_text("#!/usr/bin/env bash\nrun_job\n")
    edit = {"tool_name": "Edit", "cwd": "/", "tool_input": {
        "file_path": str(script), "old_string": "run_job", "new_string": 'run_job; echo "$(date) $?"'}}
    assert ss.check(edit)
    other = tmp_path / "notes.txt"
    other.write_text("run_job\n")
    edit["tool_input"]["file_path"] = str(other)
    assert ss.check(edit) is None


def test_an_edit_whose_old_text_is_missing_is_judged_by_its_new_text(tmp_path):
    edit = {"tool_name": "Edit", "cwd": "/", "tool_input": {
        "file_path": str(tmp_path / "gone.sh"), "old_string": "x", "new_string": 'echo "$(date) $?"'}}
    assert ss.check(edit)


def test_text_a_bash_command_writes_into_a_script_is_refused_but_its_own_line_is_not(tmp_path):
    assert ss.check(bash("cat > job.sh <<'EOF'\nrun_job\necho \"$(date) rc $?\"\nEOF", tmp_path))
    assert ss.check(bash("cat > job <<'EOF'\n#!/bin/bash\necho \"`date` $?\"\nEOF", tmp_path))
    assert ss.check(bash("echo 'echo \"$(date) $?\"' >> job.sh", tmp_path))
    assert ss.check(bash("sed -i '' 's/rc=$rc/echo \"$(date) rc $?\"/' job.sh", tmp_path))
    assert ss.check(bash("sed -i '' 's/echo \"$(date) rc $?\"/rc=$rc/' job.sh", tmp_path)) is None
    assert ss.check(bash("cat > notes.txt <<'EOF'\necho \"$(date) rc $?\"\nEOF", tmp_path)) is None
    assert ss.check(bash('run_job; echo "$(date) rc $?"', tmp_path)) is None


def test_other_tools_and_broken_input_go_through(tmp_path):
    assert ss.check({}) is None
    assert ss.check({"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "x.sh")}}) is None
    assert ss.check({"tool_name": "Write", "tool_input": {}}) is None


def test_main_says_blocked_with_the_fix(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(write(tmp_path / "job.sh", 'echo "$(date) $?"'))))
    assert ss.main([]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Blocked: line 1 of ") and "cmd; rc=$?" in err
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(write(tmp_path / "job.sh", "rc=$?"))))
    assert ss.main([]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert ss.main([]) == 0


def test_the_cli_and_the_chain_offer_the_hook(tmp_path, monkeypatch):
    assert any(h.get("matcher") == "Bash|Write|Edit|MultiEdit"
               and h["hooks"][0]["command"] == "claude-ops hook stale-status" for h in cli.HOOKS["PreToolUse"])
    call = json.dumps(write(tmp_path / "job.sh", 'echo "$(date) $?"'))
    monkeypatch.setattr("sys.stdin", io.StringIO(call))
    assert cli.main(["hook", "stale-status"]) == 2
    rc, out, err = chain.run_module("claude_ops.hooks.stale_status", [], call)
    assert rc == 2 and err.startswith("Blocked:") and out == ""
    rc, out, err = chain.run_module("claude_ops.hooks.stale_status", [], json.dumps(write(tmp_path / "job.sh", "rc=$?")))
    assert rc == 0 and err == ""


# --- a background command that hides a gate's exit ------------------------------------------------
# Each command runs with a stand-in `gh` that fails, as a red check would. The hook refuses exactly the
# commands the shell then reports as a success (HIDDEN), and lets through the ones whose exit still
# carries the failure (REPORTED).
HIDDEN = [
    'gh pr checks 7 --watch; echo "rc=$?" >> log',
    'gh pr checks 7 --watch --fail-fast >/dev/null; echo "checks exit $?"',
    'gh pr checks 7 --watch 2>&1 | tail -3',
    'cd . && gh run watch 9 --exit-status | tail -1',
    'gh pr checks 7 --watch || echo "the checks failed"',
    'gh pr checks 7 --watch && echo green || echo red',
    'gh pr checks 7 --watch; echo done; exit 0',
    'gh pr checks 7 --watch; echo done; exit $?',
    'gh pr checks 7 --watch\necho done',
    'set -e; gh pr checks 7 --watch && echo green; echo done',
    'if true; then gh pr checks 7 --watch; fi; echo done',
]
REPORTED = [
    'gh pr checks 7 --watch',
    'cd . && gh pr checks 7 --watch',
    'echo start\ngh pr checks 7 --watch',
    'gh pr checks 7 --watch && echo merged',
    'gh pr checks 7 --watch; rc=$?; echo "rc=$rc" >> log; exit "$rc"',
    'gh pr checks 7 --watch; exit $?',
    'gh pr checks 7 --watch; rc=$?; exit ${rc:-1}',
    'set -o pipefail; gh pr checks 7 --watch 2>&1 | tail -3',
    'set -e; gh pr checks 7 --watch; echo green',
    'gh pr checks 7 --watch || exit 1; echo green',
    'for p in 7 8; do gh pr checks $p --watch || exit 1; done; echo all green',
    'if [ -n "x" ]; then gh pr checks 7 --watch; else echo none; fi',
]
# The command tests the gate's status itself, so the hook leaves it to the command.
HANDLED = [
    'if gh pr checks 7 --watch; then echo green; else echo red; fi; echo done',
    'if cd . && gh pr checks 7 --watch; then echo green; fi',
    'until gh run watch 9 --exit-status; do break; done; echo done',
    '! gh pr checks 7 --watch; echo inverted',
]
# Not a gate: no --watch, no --exit-status, or another gh command.
NOT_GATES = ['gh run watch 9; echo done', 'gh pr checks 7; echo done', 'gh pr view 7 | tail -1']
SHELLS = [s for s in ("bash", "zsh") if shutil.which(s)]


def background(command, cwd="/"):
    return {"tool_name": "Bash", "tool_input": {"command": command, "run_in_background": True}, "cwd": str(cwd)}


@pytest.fixture
def failing_gh(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    gh = folder / "gh"
    gh.write_text("#!/bin/sh\necho 'tests  fail  1m'\nexit 1\n")
    gh.chmod(0o755)
    return {"PATH": f"{folder}:/usr/bin:/bin", "HOME": str(tmp_path)}


@pytest.mark.skipif(not SHELLS, reason="needs bash or zsh")
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("command,hidden", [(c, True) for c in HIDDEN] + [(c, False) for c in REPORTED])
def test_the_hook_refuses_the_commands_the_shell_reports_as_green(command, hidden, shell, failing_gh, tmp_path):
    rc = subprocess.run([shell, "-c", command], cwd=tmp_path, env=failing_gh, capture_output=True).returncode
    assert (rc == 0) == hidden
    assert bool(ss.check(background(command, tmp_path))) == hidden


@pytest.mark.parametrize("command", HANDLED + NOT_GATES)
def test_a_tested_gate_and_other_commands_go_through(command):
    assert ss.check(background(command)) is None


def test_a_foreground_command_is_not_read():
    for command in HIDDEN:
        assert ss.check(bash(command, "/")) is None
        call = background(command)
        call["tool_input"]["run_in_background"] = False
        assert ss.check(call) is None


def test_a_gate_behind_a_wrapper_is_read():
    assert ss.check(background("timeout 600 gh pr checks 7 --watch; echo done"))
    assert ss.check(background("rtk gh pr checks 7 --watch | tail -3"))
    assert ss.check(background("env GH_PAGER= gh run watch 9 --exit-status\necho done"))


def test_the_reason_gives_the_fix_for_its_shape():
    pipe = ss.check(background("gh pr checks 7 --watch 2>&1 | tail -3"))
    assert "pipes gh pr checks into `tail -3`" in pipe and "set -o pipefail" in pipe
    later = ss.check(background('gh pr checks 7 --watch; echo "rc=$?" >> log'))
    assert "then runs `echo rc=$?`" in later and 'rc=$?; echo "rc=$rc" >> log; exit $rc' in later
    either = ss.check(background("gh run watch 9 --exit-status || echo failed"))
    assert "runs `|| echo failed` when gh run watch fails" in either


def test_an_unreadable_command_goes_through():
    assert ss.check(background('gh pr checks 7 --watch; echo "unfinished')) is None
    assert ss.check({"tool_name": "Bash", "tool_input": {"run_in_background": True}}) is None


def test_main_refuses_a_hidden_background_gate(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(background('gh pr checks 7 --watch; echo "rc=$?"'))))
    assert ss.main([]) == 2
    assert capsys.readouterr().err.startswith("Blocked: this background command then runs")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(background("gh pr checks 7 --watch"))))
    assert ss.main([]) == 0
