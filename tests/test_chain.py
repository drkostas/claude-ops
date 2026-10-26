"""The hook chain: several hooks in one interpreter, in order, refusing on the first refusal."""
import io
import json
import os
import stat
import subprocess
import sys

import pytest

from claude_ops.hooks import chain

ALLOW = '''
import json, sys
def main(argv=None):
    json.load(sys.stdin)
    open({marker!r}, "a").write("{name} " + " ".join(argv or []) + "\\n")
    return 0
'''
REFUSE = '''
import json, sys
def main(argv=None):
    p = json.load(sys.stdin)
    open({marker!r}, "a").write("{name}\\n")
    if "bad" in p["tool_input"].get("command", ""):
        print("Blocked: the command says bad.", file=sys.stderr)
        return 2
    return 0
'''
NO_ARGV = '''
import json, sys
def main():
    json.load(sys.stdin)
    open({marker!r}, "a").write("{name} " + " ".join(sys.argv[1:]) + "\\n")
    return 0
'''
BROKEN = '''
def main(argv=None):
    raise RuntimeError("this hook is broken")
'''
CONTEXT = '''
import json, sys
def main(argv=None):
    json.load(sys.stdin)
    print(json.dumps({{"hookSpecificOutput": {{"hookEventName": "PreToolUse", "additionalContext": "{name} says hi"}}}}))
    return 0
'''
EXIT2 = '''
import sys
def main(argv=None):
    print("usage: something went wrong", file=sys.stderr)
    sys.exit(2)
'''


@pytest.fixture()
def hooks(tmp_path, monkeypatch):
    """A folder of hook modules on sys.path, and the file each one marks when it runs."""
    marker = str(tmp_path / "ran.txt")
    for name, body in (("h_allow", ALLOW), ("h_refuse", REFUSE), ("h_noargv", NO_ARGV), ("h_broken", BROKEN),
                       ("h_context", CONTEXT), ("h_exit2", EXIT2), ("h_after", ALLOW)):
        (tmp_path / f"{name}.py").write_text(body.format(marker=marker, name=name))
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in list(sys.modules):
        if name.startswith("h_"):
            del sys.modules[name]
    return tmp_path, marker


def call(command="ls", tool="Bash", event="PreToolUse"):
    return json.dumps({"hook_event_name": event, "tool_name": tool, "tool_input": {"command": command}, "cwd": "/"})


def ran(marker):
    return open(marker).read().split("\n")[:-1] if os.path.exists(marker) else []


def test_steps_run_in_order_in_one_interpreter_and_a_call_they_allow_goes_on(hooks):
    _, marker = hooks
    rc, out, err = chain.chain(call(), [{"module": "h_allow", "args": ["--x", "1"]}, {"module": "h_refuse"},
                                        {"module": "h_noargv", "args": ["--y"]}])
    assert (rc, out, err) == (0, "", "")
    assert ran(marker) == ["h_allow --x 1", "h_refuse", "h_noargv --y"]


def test_the_first_refusal_stops_the_chain_with_its_reason(hooks):
    _, marker = hooks
    rc, out, err = chain.chain(call("bad thing"), [{"module": "h_refuse"}, {"module": "h_after"}])
    assert rc == 2 and err.startswith("Blocked: the command says bad.")
    assert ran(marker) == ["h_refuse"]


def test_a_step_runs_only_for_its_tools_and_events(hooks):
    _, marker = hooks
    steps = [{"module": "h_allow", "tools": "Write|Edit"}, {"module": "h_noargv", "tools": "mcp__github__.*"},
             {"module": "h_after", "tools": "Bash", "events": ["PostToolUse"]}]
    chain.chain(call(), steps)
    chain.chain(call(tool="mcp__github__create_pull_request"), steps)
    chain.chain(call(tool="Bash", event="PostToolUse"), steps)
    chain.chain(call(tool="BashOutput"), [{"module": "h_allow", "tools": "Bash"}])
    assert ran(marker) == ["h_noargv ", "h_after "]


