"""log_cap: a log opened for appending is cut over a size while its writer runs, its end kept in <log>.1."""
from __future__ import annotations

import json
import os

import pytest

from claude_ops import cli, log_cap


def lines(n: int, start: int = 0) -> bytes:
    return b"".join(b"2026-01-02T03:04:%02d worker: job %06d done\n" % (i % 60, i) for i in range(start, start + n))


def test_a_log_under_the_size_or_none_is_left_alone(tmp_path):
    log = tmp_path / "app.log"
    assert log_cap.cap(log, max_bytes=100) == log_cap.Capped(str(log), 0, False)
    log.write_bytes(lines(2))
    r = log_cap.cap(str(log), max_bytes=10_000, keep_bytes=10)
    assert not r.cut and r.bytes == len(lines(2)) and log.read_bytes() == lines(2)
    assert not (tmp_path / "app.log.1").exists()


def test_a_log_over_the_size_keeps_its_end_from_a_line_start_and_is_emptied(tmp_path):
    log = tmp_path / "app.log"
    data = lines(1000)
    log.write_bytes(data)
    (tmp_path / "app.log.1").write_bytes(b"an older cut\n")
    r = log_cap.cap(log, max_bytes=10_000, keep_bytes=2_000)
    kept = (tmp_path / "app.log.1").read_bytes()
    assert r.cut and r.bytes == len(data) and r.kept == str(tmp_path / "app.log.1") and r.kept_bytes == len(kept)
    assert data.endswith(kept) and 0 < len(kept) <= 2_000 and kept.startswith(b"2026-01-02T")
    assert log.read_bytes() == b"" and not (tmp_path / "app.log.1.tmp").exists()


def test_a_writer_holding_the_log_open_for_appending_writes_from_the_top_after_the_cut(tmp_path):
    """A writer that opened its log with O_WRONLY | O_CREAT | O_APPEND and keeps it open writes its next line at
    the new end after the cut, so the log holds only that line, with no hole where the old bytes were."""
    log = tmp_path / "app.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        os.write(fd, lines(500))
        assert log_cap.cap(log, max_bytes=1_000, keep_bytes=500).cut
        os.write(fd, lines(1, start=500))
    finally:
        os.close(fd)
    assert log.read_bytes() == lines(1, start=500) and log.stat().st_size == len(lines(1))


@pytest.mark.parametrize("keep", [0, 7])
def test_a_tail_shorter_than_one_line_keeps_nothing(tmp_path, keep):
    log = tmp_path / "app.log"
    log.write_bytes(lines(50))
    log_cap.cap(log, max_bytes=10, keep_bytes=keep)
    assert (tmp_path / "app.log.1").read_bytes() == b"" and log.read_bytes() == b""


def test_tail_of_a_file_smaller_than_the_keep_is_the_whole_file(tmp_path):
    f = tmp_path / "small.log"
    f.write_bytes(lines(3))
    assert log_cap.tail(f, 10_000) == lines(3)


def test_the_command_cuts_each_file_over_the_size_and_says_so(tmp_path, capsys):
    big, small, missing = tmp_path / "big.log", tmp_path / "small.log", tmp_path / "missing.log"
    big.write_bytes(lines(5000))
    small.write_bytes(lines(2))
    assert cli.main(["log-cap", str(big), str(small), str(missing), "--max", "0.1", "--keep", "0.01", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["path"] for r in out] == [str(big), str(small), str(missing)]
    assert [r["cut"] for r in out] == [True, False, False] and out[0]["kept"] == str(big) + ".1"
    assert big.stat().st_size == 0 and small.read_bytes() == lines(2) and not missing.exists()
    assert cli.main(["log-cap", str(small), "--max", "0.1"]) == 0
    assert capsys.readouterr().out == f"{small}: {len(lines(2))} bytes, at most {int(0.1 * 2**20)}, left as it is\n"


def test_the_command_needs_a_file(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["log-cap"])
    assert e.value.code == 2
