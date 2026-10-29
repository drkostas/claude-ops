"""sqlite_health: an SQLite database inside a container reads as intact, damaged, or not asked."""
from __future__ import annotations

import sqlite3
import subprocess
import sys

from claude_ops import sqlite_health as sh


class Engine:
    """A stand-in for `docker`: answers `exec` from a table of programs and `logs` from a text."""

    def __init__(self, programs=None, logs="", missing=False, logs_rc=0):
        self.programs = programs or {}
        self.logs, self.missing, self.logs_rc = logs, missing, logs_rc
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(cmd)
        if self.missing:
            return subprocess.CompletedProcess(cmd, 1, "", "Error response from daemon: No such container: box\n")
        if cmd[1] == "logs":
            return subprocess.CompletedProcess(cmd, self.logs_rc, self.logs, "")
        program = cmd[3]
        if program not in self.programs:
            return subprocess.CompletedProcess(
                cmd, 127, "", f'OCI runtime exec failed: exec: "{program}": executable file not found in $PATH\n')
        rc, out, err = self.programs[program]
        return subprocess.CompletedProcess(cmd, rc, out, err)


def test_ok_is_intact_and_sqlite3_is_tried_first():
    eng = Engine({"sqlite3": (0, "ok\n", ""), "python3": (0, "should not be asked\n", "")})
    assert sh.quick_check_in_container("box", "/data/app.db", runner=eng) == {"intact": True, "detail": "ok"}
    assert [c[3] for c in eng.calls] == ["sqlite3"]


def test_an_image_without_sqlite3_is_checked_with_python3():
    eng = Engine({"python3": (0, "ok\n", "")})
    assert sh.quick_check_in_container("box", "/data/app.db", runner=eng)["intact"] is True
    assert [c[3] for c in eng.calls] == ["sqlite3", "python3"]
    assert eng.calls[-1][-1] == "/data/app.db"


def test_the_problems_quick_check_lists_are_damage():
    report = "*** in database main ***\nTree 78 page 1639 cell 401: Rowid 147719 out of order\nPage 229: never used\n"
    got = sh.quick_check_in_container("box", "/data/app.db", runner=Engine({"sqlite3": (0, report, "")}))
    assert got["intact"] is False and "Rowid 147719 out of order" in got["detail"]


def test_a_file_sqlite_refuses_to_open_is_damage():
    eng = Engine({"python3": (1, "", "sqlite3.DatabaseError: file is not a database\n")})
    got = sh.quick_check_in_container("box", "/data/app.db", runner=eng)
    assert got["intact"] is False and "file is not a database" in got["detail"]


def test_nothing_asked_is_none_never_a_pass():
    assert sh.quick_check_in_container("box", "/x.db", runner=Engine(missing=True))["intact"] is None
    neither = sh.quick_check_in_container("box", "/x.db", runner=Engine({}))
    assert neither["intact"] is None and "neither" in neither["detail"]
    wrong_path = Engine({"python3": (1, "", "sqlite3.OperationalError: unable to open database file\n")})
    assert sh.quick_check_in_container("box", "/x.db", runner=wrong_path)["intact"] is None

    def cannot_run(cmd, **_kw):
        raise FileNotFoundError("docker")
    assert sh.quick_check_in_container("box", "/x.db", runner=cannot_run)["intact"] is None


def test_the_python_check_reads_a_real_database(tmp_path):
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as c:
        c.execute("create table t (x)")
        c.execute("insert into t values (1)")
    out = subprocess.run([sys.executable, "-c", sh._PY_CHECK, str(db)], capture_output=True, text=True)
    assert out.returncode == 0 and sh.read_quick_check(out.stdout) == {"intact": True, "detail": "ok"}


def test_log_lines_with_damage_messages_are_counted():
    logs = ("12:00 WARNING history recording failed: (sqlite3.DatabaseError) database disk image is malformed\n"
            "12:00 INFO printer connected\n"
            "12:01 ERROR (sqlite3.OperationalError) disk I/O error\n")
    assert sh.log_error_lines("box", "10m", runner=Engine(logs=logs)) == 2
    assert sh.log_error_lines("box", "10m", runner=Engine(logs="all quiet\n")) == 0
    assert sh.log_error_lines("box", "10m", runner=Engine(logs_rc=1)) is None


def test_the_command_exits_by_what_it_found(capsys):
    assert sh.main(["box", "/d.db"], runner=Engine({"sqlite3": (0, "ok\n", "")})) == 0
    assert sh.main(["box", "/d.db"], runner=Engine({"sqlite3": (0, "Page 9: never used\n", "")})) == 1
    assert sh.main(["box", "/d.db"], runner=Engine(missing=True)) == 2
    damaged_log = Engine({"sqlite3": (0, "ok\n", "")}, logs="database disk image is malformed\n")
    assert sh.main(["box", "/d.db", "--since", "10m", "--json"], runner=damaged_log) == 1
    assert '"error_lines": 1' in capsys.readouterr().out
