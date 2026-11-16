"""memory_notes: large notes found by size and time alone, with the index line that links each."""
from __future__ import annotations

import os

from claude_ops import memory_notes as mn

NOW = 1_800_000_000.0
DAY = 86_400


def note(folder, name, size, age_s, text=None):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(text if text is not None else "x" * size)
    os.utime(p, (NOW - age_s, NOW - age_s))
    return p


def test_a_large_note_written_today_is_found(tmp_path):
    mem = tmp_path / "-home-a-proj" / "memory"
    note(mem, "build-state-2026-11-15.md", 70_000, 3600)
    got = mn.large_notes(tmp_path, now=NOW)
    assert [(n.path.name, n.size, n.label) for n in got] == [("build-state-2026-11-15.md", 70_000, None)]
    assert got[0].folder == mem and got[0].written_at == NOW - 3600


def test_small_or_old_notes_are_not(tmp_path):
    mem = tmp_path / "-home-a-proj" / "memory"
    note(mem, "small.md", 60_000, 60)
    note(mem, "old.md", 200_000, DAY + 60)
    note(mem, "not-markdown.txt", 200_000, 60)
    assert mn.large_notes(tmp_path, now=NOW) == []


def test_largest_first_across_folders(tmp_path):
    note(tmp_path / "-a" / "memory", "a.md", 61_000, 10)
    note(tmp_path / "-b" / "memory", "b.md", 90_000, 10)
    assert [n.path.name for n in mn.large_notes(tmp_path, now=NOW)] == ["b.md", "a.md"]


def test_the_index_line_names_the_note(tmp_path):
    mem = tmp_path / "-p" / "memory"
    note(mem, "build-state-2026-11-15.md", 80_000, 10)
    note(mem, "notes-index.md", 0, 10, "- [builder state, 15 Nov](build-state-2026-11-15.md) read it first\n")
    got = mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW)
    assert [(n.path.name, n.label) for n in got] == [("build-state-2026-11-15.md", "builder state, 15 Nov")]


def test_a_rolled_note_is_left_out(tmp_path):
    mem = tmp_path / "-p" / "memory"
    # rolled into a new dated note: the index links the new one, the old one keeps its history
    note(mem, "build-state-2026-11-01-night.md", 150_000, 600)
    note(mem, "build-state-2026-11-15.md", 4_000, 60)
    # rolled into a note of another name: the index names the old file in its line
    note(mem, "calendar-work-2026-11-03.md", 170_000, 600)
    note(mem, "stats-state-2026-11-15.md", 3_000, 60)
    # never linked, never named, no linked note of its name: still being written by someone
    note(mem, "loose-notes.md", 65_000, 600)
    note(mem, "notes-index.md", 0, 30,
         "- [builder state](build-state-2026-11-15.md) read it\n"
         "- [stats state](stats-state-2026-11-15.md) older history in calendar-work-2026-11-03.md\n")
    # both successors were created 60 s ago, after the old notes' last writes
    got = mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW, birth=lambda p: NOW - 60)
    assert [(n.path.name, n.label) for n in got] == [("loose-notes.md", None)]


def rolled(tmp_path, old_age_s):
    mem = tmp_path / "-p" / "memory"
    note(mem, "build-state-2026-11-01-night.md", 150_000, old_age_s)
    note(mem, "build-state-2026-11-15.md", 4_000, 30)
    note(mem, "calendar-work-2026-11-03.md", 170_000, old_age_s)
    note(mem, "stats-state-2026-11-15.md", 3_000, 30)
    note(mem, "notes-index.md", 0, 30,
         "- [builder state](build-state-2026-11-15.md) read it\n"
         "- [stats state](stats-state-2026-11-15.md) older history in calendar-work-2026-11-03.md\n")
    born = {"build-state-2026-11-15.md": NOW - 120, "stats-state-2026-11-15.md": NOW - 120}
    return lambda p: born.get(p.name)


def test_a_rolled_note_still_written_is_reported_with_its_successors_label(tmp_path):
    # written 10 s ago, after its successor was created 120 s ago: a session still appends to it
    birth = rolled(tmp_path, 10)
    got = mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW, birth=birth)
    assert sorted((n.path.name, n.label) for n in got) == [
        ("build-state-2026-11-01-night.md", "builder state"),
        ("calendar-work-2026-11-03.md", "stats state")]


def test_a_rolled_note_written_before_its_successor_is_left_out(tmp_path):
    birth = rolled(tmp_path, 600)
    assert mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW, birth=birth) == []


def test_without_a_creation_time_a_rolled_note_is_left_out(tmp_path):
    rolled(tmp_path, 10)
    assert mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW, birth=lambda p: None) == []


def test_a_name_counts_only_whole():
    assert mn.names("older history in calendar-work-2026-11-03.md.", "calendar-work-2026-11-03.md")
    assert mn.names("see a.md, and b.md", "a.md")
    assert not mn.names("see data.md", "a.md")
    assert not mn.names("see a.md.bak and xa.md", "a.md")
    assert not mn.names("see a.mdx", "a.md")


