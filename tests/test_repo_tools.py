"""The repository tools: union, secret-shapes, mutate and plan-apply."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_ops import cli, mutate, plan_apply, secret_shapes, union

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.org", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
       "GIT_EDITOR": "true", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args, ok=True):
    return subprocess.run(["git", "-C", str(cwd), "-c", "merge.conflictStyle=diff3", *args],
                          capture_output=True, text=True, env=ENV, check=ok)


def rebasing(tmp_path, files, upstream, branch):
    """A repository whose main and branch `topic` each changed `files`; topic is mid-rebase."""
    d = tmp_path / "repo"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    git(d, "checkout", "-qb", "topic")
    for name, text in branch.items():
        (d / name).write_text(text)
    git(d, "commit", "-qam", "topic")
    git(d, "checkout", "-q", "main")
    for name, text in upstream.items():
        (d / name).write_text(text)
    git(d, "commit", "-qam", "main")
    git(d, "checkout", "-q", "topic")
    git(d, "rebase", "-q", "main", ok=False)
    return d


REASONS = 'REASONS = {\n    "a": "first",\n}\n'


def test_union_keeps_both_added_entries_upstream_first(tmp_path):
    d = rebasing(tmp_path, {"r.py": REASONS},
                 {"r.py": REASONS.replace('"first",\n', '"first",\n    "m": "upstream",\n')},
                 {"r.py": REASONS.replace('"first",\n', '"first",\n    "b": "branch",\n')})
    assert "<<<<<<<" in (d / "r.py").read_text()
    assert union.settle_tree(d, env=ENV) == 0
    text = (d / "r.py").read_text()
    assert "<<<<<<<" not in text and text.index('"m"') < text.index('"b"')
    assert git(d, "rebase", "--continue", ok=False).returncode == 0


def test_union_refuses_a_changed_line_and_writes_nothing(tmp_path):
    code = "def f():\n    return 1\n"
    d = rebasing(tmp_path, {"a.py": code, "b.py": REASONS},
                 {"a.py": code.replace("1", "2"), "b.py": REASONS.replace('"first",\n', '"first",\n    "m": 1,\n')},
                 {"a.py": code.replace("1", "3"), "b.py": REASONS.replace('"first",\n', '"first",\n    "b": 2,\n')})
    before = {f: (d / f).read_text() for f in ("a.py", "b.py")}
    assert union.settle_tree(d, env=ENV) == 1
    assert {f: (d / f).read_text() for f in before} == before


def test_union_refuses_a_name_defined_twice(tmp_path, capsys):
    base = "import os\n\n\ndef first():\n    return 1\n"
    same = "\n\ndef derived(x):\n    return x\n"
    d = rebasing(tmp_path, {"m.py": base}, {"m.py": base + same},
                 {"m.py": base + same.replace("return x", "return x.strip()")})
    assert union.settle_tree(d, env=ENV) == 1
    assert "derived" in capsys.readouterr().err
    assert union.defined_twice("H = 1\nH = 2\nX = 3\n", "H = 1\nH = 2\n", "H = 1\nH = 2\nX = 3\n") == []


def test_union_ordered_file_lists_each_entry_once(tmp_path):
    d = rebasing(tmp_path, {"order.txt": "a\nb\n"}, {"order.txt": "a\nb\nc\nd\n"},
                 {"order.txt": "a\nb\nd\ne\n"})
    assert cli.main(["union", str(d), "--ordered", "order.txt"]) == 0
    assert (d / "order.txt").read_text() == "a\nb\nc\nd\ne\n"


def test_union_ordered_file_keeps_a_note_both_sides_wrote(tmp_path):
    d = rebasing(tmp_path, {"order.txt": "a\n"}, {"order.txt": "a\n# note\nb\n"},
                 {"order.txt": "a\n# about c\n# note\nc\n"})
    assert union.settle_tree(d, ordered=("order.txt",), env=ENV) == 0
    assert (d / "order.txt").read_text() == "a\n# note\nb\n# about c\n# note\nc\n"


def test_union_journal_drops_a_stacked_base_entry():
    text = ("<<<<<<< HEAD\nupstream entry\n||||||| base\nlower branch entry\n=======\n"
            "lower branch entry\nmy entry\n>>>>>>> topic\n")
    assert union.settle("log.md", text, journals=("log.md",)) == "upstream entry\n\nmy entry\n"
    with pytest.raises(union.Refused):
        union.settle("log.md", text)          # not named a journal: refused


def test_union_refuses_without_a_base_and_a_lone_marker():
    with pytest.raises(union.Refused, match="diff3"):
        union.settle("x.py", "<<<<<<< HEAD\na\n=======\nb\n>>>>>>> topic\n")
    with pytest.raises(union.Refused, match="markers left"):
        union.settle("x.md", "one\n||||||| parent of abc\ntwo\n")
    assert union.settle("x.md", "Title\n=======\n") == "Title\n=======\n"


def test_union_merges_json_by_keys():
    base = json.dumps({"a": 1}, indent=2) + "\n"
    ours = json.dumps({"a": 1, "m": 2}, indent=2) + "\n"
    theirs = json.dumps({"a": 1, "b": 3}, indent=2) + "\n"
    assert json.loads(union.settle_json(base, ours, theirs)) == {"a": 1, "m": 2, "b": 3}
    with pytest.raises(union.Refused):
        union.settle_json(base, json.dumps({"a": 2}, indent=2) + "\n", json.dumps({"a": 3}, indent=2) + "\n")


SHAPES = {"shapes": [{"name": "a passphrase key", "pattern": r"passphrase\s*=\s*\w", "detector": "Generic",
                      "date": "2025-01-01", "fix": "build the name at run time"}]}


def test_secret_shapes_reads_every_commit(tmp_path, capsys):
    shapes = tmp_path / "shapes.json"
    shapes.write_text(json.dumps(SHAPES))
    d = tmp_path / "repo"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    (d / "a.txt").write_text("hello\n")
    git(d, "add", "a.txt")
    git(d, "commit", "-qm", "base")
    base = git(d, "rev-parse", "HEAD").stdout.strip()
    (d / "a.txt").write_text("hello\npassphrase = value\n")
    git(d, "commit", "-qam", "add")
    (d / "a.txt").write_text("hello\n")
    git(d, "commit", "-qam", "remove")
    assert secret_shapes.main([str(d), base, "--shapes", str(shapes)]) == 1
    err = capsys.readouterr().err
    assert "a.txt:2" in err and "build the name at run time" in err
    assert secret_shapes.main([str(d), "HEAD", "--shapes", str(shapes)]) == 0
    assert secret_shapes.main([str(d), base, "--shapes", str(tmp_path / "none.json")]) == 2


def test_secret_shapes_added_lines():
    diff = "+++ b/f.py\n@@ -1,0 +3,2 @@\n+one\n+two\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    assert secret_shapes.added_lines(diff) == [("f.py", 3, "one"), ("f.py", 4, "two")]


LIB = '''def grade(score):
    """A pass from 50."""
    if score >= 50:
        return "pass"
    return "fail"
'''
CHECK = "import sys\nsys.path.insert(0, sys.argv[1])\nimport lib\nraise SystemExit(0 if {cond} else 1)\n"


def project(tmp_path, cond):
    root = tmp_path / "project"
    root.mkdir()
    (root / "lib.py").write_text(LIB)
    (root / "check.py").write_text(CHECK.format(cond=cond))
    return root


def test_mutate_a_test_that_tries_the_edge_catches_everything(tmp_path):
    root = project(tmp_path, 'lib.grade(50) == "pass" and lib.grade(49) == "fail"')
    out = tmp_path / "r.jsonl"
    assert mutate.main([str(root), "--run", f"{sys.executable} {{copy}}/check.py {{copy}}",
                        "--function", "lib.py:grade", "--out", str(out)]) == 0
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert rows and all(r["caught"] for r in rows)
    assert (root / "lib.py").read_text() == LIB


def test_mutate_a_test_that_never_tries_the_edge_is_found(tmp_path, capsys):
    root = project(tmp_path, 'lib.grade(90) == "pass" and lib.grade(10) == "fail"')
    assert mutate.main([str(root), "--run", f"{sys.executable} {{copy}}/check.py {{copy}}",
                        "--function", "lib.py:grade"]) == 1
    assert "edge" in capsys.readouterr().out


def test_mutate_refuses_a_baseline_that_fails_and_a_run_without_copy(tmp_path):
    root = project(tmp_path, "False")
    run = f"{sys.executable} {{copy}}/check.py {{copy}}"
    assert mutate.main([str(root), "--run", run, "--function", "lib.py:grade"]) == 2
    assert mutate.main([str(root), "--run", "true", "--function", "lib.py:grade"]) == 2
    assert mutate.main([str(root), "--run", run, "--caught", "0", "--function", "lib.py:grade"]) == 2


PLAN = """# Plan

- [ ] **Step 1: Write the module** (Write `{dir}/m.py`)

```python
X = 1
```

- [ ] **Step 2: Change it** (Edit on `{dir}/m.py`)

```python
X = 1
```

```python
X = 2
```
"""


def test_plan_apply_writes_then_edits_and_refuses_a_missing_old_text(tmp_path, capsys):
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN.format(dir=tmp_path))
    assert cli.main(["plan-apply", str(plan), "Step 1", "Step 2"]) == 0
    assert (tmp_path / "m.py").read_text() == "X = 2\n"
    assert plan_apply.main([str(plan), "Step 2"]) == 1
    assert "0 times" in capsys.readouterr().err
    assert plan_apply.main([str(plan), "--steps"]) == 0
