import os
import time

from claude_ops import cli
from claude_ops import instruction_history as ih


def _write(path, text, at):
    path.write_text(text)
    os.utime(path, (at, at))


GUIDE = "# Guide\n\n## Voice\n\nWrite plain sentences.\n\n## Tools\n\nUse the search tool.\n"


def test_record_keeps_one_copy_per_content_from_the_file_time(tmp_path):
    store, md = tmp_path / "store", tmp_path / "CLAUDE.md"
    _write(md, GUIDE, 1000)
    assert ih.record([md], store) == [md]
    assert ih.record([md], store) == []                 # same content: nothing new
    _write(md, GUIDE + "\nMore.\n", 2000)
    assert ih.record([md], store) == [md]
    assert len(list((store / "copies").iterdir())) == 2
    # the version a reader had depends on the file's time, not on when it was recorded
    assert ih.version_at(md, 999, store) is None
    first = ih.version_at(md, 1500, store)
    assert first and ih.copy_text(first, store) == GUIDE
    assert ih.version_at(md, 2500, store) != first
    # an unreadable file is skipped
    assert ih.record([tmp_path / "missing.md"], store) == []


def test_a_file_put_back_with_an_older_time_still_comes_after(tmp_path):
    store, md = tmp_path / "store", tmp_path / "CLAUDE.md"
    _write(md, "one\n", 2000)
    ih.record([md], store)
    _write(md, "two\n", 1000)
    ih.record([md], store)
    assert ih.copy_text(ih.version_at(md, 2000, store), store) == "two\n"


def test_a_small_change_shows_its_lines_and_heading(tmp_path):
    store, md = tmp_path / "store", tmp_path / "CLAUDE.md"
    _write(md, GUIDE, 1000)
    ih.record([md], store)
    _write(md, GUIDE.replace("Write plain sentences.", "Write short plain sentences."), 3000)
    changes = ih.changes_since([md], 2000, store)
    assert [c.path for c in changes] == [md]
    c = changes[0]
    assert c.added == ["Write short plain sentences."] and c.removed == ["Write plain sentences."]
    assert c.sections == ["## Voice"]
    lines = ih.summary(changes)
    assert lines[0].endswith('1 line added and 1 line removed under "## Voice":')
    assert lines[1:] == ["  - Write plain sentences.", "  + Write short plain sentences."]
    # nothing changed since the newer time
    ih.record([md], store)
    assert ih.changes_since([md], 3500, store) == []


def test_a_large_change_gives_headings_and_a_command_that_prints_it(tmp_path, capsys):
    store, md = tmp_path / "store", tmp_path / "CLAUDE.md"
    _write(md, GUIDE, 1000)
    ih.record([md], store)
    extra = "".join(f"- rule {i}\n" for i in range(30))
    _write(md, GUIDE + "\n## Rules\n\n" + extra, 3000)
    c, = ih.changes_since([md], 2000, store)
    line, = ih.summary([c])
    assert "33 lines added under \"## Tools\", \"## Rules\"." in line
    assert line.endswith(f"claude-ops instructions diff {md} {c.old[:ih.SHORT]}")
    # the printed command finds the copy by its short hash and prints the diff
    os.environ["CLAUDE_OPS_INSTRUCTIONS_STORE"] = str(store)
    try:
        assert ih.diff_text(md, c.old[:ih.SHORT], store).count("\n+- rule ") == 30
        assert ih.main(["diff", str(md), "deadbeefdead"]) == 2
    finally:
        del os.environ["CLAUDE_OPS_INSTRUCTIONS_STORE"]


def test_the_line_budget_is_shared_across_files(tmp_path):
    store = tmp_path / "store"
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    _write(a, "# A\n", 1000)
    _write(b, "# B\n", 1000)
    ih.record([a, b], store)
    _write(a, "# A\n" + "".join(f"a{i}\n" for i in range(15)), 3000)
    _write(b, "# B\n" + "".join(f"b{i}\n" for i in range(10)), 3000)
    lines = ih.summary(ih.changes_since([a, b], 2000, store))
    assert lines[0].startswith(f"{a}: 15 lines added") and len([x for x in lines if x.startswith("  + a")]) == 15
    assert "See them with:" in lines[-1] and str(b) in lines[-1]


def test_no_copy_from_before_and_a_removed_file_are_said_plainly(tmp_path):
    store = tmp_path / "store"
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    _write(a, "new\n", 3000)
    _write(b, "old\n", 1000)
    ih.record([b], store)
    b.unlink()
    lines = ih.summary(ih.changes_since([a, b], 2000, store))
    assert lines == [f"{a} changed, and no copy from before is kept, so read it again.",
                     f"{b} was removed."]


def test_a_heading_inside_fenced_code_is_not_a_section():
    old = "## Real\n\n```\n# comment\nx = 1\n```\n"
    new = old.replace("x = 1", "x = 2")
    added, removed, sections = ih.compare(old, new)
    assert (added, removed, sections) == (["x = 2"], ["x = 1"], ["## Real"])


def test_prune_keeps_what_a_reader_since_the_cutoff_can_ask_for(tmp_path):
    store, md = tmp_path / "store", tmp_path / "CLAUDE.md"
    for i, at in enumerate((1000, 2000, 3000)):
        _write(md, f"v{i}\n", at)
        ih.record([md], store)
    assert ih.prune(2500, store) == 1                    # v0 is older than what 2500 needs
    assert ih.copy_text(ih.version_at(md, 2500, store), store) == "v1\n"
    assert ih.copy_text(ih.version_at(md, 3500, store), store) == "v2\n"
    assert ih.prune(time.time(), store) == 1
    assert ih.copy_text(ih.version_at(md, time.time(), store), store) == "v2\n"


def test_the_cli_names_the_tool():
    assert cli.TOOLS["instructions"][0] == "instruction_history"
