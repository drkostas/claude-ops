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


def test_a_branch_git_cannot_read_is_unread_with_the_reason_never_empty(repo, tmp_path, capsys):
    gone = {"number": 9, "branch": "gone", "base": "main", "title": "Not fetched"}
    rows = pc.changes(None, str(repo), prs=[gone], cache=None, ref_prefix="")
    assert rows == [{**gone, "files": {}, "unread": f"gone is not in {repo}"}]
    assert pc.read_files(str(repo), "main", "gone", ref_prefix="origin/") == ({}, f"origin/gone is not in {repo} (git fetch origin gone)")
    assert pc.read_files(str(repo), "nowhere", "fix/keep", ref_prefix="") == ({}, f"nowhere is not in {repo}")
    assert pc.pr_files(str(repo), "main", "gone", ref_prefix="") == {}
    # the warning lines say so, after the overlaps, and the command exits 2 when nothing was found
    listing = pc.changes(None, str(repo), prs=PRS + [gone], cache=None, ref_prefix="")
    lines = pc.warnings("fix kept_by_owner", listing=listing)
    assert lines == ["open pull request #12 (Keep open questions) already changes kept_by_owner in src/dispatch.py",
                     f"open pull request #9 (Not fetched) could not be read: gone is not in {repo}"]
    assert pc.warnings("fix kept_by_owner", listing=listing, with_unread=False) == lines[:1]
    assert pc.unread_lines(listing) == lines[1:] and pc.unread_lines(listing[:2]) == []
    prs = tmp_path / "prs.json"
    prs.write_text(json.dumps(PRS + [gone]))
    rc = cli.main(["pr-changes", "--git-dir", str(repo), "--ref-prefix", "", "--prs-file", str(prs), "--text", "nothing named"])
    out = capsys.readouterr()
    assert rc == 2 and out.out.strip() == lines[1]
    rc = cli.main(["pr-changes", "--git-dir", str(repo), "--ref-prefix", "", "--prs-file", str(prs), "--text", "kept_by_owner"])
    assert rc == 1 and capsys.readouterr().out.splitlines() == lines
    rc = cli.main(["pr-changes", "--git-dir", str(repo), "--ref-prefix", "", "--prs-file", str(prs), "--text", "nothing", "--json"])
    out = capsys.readouterr()
    assert rc == 2 and json.loads(out.out) == [] and out.err.strip() == lines[1], "the JSON stays JSON; the unread line goes to stderr"
    rc = cli.main(["pr-changes", "--git-dir", str(repo), "--ref-prefix", "", "--prs-file", str(prs)])
    out = capsys.readouterr().out
    assert rc == 2 and f"#9 gone: Not fetched\n   could not be read: gone is not in {repo}" in out
    rc = cli.main(["pr-changes", "--git-dir", str(repo), "--ref-prefix", "", "--prs-file", str(prs), "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 2 and rows[2]["unread"] == f"gone is not in {repo}" and "unread" not in rows[0]


def test_a_folder_that_is_no_working_tree_reads_nothing_and_says_so(repo, tmp_path, capsys):
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    rows = pc.changes(None, str(nowhere), prs=PRS, cache=None, ref_prefix="")
    assert [r["files"] for r in rows] == [{}, {}]
    assert all(r["unread"].startswith(f"{nowhere} is not a git working tree (fatal: not a git repository") for r in rows)
    assert pc.unread_lines(rows) == [f"open pull request #12 (Keep open questions) could not be read: {rows[0]['unread']}",
                                     f"open pull request #13 (The board renders) could not be read: {rows[1]['unread']}"]
    # a .git folder itself: a diff of two refs there prints nothing and exits 0, so it is refused as a folder
    dot = pc.changes(None, str(repo / ".git"), prs=PRS, cache=None, ref_prefix="")
    assert all(r["unread"].startswith(f"{repo / '.git'} is not a git working tree (fatal:") for r in dot)
    # more than three unread: one line naming the first few and the first reason
    many = pc.changes(None, str(nowhere), prs=[{"number": n, "branch": f"b{n}", "base": "main", "title": ""} for n in range(1, 8)],
                      cache=None, ref_prefix="")
    assert pc.unread_lines(many) == [f"7 of 7 open pull requests could not be read (#1, #2, #3, #4, #5, ...): {many[0]['unread']}"]
    # the command says so and exits 2, in the listing and with --text
    prs = tmp_path / "prs.json"
    prs.write_text(json.dumps(PRS))
    rc = cli.main(["pr-changes", "--git-dir", str(nowhere), "--ref-prefix", "", "--prs-file", str(prs), "--text", "fix kept_by_owner"])
    out = capsys.readouterr().out
    assert rc == 2 and "already changes" not in out and "#12 (Keep open questions) could not be read: " in out


def test_the_cache_is_per_repository_folder_and_reads_an_unread_pull_request_again(repo, tmp_path):
    cache = tmp_path / "cache.json"
    listing = [{"number": 12, "headRefName": "fix/keep", "baseRefName": "main", "title": "Keep open questions"},
               {"number": 14, "headRefName": "feat/late", "baseRefName": "main", "title": "Late"}]
    calls = []

    def run(argv, **kw):
        calls.append(argv[:3])
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(listing), stderr="")
        return subprocess.run(argv, **kw)

    gh = lambda: len([c for c in calls if c[0] == "gh"])  # noqa: E731
    first = pc.changes("o/r", str(repo), run=run, cache=cache, now=lambda: 1000.0, ref_prefix="")
    assert first[0]["files"]["src/dispatch.py"] == ["kept_by_owner"]
    assert first[1]["files"] == {} and first[1]["unread"] == f"feat/late is not in {repo}"
    assert list(json.loads(cache.read_text())["entries"]) == [f"o/r@{repo / '.git'}"]
    # the same repository name read from another clone is another cache entry
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(repo), str(other))
    n = gh()
    pc.changes("o/r", str(other), run=run, cache=cache, now=lambda: 1001.0, ref_prefix="")
    assert gh() == n + 1
    # a folder that is no working tree never writes the cache, so it cannot poison the next reader
    before = cache.read_text()
    bad = pc.changes("o/r", str(tmp_path / "nowhere-yet"), run=run, cache=cache, now=lambda: 1002.0, ref_prefix="")
    assert bad and all("unread" in r for r in bad) and cache.read_text() == before
    # from a subfolder of the first repository the cache answers: same common .git
    n = gh()
    again = pc.changes("o/r", str(repo / "src"), run=run, cache=cache, now=lambda: 1003.0, ref_prefix="")
    assert gh() == n and again[1]["unread"], "the listing is from the cache, and the unread pull request is read again"
    # once the branch exists, the next call within the cache's age reads it, without gh
    git(repo, "checkout", "-q", "-b", "feat/late")
    (repo / "chain.sh").write_text(SH.replace('echo "$1"', 'echo "$2"'))
    git(repo, "commit", "-q", "-am", "late")
    git(repo, "checkout", "-q", "main")
    healed = pc.changes("o/r", str(repo), run=run, cache=cache, now=lambda: 1004.0, ref_prefix="")
    assert gh() == n and healed[1]["files"] == {"chain.sh": ["tell"]} and "unread" not in healed[1]
    entries = json.loads(cache.read_text())["entries"]
    assert "unread" not in entries[f"o/r@{repo / '.git'}"]["changes"][1]
    assert f"o/r@{other / '.git'}" in entries, "the other clone's entry stays beside it"
    # an older cache file that held one listing is read as empty, never as an answer
    cache.write_text(json.dumps({"repo": "o/r", "at": 1004.0, "changes": [{"number": 1, "branch": "x", "base": "main", "title": "", "files": {}}]}))
    n = gh()
    assert [r["number"] for r in pc.changes("o/r", str(repo), run=run, cache=cache, now=lambda: 1005.0, ref_prefix="")] == [12, 14]
    assert gh() == n + 1


