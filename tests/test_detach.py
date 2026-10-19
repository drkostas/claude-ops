"""detach starts a command in its own session that outlives its parent's process group."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from claude_ops import cli, detach


def _wait_for(path, text, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if os.path.exists(path) and text in open(path).read():
            return True
        time.sleep(0.05)
    return False


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_command_leads_its_own_session_and_logs(tmp_path):
    log = tmp_path / "logs" / "job.log"
    pid = detach.detach([sys.executable, "-c", "import os; print('sid', os.getsid(0)); print('done')"],
                        str(log))
    assert _wait_for(log, "done")
    assert f"sid {pid}" in log.read_text()


def test_the_log_is_appended_to(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("before\n")
    detach.detach([sys.executable, "-c", "print('after')"], str(log))
    assert _wait_for(log, "after")
    assert log.read_text().startswith("before\n")


def test_it_outlives_a_kill_of_the_starters_group(tmp_path):
    log = tmp_path / "job.log"
    starter = tmp_path / "starter.py"
    starter.write_text(
        "import sys\n"
        "from claude_ops import detach\n"
        f"print(detach.detach([sys.executable, '-c', 'import time; time.sleep(30)'], {str(log)!r}),"
        " flush=True)\n"
        "import time; time.sleep(30)\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    parent = subprocess.Popen([sys.executable, str(starter)], stdout=subprocess.PIPE, text=True,
                              env=env, start_new_session=True)
    child = int(parent.stdout.readline())
    try:
        os.killpg(parent.pid, signal.SIGTERM)
        parent.wait(timeout=10)
        time.sleep(0.2)
        assert _alive(child)
    finally:
        try:
            os.killpg(child, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_cwd_and_env(tmp_path):
    log = tmp_path / "job.log"
    detach.detach([sys.executable, "-c", "import os; print(os.getcwd(), os.environ.get('X'))"],
                  str(log), cwd=str(tmp_path), env={"X": "set", "PATH": os.environ["PATH"]})
    assert _wait_for(log, "set")
    assert os.path.realpath(str(tmp_path)) in log.read_text()


def test_an_empty_command_or_a_missing_program_starts_nothing(tmp_path):
    with pytest.raises(ValueError):
        detach.detach([], str(tmp_path / "a.log"))
    with pytest.raises(FileNotFoundError):
        detach.detach([str(tmp_path / "no-such-program")], str(tmp_path / "b.log"))


def test_the_command_line(tmp_path, capsys):
    log = tmp_path / "job.log"
    assert cli.main(["detach", "--log", str(log), "--", sys.executable, "-c", "print('cli')"]) == 0
    pid = int(capsys.readouterr().out.strip())
    assert pid > 0 and _wait_for(log, "cli")
    assert cli.main(["detach", "--log", str(log)]) == 2
