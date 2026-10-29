"""pr_changes: what open pull requests change, by file and function, matched against a text. The
repository, its branches and the pull requests are invented in a temporary folder; gh is stood in
for by a listing."""
import json
import subprocess

import pytest

from claude_ops import cli
from claude_ops import pr_changes as pc

PY = '''import os


def spawn_helpers(conn):
    return 1


def kept_by_owner(conn, owner, it):
    return None


class Dispatcher:
    def move(self, it):
        return it

    def keep(self, it):
        return None
'''
SQL = '''BEGIN;
CREATE OR REPLACE VIEW unanswered_comments AS
  SELECT 1;
CREATE OR REPLACE FUNCTION task_owner(uuid) RETURNS text AS $$
  SELECT 'x';
$$ LANGUAGE sql;
INSERT INTO t VALUES (1);
COMMIT;
'''
SH = '''#!/bin/bash
tell() {
  echo "$1"
}
settle_taken() {
  echo taken
}
echo done
'''
TS = '''export function routeFor(page) {
  return page;
}
const helper = (x) => x;
export class Board {
  render() {}
}
'''


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                          cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "src").mkdir()
    (r / "src" / "dispatch.py").write_text(PY)
    (r / "views.sql").write_text(SQL)
    (r / "chain.sh").write_text(SH)
    (r / "app.ts").write_text(TS)
    (r / "NOTES.md").write_text("notes\n")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "first")
    # a branch that changes kept_by_owner, the SQL function, the shell function and the notes
    git(r, "checkout", "-q", "-b", "fix/keep")
    (r / "src" / "dispatch.py").write_text(PY.replace("    return None\n\n\nclass", "    return 'kept'\n\n\nclass"))
    (r / "views.sql").write_text(SQL.replace("SELECT 'x';", "SELECT 'y';"))
    (r / "chain.sh").write_text(SH.replace("echo taken", "echo taken twice"))
    (r / "NOTES.md").write_text("notes\nmore\n")
    git(r, "commit", "-q", "-am", "keep")
    # a branch that changes a method and the TypeScript class, and adds a module-level line
    git(r, "checkout", "-q", "main")
    git(r, "checkout", "-q", "-b", "feat/board")
    (r / "src" / "dispatch.py").write_text(PY.replace("        return it\n", "        return [it]\n") + "LIMIT = 3\n")
    (r / "app.ts").write_text(TS.replace("  render() {}", "  render() { return 1; }"))
    git(r, "commit", "-q", "-am", "board")
    git(r, "checkout", "-q", "main")
    return r


PRS = [{"number": 12, "branch": "fix/keep", "base": "main", "title": "Keep open questions"},
       {"number": 13, "branch": "feat/board", "base": "main", "title": "The board renders"}]


def test_diff_ranges_reads_the_new_side_and_a_pure_deletion():
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -3,0 +4,2 @@ def f():\n+a\n+b\n"
            "@@ -10 +12,0 @@ def g():\n-c\n@@ -20,2 +21 @@\n-d\n-e\n+f\n")
    assert pc.diff_ranges(diff) == {"x.py": [(4, 5), (12, 12), (21, 21)]}


def test_definitions_by_language():
    assert pc.definitions("a.py", PY) == [("spawn_helpers", 4, 5), ("kept_by_owner", 8, 9), ("Dispatcher", 12, 17),
                                          ("move", 13, 14), ("keep", 16, 17)]
    assert pc.definitions("a.py", "def broken(:\n") == []
    assert pc.definitions("v.sql", SQL) == [("unanswered_comments", 2, 3), ("task_owner", 4, 6)]
    assert pc.definitions("c.sh", SH) == [("tell", 2, 4), ("settle_taken", 5, 7)]
    assert pc.definitions("a.ts", TS) == [("routeFor", 1, 3), ("helper", 4, 4), ("Board", 5, 7)]
    assert pc.definitions("notes.md", "# x\n") == []


def test_functions_in_names_each_definition_a_change_touches_once():
    defs = pc.definitions("a.py", PY)
    assert pc.functions_in(defs, [(9, 9)]) == ["kept_by_owner"]
    assert pc.functions_in(defs, [(14, 14), (14, 14)]) == ["Dispatcher", "move"]
    assert pc.functions_in(defs, [(1, 1)]) == []


def test_changes_reads_each_open_pull_request_from_its_branch(repo):
    rows = pc.changes(None, str(repo), prs=PRS, cache=None, ref_prefix="")
    by = {r["number"]: r["files"] for r in rows}
    assert by[12] == {"src/dispatch.py": ["kept_by_owner"], "views.sql": ["task_owner"],
                      "chain.sh": ["settle_taken"], "NOTES.md": []}
    assert by[13] == {"src/dispatch.py": ["Dispatcher", "move"], "app.ts": ["Board"]}
    assert rows[0]["title"] == "Keep open questions" and rows[0]["branch"] == "fix/keep"


