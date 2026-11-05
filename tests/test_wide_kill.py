"""The wide-kill hook: wide signals are refused, narrow ones are not.

Nothing here signals a process or reads the real process table. The processes `pgrep` would list
are a fake, and the one test that sends a signal starts its own child.
"""
import io
import json
import subprocess
import sys

import pytest

from claude_ops.hooks import wide_kill as hook

CFG = {"protected": {"names": ["sshd", "tailscaled"], "labels": ["com.example.tunnel"],
                     "paths": ["/usr/sbin/sshd"]}}


def fake(lines=None):
    seen = []

    def resolve(args):
        seen.append(args)
        return lines

    resolve.seen = seen
    return resolve


def run(command, lines=(), cfg=CFG, groups=()):
    return hook.check({"tool_name": "Bash", "tool_input": {"command": command}}, cfg, fake(list(lines)),
                      lambda: set(groups))


@pytest.mark.parametrize("command", [
    "kill -1", "kill -9 -1", "kill 0", "kill -TERM 0", "kill 1", "kill -- -1234", "kill -9 -4321",
    "kill -- -$PGID", "kill -9 1 2", "kill 1 2 3 4", "kill $a $b $c $d",
    "killall node", "killall5", "sudo killall -9 Finder", "skill -u me",
    "pkill -u me", "pkill -9 -U 501", "pkill -v keepme",
    "xargs killall", "echo x | xargs -n1 killall",
    "bash -c 'kill -1'", "sh -c \"cd /tmp && kill 0\"", "eval 'killall node'",
    "echo $(kill -1)",
])
def test_structural_refusals(command):
    assert run(command)


@pytest.mark.parametrize("command", [
    "kill 4242", "kill -TERM $!", "kill -9 4242 4243 4244", "kill -0 4242", "kill -s TERM 4242",
    "kill -l", "kill -- 4242", "kill -TERM -$!", "sudo kill 4242",
    "lsof -ti:4321 | xargs kill", "lsof -ti:4321 | xargs -r kill -9", "kill $(lsof -ti:3000)",
    "kill $(cat app.pid)", "echo killed", "git commit -m 'kill -1 is refused now'",
    "ls | xargs echo", "grep -r skill docs", "ps -p 4242 -o pid= | xargs kill",
])
def test_narrow_kills_are_allowed(command):
    assert run(command) is None


def test_pattern_matching_a_protected_name_is_refused_without_reading_processes():
    resolve = fake([])
    cmd = "pkill -f ssh"
    assert hook.check({"tool_name": "Bash", "tool_input": {"command": cmd}}, CFG, resolve)
    assert not resolve.seen


def test_exact_pattern_must_equal_the_name():
    assert run("pkill -x ssh") is None
    assert run("pkill -x sshd")


def test_pattern_reaching_a_protected_process_is_refused():
    why = run("pkill -f 'web --port 80'", ["/opt/web --port 80", "/usr/sbin/sshd -D"])
    assert "sshd" in why or "/usr/sbin/sshd" in why


def test_pattern_reaching_a_protected_label_is_refused():
    assert run("pkill -f tunnel-agent", ["/x/tunnel-agent --label com.example.tunnel"])


def test_pattern_matching_many_processes_is_refused():
    lines = [f"/usr/bin/worker {i}" for i in range(5)]
    assert "5 processes" in run("pkill -f worker", lines)
    assert run("pkill -f worker", lines[:3]) is None


def test_an_unreadable_process_table_does_not_refuse():
    assert hook.check({"tool_name": "Bash", "tool_input": {"command": "pkill -f worker"}}, CFG, fake(None)) is None


def test_pgrep_piped_into_kill_is_a_pattern_kill():
    lines = [f"/usr/bin/worker {i}" for i in range(5)]
    assert run("pgrep -f worker | xargs kill", lines)
    assert run("kill $(pgrep -f worker)", lines)
    assert run('kill -9 "$(pgrep -f worker)"', lines)
    assert run("for p in $(pgrep -f worker); do kill $p; done", lines)
    assert run("pgrep -f worker | xargs kill", lines[:1]) is None
    assert run("kill $(pgrep -f worker)", lines[:1]) is None


def test_pgrep_without_a_kill_is_left_alone():
    lines = [f"/usr/bin/worker {i}" for i in range(9)]
    assert run("pgrep -f worker", lines) is None
    assert run("echo $(pgrep -f worker)", lines) is None


def test_ps_listing_is_read_through_its_grep_pattern():
    many = [f"/usr/bin/worker {i}" for i in range(5)]
    assert run("ps aux | grep worker | grep -v grep | awk '{print $2}' | xargs kill", many)
    assert run("kill $(ps aux | grep worker | awk '{print $2}')", many)
    assert run("ps aux | grep worker | awk '{print $2}' | xargs kill", many[:2]) is None
    assert run("ps -eo pid,command | grep sshd | awk '{print $1}' | xargs kill", ["/usr/sbin/sshd -D"])


