import os

from claude_ops import restore


def make(path, text, mtime=1_500_000_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def test_only_missing_chats_come_back_with_their_time(tmp_path):
    arc, live = tmp_path / "archive", tmp_path / "projects"
    make(arc / "-a" / "one.jsonl", "old copy")
    make(arc / "-a" / "two.jsonl", "archived")
    make(live / "-a" / "one.jsonl", "newer live chat", 1_600_000_000)
    r = restore.restore(arc, live)
    assert r["restored"] == [str(live / "-a" / "two.jsonl")]
    assert (live / "-a" / "one.jsonl").read_text() == "newer live chat"
    assert os.stat(live / "-a" / "two.jsonl").st_mtime == 1_500_000_000
    assert restore.restore(arc, live)["missing"] == 0
    assert not list(live.glob("*/*.restore.*"))


def test_mass_restore_is_refused_and_dry_run_writes_nothing(tmp_path):
    arc, live = tmp_path / "archive", tmp_path / "projects"
    for i in range(5):
        make(arc / "-a" / f"{i}.jsonl", "x")
    assert restore.restore(arc, live, max_missing=3)["result"] == "refused"
    r = restore.restore(arc, live, dry_run=True)
    assert r["result"] == "dry-run" and len(r["restored"]) == 5 and not live.exists()
