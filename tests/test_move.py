"""Moving a conversation's files: every copy is checked before any original leaves, and the
originals are held, never deleted."""
import shutil
from pathlib import Path

import pytest

from claude_ops import move

SID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def world(tmp_path):
    proj, arch, hold = tmp_path / "projects", tmp_path / "archive", tmp_path / "hold"
    src, dst = proj / "-old", proj / "-new"
    (src / SID / "subagents").mkdir(parents=True)
    (src / f"{SID}.jsonl").write_text('{"type":"user"}\n' * 50)
    (src / SID / "subagents" / "a.jsonl").write_text("x")
    (arch / "-old" / SID).mkdir(parents=True)
    (arch / "-old" / f"{SID}.jsonl").write_text("archived")
    (arch / "-old" / f"{SID}-summary.txt").write_text("summary")
    (arch / "-old" / SID / "s.jsonl").write_text("y")
    return src, dst, arch, hold


def test_the_conversation_and_the_archive_move_and_the_originals_are_held(world):
    src, dst, arch, hold = world
    move.move_files(SID, src, dst, arch, hold / "one")
    assert (dst / f"{SID}.jsonl").read_text() == '{"type":"user"}\n' * 50
    assert (dst / SID / "subagents" / "a.jsonl").exists()
    assert all((arch / "-new" / f).exists() for f in (f"{SID}.jsonl", f"{SID}-summary.txt", SID))
    assert not (src / f"{SID}.jsonl").exists() and not (arch / "-old" / f"{SID}.jsonl").exists()
    assert (hold / "one" / "projects" / f"{SID}.jsonl").exists()
    assert (hold / "one" / "archive" / f"{SID}-summary.txt").exists()


def test_moving_into_the_same_folder_moves_nothing(world):
    src, _dst, arch, hold = world
    assert move.move_files(SID, src, src, arch, hold / "two")["moved"] == []


def test_a_missing_transcript_is_an_error(world):
    src, dst, arch, hold = world
    with pytest.raises(FileNotFoundError):
        move.move_files("nope", src, dst, arch, hold)


def test_a_copy_that_fails_its_checksum_leaves_the_original_in_place(world, monkeypatch):
    src, dst, arch, hold = world
    sid2 = "22222222-2222-4333-8444-555555555555"
    (src / f"{sid2}.jsonl").write_text("real")
    monkeypatch.setattr(shutil, "copy2", lambda a, b, **kw: Path(b).write_text("corrupt"))
    with pytest.raises(OSError):
        move.move_files(sid2, src, dst, arch, hold / "three")
    assert (src / f"{sid2}.jsonl").read_text() == "real" and not (hold / "three").exists()