def test_ps_listing_with_no_pattern_is_refused_only_when_it_feeds_the_kill():
    assert run("ps aux | awk '$3 > 50 {print $2}' | xargs kill")
    assert run("kill $(ps -Ao pid,ppid | awk '$2==77{print $1}')")
    assert run("C=$(ps -Ao pid,ppid | awk '$2==77{print $1}'); kill $C") is None


def test_the_first_match_only_skips_the_count_but_not_the_protected_check():
    many = [f"/usr/bin/worker {i}" for i in range(5)]
    assert run("P=$(ps aux | grep worker | awk '{print $2}' | head -1); kill $P", many) is None
    assert run("P=$(ps aux | grep sshd | awk '{print $2}' | head -1); kill $P", ["/usr/sbin/sshd -D"])


def test_text_that_is_data_is_not_read_as_a_substitution():
    assert run("git commit -m 'run `kill -1` and $(killall node) never'") is None
    assert run("python3 - <<'EOF'\ntext = \"`kill 0` and $(killall x)\"\nEOF") is None
    assert run("echo \"`kill -1`\"")


def test_lsof_by_user_is_refused():
    assert run("lsof -t -u me | xargs kill")


def test_pidof_of_a_protected_name_is_refused():
    assert run("kill $(pidof sshd)")
    assert run("kill $(pidof worker)", ["/usr/bin/worker"]) is None


def test_max_targets_is_configurable():
    cfg = {"max_targets": 5}
    assert run("kill 11 12 13 14 15", cfg=cfg) is None
    assert run("kill 11 12 13 14 15 16", cfg=cfg)


def test_the_structural_rules_hold_without_a_config():
    assert run("kill -1", cfg={})
    assert run("kill 4242", cfg={}) is None


def test_other_tools_are_ignored():
    assert hook.check({"tool_name": "Write", "tool_input": {"command": "kill -1"}}, CFG, fake([])) is None


def test_an_unfinished_quote_fails_open():
    assert run("kill 'oops") is None


def run_main(monkeypatch, capsys, payload, config):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return hook.main(["--config", str(config)]), capsys.readouterr().err


def test_main_refuses_with_exit_2_and_the_signature(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"message": "Ask the owner."}))
    rc, err = run_main(monkeypatch, capsys, {"tool_name": "Bash", "tool_input": {"command": "kill -1"}}, cfg)
    assert rc == 2
    assert err.startswith("Blocked:")
    assert "lsof -ti" in err and "Ask the owner." in err


def test_main_lets_a_narrow_kill_through(monkeypatch, capsys, tmp_path):
    rc, err = run_main(monkeypatch, capsys, {"tool_name": "Bash", "tool_input": {"command": "kill 4242"}},
                       tmp_path / "missing.json")
    assert (rc, err) == (0, "")


def test_main_fails_open_on_garbage(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hook.main(["--config", str(tmp_path / "x.json")]) == 0


def test_the_real_process_table_is_read_for_a_throwaway_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)  # wide-kill-test-marker"])
    try:
        lines = hook.processes(["-f", "wide-kill-test-marker"])
        assert lines is not None and any("wide-kill-test-marker" in ln for ln in lines)
    finally:
        child.kill()
        child.wait()


def test_a_search_that_does_not_feed_the_kill_is_only_checked_for_protected_processes():
    many = [f"/usr/bin/worker {i}" for i in range(9)]
    assert run("kill 4242; P=$(pgrep -f worker | head -1); echo $P", many) is None
    assert run("kill 4242; echo $(pgrep -f worker)", many) is None
    assert run("kill 4242; echo $(pgrep -f sshd)", ["/usr/sbin/sshd -D"])
    assert run("P=$(pgrep -f worker); kill $P", many)
    assert run("pkill -f worker -P $(pgrep -f parent | head -1)", many) is None


def test_a_group_the_session_started_is_allowed_when_written_as_a_number():
    assert run("kill -TERM -- -4321", groups=[4321]) is None
    assert run("kill -9 -4321", groups=[4321]) is None
    assert run("kill -TERM -- -4322", groups=[4321])
    assert run("kill -TERM -- -1", groups=[1])
    assert "write the group's number" in run("kill -TERM -$pg", groups=[4321])


def test_lines_holding_the_packages_own_name_are_read_like_any_other():
    four = [f"python3 -m claude_ops.tavily {i}" for i in range(4)]
    assert "4 processes" in run("pkill -f claude_ops", four)
    assert "4 processes" in run("pkill -f tavily", four)


def test_the_process_listing_leaves_out_only_the_hook_and_its_parent(monkeypatch):
    import os
    out = f"{os.getpid()} python3 -m claude_ops.hooks.chain\n{os.getppid()} claude\n7 python3 -m claude_ops.tavily\n"
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    assert hook.processes(["-f", "x"]) == ["python3 -m claude_ops.tavily"]