def test_a_branch_git_cannot_read_changes_nothing(repo):
    rows = pc.changes(None, str(repo), prs=[{"number": 9, "branch": "gone", "base": "main", "title": "x"}],
                      cache=None, ref_prefix="")
    assert rows == [{"number": 9, "branch": "gone", "base": "main", "title": "x", "files": {}}]


def test_the_cache_answers_while_young_and_for_the_same_repository(repo, tmp_path):
    cache = tmp_path / "cache.json"
    calls = []

    def run(argv, **kw):
        calls.append(argv[:3])
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
                [{"number": 12, "headRefName": "fix/keep", "baseRefName": "main", "title": "Keep open questions"}]), stderr="")
        return subprocess.run(argv, **kw)

    first = pc.changes("o/r", str(repo), run=run, cache=cache, now=lambda: 1000.0, ref_prefix="")
    assert [r["number"] for r in first] == [12] and first[0]["files"]["src/dispatch.py"] == ["kept_by_owner"]
    gh_calls = len([c for c in calls if c[0] == "gh"])
    second = pc.changes("o/r", str(repo), run=run, cache=cache, now=lambda: 1100.0, ref_prefix="")
    assert second == first and len([c for c in calls if c[0] == "gh"]) == gh_calls
    pc.changes("o/other", str(repo), run=run, cache=cache, now=lambda: 1100.0, ref_prefix="")
    assert len([c for c in calls if c[0] == "gh"]) == gh_calls + 1
    pc.changes("o/other", str(repo), run=run, cache=cache, now=lambda: 1100.0 + pc.CACHE_S + 1, ref_prefix="")
    assert len([c for c in calls if c[0] == "gh"]) == gh_calls + 2


def test_open_prs_is_empty_when_gh_fails_or_answers_nonsense():
    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no auth")
    assert pc.open_prs("o/r", run=failing) == []

    def nonsense(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")
    assert pc.open_prs("o/r", run=nonsense) == []

    def raising(argv, **kw):
        raise OSError("no gh")
    assert pc.open_prs("o/r", run=raising) == []


def test_names_in_finds_paths_and_function_names_and_leaves_everyday_words():
    named = pc.names_in("Fix kept_by_owner() in src/dispatch.py and `settle_taken`; dispatch.spawn_helpers "
                        "reads it. Run the tests, check the main page, and open docs/notes.md.")
    assert named["files"] == {"src/dispatch.py"}
    assert pc.names_in("touch chain.sh and Dispatcher.move")["files"] == {"chain.sh"}
    assert pc.names_in("touch chain.sh and Dispatcher.move")["functions"] == {"Dispatcher", "move"}
    assert named["functions"] == {"kept_by_owner", "settle_taken", "spawn_helpers"}
    assert pc.names_in("")["functions"] == set()


def test_overlaps_and_warnings_name_the_pull_request_function_and_file(repo):
    rows = pc.changes(None, str(repo), prs=PRS, cache=None, ref_prefix="")
    found = pc.overlaps(rows, "Move the critical items first in spawn_helpers and kept_by_owner.")
    assert found == [{"number": 12, "title": "Keep open questions", "path": "src/dispatch.py", "function": "kept_by_owner"}]
    lines = pc.warnings("make Dispatcher.move return a list; touch chain.sh too", listing=rows)
    assert lines == ["open pull request #12 (Keep open questions) already changes chain.sh",
                     "open pull request #13 (The board renders) already changes Dispatcher in src/dispatch.py",
                     "open pull request #13 (The board renders) already changes move in src/dispatch.py"]
    assert pc.overlaps(rows, "a brief about the board", exclude=[13]) == []
    assert pc.overlaps(rows, "render the Board", exclude=["feat/board"]) == []
    assert pc.overlaps(rows, "the NOTES.md file") == [], "a file that is not code is not matched by path"
    assert pc.warnings("nothing named here", listing=rows) == []


def test_the_command_prints_overlaps_and_exits_1_when_there_are_any(repo, tmp_path, capsys):
    prs = tmp_path / "prs.json"
    prs.write_text(json.dumps(PRS))
    rc = cli.main(["pr-changes", "--git-dir", str(repo), "--ref-prefix", "", "--prs-file", str(prs), "--text", "fix kept_by_owner"])
    out = capsys.readouterr().out
    assert rc == 1 and "open pull request #12 (Keep open questions) already changes kept_by_owner in src/dispatch.py" in out
    rc = cli.main(["pr-changes", "--git-dir", str(repo), "--ref-prefix", "", "--prs-file", str(prs), "--text", "nothing"])
    assert rc == 0 and capsys.readouterr().out == ""
    rc = cli.main(["pr-changes", "--git-dir", str(repo), "--ref-prefix", "", "--prs-file", str(prs), "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0 and [r["number"] for r in rows] == [12, 13]
