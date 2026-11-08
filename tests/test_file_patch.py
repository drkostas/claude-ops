"""The recipe re-apply. Every test works on a copy in a temporary folder."""
import os

from claude_ops import file_patch

ORIGINAL = 'start();\nrun(args);\nstop();\n'


def make(tmp_path, text=ORIGINAL, mode=0o644):
    f = tmp_path / "tool.js"
    f.write_text(text, encoding="utf-8")
    os.chmod(f, mode)
    return f


def recipe(**kw):
    base = dict(find="run(args);", replace="run(args, {quiet: true});", contains="quiet: true")
    base.update(kw)
    return base


def test_applies_once_and_reads_it_back(tmp_path):
    f = make(tmp_path, mode=0o755)
    r = file_patch.apply(f, **recipe(), backup_suffix=".bak-quiet")
    assert r["status"] == "applied"
    assert f.read_text() == 'start();\nrun(args, {quiet: true});\nstop();\n'
    assert (tmp_path / "tool.js.bak-quiet").read_text() == ORIGINAL
    assert oct(f.stat().st_mode & 0o777) == "0o755"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tool.js", "tool.js.bak-quiet"]


def test_a_second_apply_changes_nothing(tmp_path):
    f = make(tmp_path)
    file_patch.apply(f, **recipe())
    before = f.read_text()
    assert file_patch.apply(f, **recipe())["status"] == "present"
    assert f.read_text() == before


def test_text_the_update_changed_is_not_found_and_left_alone(tmp_path):
    f = make(tmp_path, "start();\nrun(args, other);\nstop();\n")
    r = file_patch.apply(f, **recipe(), backup_suffix=".bak-quiet")
    assert r["status"] == "not_found"
    assert f.read_text() == "start();\nrun(args, other);\nstop();\n"
    assert not (tmp_path / "tool.js.bak-quiet").exists()


def test_text_that_appears_twice_is_ambiguous(tmp_path):
    f = make(tmp_path, ORIGINAL + ORIGINAL)
    assert file_patch.apply(f, **recipe())["status"] == "ambiguous"
    assert f.read_text() == ORIGINAL + ORIGINAL


def test_a_missing_file_fails_without_creating_it(tmp_path):
    r = file_patch.apply(tmp_path / "none.js", **recipe())
    assert r["status"] == "failed" and not (tmp_path / "none.js").exists()


def test_a_recipe_whose_replacement_lacks_its_proof_is_refused(tmp_path):
    f = make(tmp_path)
    r = file_patch.apply(f, find="run(args);", replace="run(args);", contains="quiet: true")
    assert r["status"] == "failed" and f.read_text() == ORIGINAL


def test_the_first_backup_is_kept_when_applied_again_after_an_update(tmp_path):
    f = make(tmp_path)
    file_patch.apply(f, **recipe(), backup_suffix=".bak-quiet")
    f.write_text("start();\nrun(args);\nstop();\n// update\n")        # an update replaced the file
    assert file_patch.apply(f, **recipe(), backup_suffix=".bak-quiet")["status"] == "applied"
    assert (tmp_path / "tool.js.bak-quiet").read_text() == ORIGINAL