def test_gh_failing_is_said_and_not_cached(repo, tmp_path):
    cache = tmp_path / "cache.json"

    def failing(argv, **kw):
        if argv[:1] == ["gh"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh: To get started with GitHub CLI, please run: gh auth login")
        return subprocess.run(argv, **kw)

    problems = []
    assert pc.changes("o/r", str(repo), run=failing, cache=cache, unread=problems) == []
    assert problems == ["the open pull requests could not be read: gh exited 1: gh: To get started with GitHub CLI, please run: gh auth login"]
    assert not cache.exists()
    assert pc.warnings("fix kept_by_owner", repo="o/r", git_dir=str(repo), run=failing, cache=cache) == problems
    assert pc.list_prs("o/r", run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="{}", stderr=""))[1] \
        == "gh answered something that is not a listing"


def test_fetch_brings_a_branch_the_checkout_lacks(repo, tmp_path):
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    git(repo, "checkout", "-q", "-b", "feat/late")
    (repo / "chain.sh").write_text(SH.replace('echo "$1"', 'echo "$2"'))
    git(repo, "commit", "-q", "-am", "late")
    git(repo, "checkout", "-q", "main")
    late = [{"number": 14, "branch": "feat/late", "base": "main", "title": "Late"}]
    rows = pc.changes(None, str(clone), prs=late, cache=None)
    assert rows[0]["unread"] == f"origin/feat/late is not in {clone} (git fetch origin feat/late)"
    rows = pc.changes(None, str(clone), prs=late, cache=None, fetch=True)
    assert rows[0]["files"] == {"chain.sh": ["tell"]} and "unread" not in rows[0]
    assert "origin/feat/late" in pc.refs_in(str(clone))


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
