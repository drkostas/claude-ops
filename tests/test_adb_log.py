"""adb_log: the adb server's log is cut over a size while the server runs, its end kept in <log>.1."""
from __future__ import annotations

import json
import os

import pytest

from claude_ops import adb_log, cli


def lines(n: int, start: int = 0) -> bytes:
    return b"".join(b"11-15 07:41:%02d.000 transport localhost:15555: read failed %06d\n" % (i % 60, i)
                    for i in range(start, start + n))


def test_server_log_follows_adbs_own_rule(tmp_path):
    assert adb_log.server_log({"ANDROID_ADB_LOG_PATH": str(tmp_path / "x.log"), "TMPDIR": "/t"}) == tmp_path / "x.log"
    if os.name != "nt":
        assert adb_log.server_log({"TMPDIR": str(tmp_path)}) == tmp_path / f"adb.{os.getuid()}.log"
        assert adb_log.server_log({}) == adb_log.Path("/tmp") / f"adb.{os.getuid()}.log"


def test_a_log_under_the_size_or_none_is_left_alone(tmp_path):
    log = tmp_path / "adb.log"
    assert adb_log.cap(log, max_bytes=100) == adb_log.Capped(str(log), 0, False)
    log.write_bytes(lines(2))
    r = adb_log.cap(log, max_bytes=10_000, keep_bytes=10)
    assert not r.cut and r.bytes == len(lines(2)) and log.read_bytes() == lines(2)
    assert not (tmp_path / "adb.log.1").exists()


def test_a_log_over_the_size_keeps_its_end_from_a_line_start_and_is_emptied(tmp_path):
    log = tmp_path / "adb.log"
    data = lines(1000)
    log.write_bytes(data)
    (tmp_path / "adb.log.1").write_bytes(b"an older cut\n")
    r = adb_log.cap(log, max_bytes=10_000, keep_bytes=2_000)
    kept = (tmp_path / "adb.log.1").read_bytes()
    assert r.cut and r.bytes == len(data) and r.kept == str(tmp_path / "adb.log.1") and r.kept_bytes == len(kept)
    assert data.endswith(kept) and 0 < len(kept) <= 2_000 and kept.startswith(b"11-15 ")
    assert log.read_bytes() == b"" and not (tmp_path / "adb.log.1.tmp").exists()


def test_a_server_holding_the_log_open_for_appending_writes_from_the_top_after_the_cut(tmp_path):
    """adb's server opens its log with O_WRONLY | O_CREAT | O_APPEND and keeps it open. After the cut its next
    write lands at the new end, so the log holds only that line, with no hole where the old bytes were."""
    log = tmp_path / "adb.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        os.write(fd, lines(500))
        assert adb_log.cap(log, max_bytes=1_000, keep_bytes=500).cut
        os.write(fd, lines(1, start=500))
    finally:
        os.close(fd)
    assert log.read_bytes() == lines(1, start=500) and log.stat().st_size == len(lines(1))


def test_the_command_cuts_and_says_so(tmp_path, capsys):
    log = tmp_path / "adb.log"
    log.write_bytes(lines(5000))
    assert cli.main(["adb-log", "--path", str(log), "--max", "0.1", "--keep", "0.01", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cut"] and out["path"] == str(log) and out["kept"] == str(log) + ".1" and log.stat().st_size == 0
    assert cli.main(["adb-log", "--path", str(log), "--max", "0.1"]) == 0
    assert "left as it is" in capsys.readouterr().out


@pytest.mark.parametrize("keep", [0, 7])
def test_a_tail_shorter_than_one_line_keeps_nothing(tmp_path, keep):
    log = tmp_path / "adb.log"
    log.write_bytes(lines(50))
    adb_log.cap(log, max_bytes=10, keep_bytes=keep)
    assert (tmp_path / "adb.log.1").read_bytes() == b"" and log.read_bytes() == b""