def test_a_note_named_inside_a_longer_name_is_not_rolled(tmp_path):
    mem = tmp_path / "-p" / "memory"
    note(mem, "a.md", 70_000, 10)
    note(mem, "data-2026-11-15.md", 1_000, 30)
    note(mem, "notes-index.md", 0, 30, "- [data state](data-2026-11-15.md) built from olddata.md\n")
    got = mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW, birth=lambda p: NOW - 60)
    assert [(n.path.name, n.label) for n in got] == [("a.md", None)]


def test_without_an_index_every_large_note_counts(tmp_path):
    mem = tmp_path / "-p" / "memory"
    note(mem, "build-state-2026-11-01-night.md", 150_000, 600)
    note(mem, "build-state-2026-11-15.md", 61_000, 60)
    got = mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW)
    assert sorted(n.path.name for n in got) == ["build-state-2026-11-01-night.md", "build-state-2026-11-15.md"]


def test_a_large_linked_note_still_counts(tmp_path):
    mem = tmp_path / "-p" / "memory"
    note(mem, "build-state-2026-11-01.md", 300_000, 60)
    note(mem, "notes-index.md", 0, 30, "- [builder state](build-state-2026-11-01.md)\n")
    got = mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW)
    assert [(n.path.name, n.label) for n in got] == [("build-state-2026-11-01.md", "builder state")]


def test_stem_drops_the_date_and_what_follows():
    assert mn.stem("build-state-2026-11-01-night.md") == "build-state"
    assert mn.stem("loose-notes.md") == "loose-notes"


def test_a_missing_projects_folder_gives_nothing(tmp_path):
    assert mn.large_notes(tmp_path / "absent", now=NOW) == []


def history_linked(tmp_path, old_age_s):
    """A note rolled into a new one whose index line still links it, after the new note's link."""
    mem = tmp_path / "-p" / "memory"
    note(mem, "build-state-2026-11-15c.md", 61_000, old_age_s)
    note(mem, "build-state-2026-11-16.md", 4_000, 30)
    note(mem, "notes-index.md", 0, 30,
         "- [builder state, 16 Nov](build-state-2026-11-16.md) read it first, "
         "older history in [part c](build-state-2026-11-15c.md) and part b\n")
    return lambda p: {"build-state-2026-11-16.md": NOW - 120}.get(p.name)


def test_a_note_linked_after_a_lines_first_link_is_rolled(tmp_path):
    birth = history_linked(tmp_path, 600)
    assert mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW, birth=birth) == []


def test_a_history_linked_note_still_written_takes_the_first_links_label(tmp_path):
    birth = history_linked(tmp_path, 10)
    got = mn.large_notes(tmp_path, index_name="notes-index.md", now=NOW, birth=birth)
    assert [(n.path.name, n.label) for n in got] == [("build-state-2026-11-15c.md", "builder state, 16 Nov")]


def test_the_first_link_of_a_line_is_current_and_later_ones_are_history():
    index = mn.Index({"new.md": "state", "old.md": "part c"},
                     [("- [state](new.md), older in [part c](old.md)", ["new.md", "old.md"])])
    assert mn.successor("new.md", index) is None
    assert mn.successor("old.md", index) == "new.md"


def test_a_folder_reached_through_links_reports_a_note_once(tmp_path):
    projects = tmp_path / "projects"
    mem = projects / "-home-a-proj" / "memory"
    note(mem, "build-state-2026-11-15.md", 70_000, 60)
    (projects / "-home-a-proj-worktree").mkdir()
    (projects / "-home-a-proj-worktree" / "memory").symlink_to(mem)
    (projects / "-home-a-proj-copy").symlink_to(projects / "-home-a-proj")
    got = mn.large_notes(projects, now=NOW)
    assert [(n.path.name, n.folder) for n in got] == [("build-state-2026-11-15.md", mem)]


def test_a_shared_folder_outside_the_projects_is_reported_once_under_its_first_link(tmp_path):
    shared = tmp_path / "shared-memory"
    note(shared, "build-state-2026-11-15.md", 70_000, 60)
    projects = tmp_path / "projects"
    for name in ("-b", "-a"):
        (projects / name).mkdir(parents=True)
        (projects / name / "memory").symlink_to(shared)
    got = mn.large_notes(projects, now=NOW)
    assert [(n.path.name, n.folder) for n in got] == [("build-state-2026-11-15.md", projects / "-a" / "memory")]


def test_memory_folders_lists_each_real_folder_once(tmp_path):
    (tmp_path / "-a" / "memory").mkdir(parents=True)
    (tmp_path / "-b" / "memory").mkdir(parents=True)
    (tmp_path / "-c").mkdir()
    (tmp_path / "-c" / "memory").symlink_to(tmp_path / "-a" / "memory")
    (tmp_path / "-d").mkdir()                     # a project with no memory folder
    assert mn.memory_folders(tmp_path) == [tmp_path / "-a" / "memory", tmp_path / "-b" / "memory"]