def test_a_broken_step_or_a_bare_exit_2_never_refuses(hooks):
    _, marker = hooks
    rc, _, err = chain.chain(call(), [{"module": "h_broken"}, {"module": "h_exit2"}, {"module": "no_such_module"},
                                      {"module": "h_after"}])
    assert rc == 0 and err == ""
    assert ran(marker) == ["h_after "]


def test_answers_are_merged_and_a_deny_stops_the_chain(hooks, tmp_path):
    rc, out, _ = chain.chain(call(), [{"module": "h_context"}, {"module": "h_context"}])
    spec = json.loads(out)["hookSpecificOutput"]
    assert rc == 0 and spec["additionalContext"] == "h_context says hi\n\nh_context says hi"
    deny = tmp_path / "deny.sh"
    deny.write_text('#!/bin/sh\necho \'{"hookSpecificOutput": {"permissionDecision": "deny", '
                    '"permissionDecisionReason": "no"}}\'\n')
    deny.chmod(deny.stat().st_mode | stat.S_IEXEC)
    rc, out, _ = chain.chain(call(), [{"command": [str(deny)]}, {"module": "h_after"}])
    assert rc == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ran(hooks[1]) == []


def test_a_command_step_gets_the_call_and_the_modules_that_passed_and_its_output_goes_on(hooks, tmp_path):
    script = tmp_path / "rewrite.sh"
    script.write_text('#!/bin/sh\nin=$(cat)\nprintf \'{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
                      '"updatedInput": {"command": "%s"}}}\' "$CLAUDE_OPS_CHAIN_PASSED"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    rc, out, _ = chain.chain(call(), [{"module": "h_allow"}, {"module": "h_refuse"},
                                      {"command": [str(script)], "tools": "Bash"}])
    assert rc == 0 and json.loads(out)["hookSpecificOutput"]["updatedInput"] == {"command": "h_allow,h_refuse"}


def test_a_command_step_refuses_only_with_the_signature(tmp_path):
    for body, want in (('echo "Blocked: no." >&2; exit 2', 2), ('echo "usage: -P" >&2; exit 2', 0)):
        script = tmp_path / "s.sh"
        script.write_text(f"#!/bin/sh\ncat >/dev/null\n{body}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        assert chain.chain(call(), [{"command": [str(script)]}])[0] == want


def test_a_real_hook_refuses_through_the_chain():
    pipe = "ls | rtk proxy grep x"
    rc, _, err = chain.chain(call(pipe), [{"module": "claude_ops.hooks.ignores_stdin"}])
    assert rc == 2 and err.startswith("Blocked:")
    assert chain.chain(call("ls -la"), [{"module": "claude_ops.hooks.ignores_stdin"}])[0] == 0


def test_config_and_main(tmp_path, monkeypatch, capsys, hooks):
    f = tmp_path / "chain.json"
    f.write_text(json.dumps({"steps": [{"module": "h_refuse"}, "not a step"]}))
    assert chain.config(["--config", str(f)]) == [{"module": "h_refuse"}]
    monkeypatch.setenv("CLAUDE_OPS_HOOK_CHAIN_CONFIG", str(tmp_path / "missing.json"))
    assert chain.config([]) == []
    monkeypatch.setattr("sys.stdin", io.StringIO(call("bad")))
    assert chain.main(["--config", str(f)]) == 2
    assert capsys.readouterr().err.startswith("Blocked:")
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert chain.main(["--config", str(f)]) == 0


def test_the_cli_runs_it():
    src = os.path.dirname(os.path.dirname(os.path.dirname(chain.__file__)))   # this checkout's src/
    p = subprocess.run([sys.executable, "-P", "-m", "claude_ops.hooks.chain", "--config", "/nonexistent.json"],
                       input=call(), capture_output=True, text=True, env={**os.environ, "PYTHONPATH": src})
    assert p.returncode == 0 and p.stdout == ""
