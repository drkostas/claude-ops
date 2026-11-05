"""The shared pre-kill check. Only throwaway children started by the test are ever signalled."""
import signal
import subprocess
import sys

import pytest

from claude_ops import killguard


@pytest.mark.parametrize("targets", [[1], [0], [-1], [-4242], [True], ["12"], [11, 12, 13, 14], [5, 1]])
def test_refused(targets):
    assert killguard.check(targets)
    with pytest.raises(killguard.Refused):
        killguard.require(targets)


@pytest.mark.parametrize("targets", [[], [4242], [11, 12, 13]])
def test_allowed(targets):
    assert killguard.check(targets) is None


def test_a_group_the_caller_started_is_allowed():
    assert killguard.check([-4242], own_groups=[4242]) is None
    assert killguard.check([-4243], own_groups=[4242])


def test_pid_one_and_everything_stay_refused_whatever_the_caller_owns():
    assert killguard.check([-1], own_groups=[1])
    assert killguard.check([1], own_groups=[1])


def test_the_limit_is_a_parameter():
    assert killguard.check([11, 12, 13, 14], max_targets=4) is None
    assert killguard.check([11, 12], max_targets=1)


def test_kill_checks_before_sending():
    sent = []
    with pytest.raises(killguard.Refused):
        killguard.kill([4242, 1], send=lambda p, s: sent.append(p))
    assert sent == []
    killguard.kill([4242, 4243], signal.SIGTERM, send=lambda p, s: sent.append((p, s)))
    assert sent == [(4242, signal.SIGTERM), (4243, signal.SIGTERM)]


def test_kill_ends_a_throwaway_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    killguard.kill([child.pid])
    assert child.wait(timeout=10) == -signal.SIGTERM


def test_command_line_exit_codes(capsys):
    assert killguard.main(["4242", "4243"]) == 0
    assert killguard.main(["-1"]) == 2
    assert capsys.readouterr().err.startswith("Blocked:")
    assert killguard.main(["--own-group", "7", "--", "-7"]) == 0
    assert killguard.main(["--max", "1", "11", "12"]) == 2
    assert killguard.main(["abc"]) == 2


TABLE = {                         # pid: (ppid, pgid, command)
    10: (1, 10, "/usr/bin/claude"),
    11: (10, 10, "python3 -m claude_ops.hooks.chain"),
    20: (10, 20, "zsh -c run_tests.sh"),
    21: (20, 20, "pytest"),
    22: (21, 22, "sleep 100"),
    30: (1, 30, "/usr/bin/claude"),
    31: (30, 31, "zsh -c other"),
    40: (1, 40, "sshd"),
}


def test_the_session_root_is_the_nearest_claude_ancestor():
    assert killguard.session_root(TABLE, start=11) == 10
    assert killguard.session_root(TABLE, start=40) is None


def test_started_groups_are_those_of_descendants_of_the_session():
    assert killguard.started_groups(TABLE, root=10) == {20, 22}
    assert killguard.started_groups(TABLE, root=30) == {31}


def test_started_groups_without_a_session_is_empty():
    assert killguard.started_groups({40: (1, 40, "sshd")}) == set()
