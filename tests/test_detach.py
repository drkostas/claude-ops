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


def test_the_job_lives_on_after_its_parent_is_ended_and_writes_its_end_line(tmp_path):
    log = tmp_path / "job.log"
    starter = tmp_path / "starter.py"
    starter.write_text(
        "import sys\n"
        "from claude_ops import detach\n"
        f"print(detach.detach([sys.executable, '-c', 'import time; time.sleep(1.5); print(\\'finished\\')'], {str(log)!r}),"
        " flush=True)\n"
        "import time; time.sleep(30)\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    parent = subprocess.Popen([sys.executable, str(starter)], stdout=subprocess.PIPE, text=True,
                              env=env, start_new_session=True)
    child = int(parent.stdout.readline())
    os.killpg(parent.pid, signal.SIGTERM)
    assert parent.wait(timeout=10) == -signal.SIGTERM
    assert _alive(child)
    assert _wait_for(log, detach.END)
    text = log.read_text()
    assert "finished" in text
    end = [ln for ln in text.splitlines() if ln.startswith(detach.END)]
    assert len(end) == 1 and end[0].startswith(f"{detach.END} exit 0 at ") and f"(pid {child})" in end[0]


def test_a_job_ended_by_a_signal_says_which(tmp_path):
    log = tmp_path / "job.log"
    pid = detach.detach([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(30)"],
                        str(log))
    assert _wait_for(log, "up")
    os.killpg(pid, signal.SIGTERM)
    assert _wait_for(log, detach.END)
    end = [ln for ln in log.read_text().splitlines() if ln.startswith(detach.END)]
    assert len(end) == 1 and end[0].startswith(f"{detach.END} signal SIGTERM (15) at ")


def test_a_failing_job_gives_its_exit_code_on_a_line_of_its_own(tmp_path):
    log = tmp_path / "job.log"
    detach.detach([sys.executable, "-c", "import sys; sys.stdout.write('no newline'); sys.exit(3)"],
                  str(log))
    assert _wait_for(log, detach.END)
    lines = log.read_text().splitlines()
    assert lines[0] == "no newline" and lines[1].startswith(f"{detach.END} exit 3 at ")


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


def test_the_started_group_is_recorded_for_the_session(monkeypatch, tmp_path):
    from claude_ops import killguard
    seen = []
    monkeypatch.setattr(killguard, "record_group", lambda group, *a, **k: seen.append(group) or True)
    pid = detach.detach([sys.executable, "-c", "pass"], str(tmp_path / "job.log"))
    assert seen == [pid]


def test_a_failure_to_record_never_stops_the_command(monkeypatch, tmp_path):
    from claude_ops import killguard

    def boom(*a, **k):
        raise RuntimeError("no state folder")
    monkeypatch.setattr(killguard, "record_group", boom)
    log = tmp_path / "job.log"
    detach.detach([sys.executable, "-c", "print('ran')"], str(log))
    assert _wait_for(log, "ran")


def test_ps_shows_a_short_command_line(tmp_path):
    log = tmp_path / "job.log"
    pid = detach.detach([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(30)"],
                        str(log))
    try:
        assert _wait_for(log, "up")
        line = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                              text=True).stdout.strip()
        assert line.endswith(f"-I {detach.SUPERVISOR} {log} {sys.executable} -c "
                             "import time; print('up', flush=True); time.sleep(30)")
        assert "\n" not in line and "import os" not in line
        assert os.getpgid(pid) == pid
    finally:
        os.killpg(pid, signal.SIGTERM)
    assert _wait_for(log, f"{detach.END} signal SIGTERM (15)")


def test_the_supervisor_imports_nothing_from_the_jobs_folder(tmp_path):
    # a module in the job's folder named like one the supervisor imports would run there first
    (tmp_path / "signal.py").write_text("open('shadowed', 'w').write('x')\nraise SystemExit(9)\n")
    log = tmp_path / "job.log"
    detach.detach([sys.executable, "-c", "print('ran')"], str(log), cwd=str(tmp_path),
                  env={"PATH": os.environ["PATH"], "PYTHONPATH": str(tmp_path)})
    assert _wait_for(log, detach.END)
    assert not (tmp_path / "shadowed").exists()
    assert f"{detach.END} exit 0 at " in log.read_text()


def test_the_supervisor_file_is_in_the_package_and_imports_only_the_standard_library():
    assert os.path.isfile(detach.SUPERVISOR)
    import ast
    tree = ast.parse(open(detach.SUPERVISOR).read())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= {"os", "signal", "subprocess", "sys", "time"}
