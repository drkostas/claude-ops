import os

from claude_ops import filemap

RULES = [{"when": ["id", "identity", "passport"], "folders": ["identity"], "names": ["card", "passport"]},
         {"when": ["bank", "routing", "account"], "folders": ["bank statements"], "names": ["statement"]}]


def make(tmp_path, files):
    for rel, size in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return tmp_path


FILES = {"Personal/Identity/National ID Card (2025).pdf": 10, "Personal/Identity/National ID Card (2025).docx": 10,
         "Personal/Identity/IMG_0001.dng": 90, "Personal/Identity/Old ID Card (2015).pdf": 10,
         "Personal/Finance/Bank Statements/Statement 2025-10.pdf": 5, "Personal/Finance/Bank Statements/Statement 2025-09.pdf": 5,
         "Personal/Housing/Lease Agreement (2025).pdf": 7, "Work/Notes.txt": 1}


def test_walk_lists_files_with_sizes_and_skips(tmp_path):
    make(tmp_path, FILES)
    (tmp_path / "Work" / ".DS_Store").write_bytes(b"x")
    (tmp_path / "Work" / "Cache").mkdir()
    (tmp_path / "Work" / "Cache" / "a.pdf").write_bytes(b"x")
    rows = filemap.walk(tmp_path, skip=("Cache",))
    paths = [r["path"] for r in rows]
    assert "Work/Notes.txt" in paths and "Work/Cache/a.pdf" not in paths and "Work/.DS_Store" not in paths
    dng = next(r for r in rows if r["ext"] == "dng")
    assert dng["size"] == 90 and dng["path"].startswith("Personal/Identity/")
    assert len(rows) == len(FILES)


def test_walk_ignores_links_and_a_missing_folder(tmp_path):
    make(tmp_path, {"a/f.pdf": 1})
    os.symlink(tmp_path / "a", tmp_path / "link")
    assert [r["path"] for r in filemap.walk(tmp_path)] == ["a/f.pdf"]
    assert filemap.walk(tmp_path / "missing") == []


def test_signature_changes_when_a_file_is_added_or_edited(tmp_path):
    make(tmp_path, {"a/f.pdf": 1})
    before = filemap.signature(filemap.walk(tmp_path))
    assert before == filemap.signature(filemap.walk(tmp_path))
    make(tmp_path, {"a/g.pdf": 2})
    assert filemap.signature(filemap.walk(tmp_path)) != before


def test_signature_changes_when_a_file_is_renamed_or_moved(tmp_path):
    make(tmp_path, {"loose/scan.pdf": 3, "other/x.pdf": 4})
    seen = [filemap.signature(filemap.walk(tmp_path))]
    os.rename(tmp_path / "loose/scan.pdf", tmp_path / "loose/passport.pdf")
    seen.append(filemap.signature(filemap.walk(tmp_path)))
    (tmp_path / "Identity").mkdir()
    os.rename(tmp_path / "loose/passport.pdf", tmp_path / "Identity/passport.pdf")
    seen.append(filemap.signature(filemap.walk(tmp_path)))
    assert len({s["digest"] for s in seen}) == 3
    assert all(s["files"] == 2 and s["bytes"] == 7 for s in seen)


def test_an_id_need_prefers_the_sendable_copy_then_the_newest(tmp_path):
    make(tmp_path, FILES)
    os.utime(tmp_path / "Personal/Identity/Old ID Card (2015).pdf", (1, 1))
    rows = filemap.walk(tmp_path)
    got = filemap.find(rows, "a copy of my ID card", RULES)
    assert got[0]["path"] == "Personal/Identity/National ID Card (2025).pdf" and got[0]["format"] == "pdf"
    order = [r["path"] for r in got]
    assert order.index("Personal/Identity/National ID Card (2025).docx") < order.index("Personal/Identity/IMG_0001.dng")
    assert "Personal/Housing/Lease Agreement (2025).pdf" not in order


def test_a_rule_finds_a_folder_the_need_never_names(tmp_path):
    rows = filemap.walk(make(tmp_path, FILES))
    got = filemap.find(rows, "document with account and routing numbers", RULES)
    assert got and all("Bank Statements" in r["path"] for r in got)


def test_the_same_score_gives_the_newest_first(tmp_path):
    make(tmp_path, FILES)
    os.utime(tmp_path / "Personal/Finance/Bank Statements/Statement 2025-09.pdf", (10, 10))
    rows = filemap.walk(tmp_path)
    got = filemap.find(rows, "bank statement", RULES)
    assert [r["path"][-11:] for r in got] == ["2025-10.pdf", "2025-09.pdf"]


def test_a_shorter_name_beats_a_longer_one_of_the_same_score(tmp_path):
    make(tmp_path, {"P/Identity/National ID Card (2025).pdf": 1, "P/Identity/National ID Card - Someone Else (2026).pdf": 1,
                    "P/Identity/ID Card Renewal Appointment Confirmation.pdf": 1})
    rows = filemap.walk(tmp_path)
    got = filemap.find(rows, "ID card", RULES)
    assert got[0]["path"].endswith("National ID Card (2025).pdf")


def test_a_plain_word_match_works_without_rules(tmp_path):
    rows = filemap.walk(make(tmp_path, FILES))
    got = filemap.find(rows, "my lease", ())
    assert [r["path"] for r in got] == ["Personal/Housing/Lease Agreement (2025).pdf"]
    assert got[0]["why"] == ["name has lease"]


def test_no_match_gives_nothing_and_limit_holds(tmp_path):
    rows = filemap.walk(make(tmp_path, FILES))
    assert filemap.find(rows, "a copy of my document", RULES) == []
    assert len(filemap.find(rows, "statement", RULES, limit=1)) == 1


def test_words_and_stems():
    assert filemap.words("ID & Civil-Registry (2025)") == ["id", "civil", "registry", "2025"]
    assert filemap.words("Résumé Müller_Ärzte Ελληνική") == ["résumé", "müller", "ärzte", "ελληνική"]
    assert filemap.stem("statements") == "statement" and filemap.stem("class") == "class"
    assert filemap.stem("copies") == "copy"
