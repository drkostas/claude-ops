"""The no-session-links hook: what it refuses in a command, in a file a command names, in a Write
and in a GitHub call, and what a `git commit` would record (staged, -a, an add on the same line).

The texts that must be refused are built at run time from parts, so that no line of this file is
itself such a text. The session id is an invented one: the tests tell the hook which ids are real.
"""
import io
import json
import os
import subprocess

import pytest

from claude_ops import cli
from claude_ops.hooks import no_session_links as nsl

FAKE_ID = "0d0e0f10-1111-4222-8333-444455556666"
OTHER_ID = "0d0e0f10-2222-4222-8333-444455556666"
LINK = "https://claude.ai/code/" + "session_" + "abc123def456"
# a trailer whose value is not a session link, so that the trailer check is the one exercised; a
# trailer that carries the link is reported as the link, which comes first
TRAILER = "Claude-" + "Session: " + "https://example.org/sessions/abc123"


@pytest.fixture(autouse=True)
def known_sessions(monkeypatch):
    monkeypatch.setattr(nsl, "_sessions", {FAKE_ID})


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                          cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.txt").write_text("one\ntwo\nthree\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "first")
    return r


def bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


# --- the texts ------------------------------------------------------------------------------------

def test_text_matchers_and_that_no_answer_carries_the_id():
    assert nsl.offending(LINK) == "a link to a Claude Code session"
    assert nsl.offending("note\n  " + TRAILER + "\n") == "a Claude-Session trailer"
    assert nsl.offending("a plain sentence about a session") is None
    why = nsl.real_session("see " + FAKE_ID)
    assert why == "the id of a real Claude session"
    assert FAKE_ID not in why and FAKE_ID[:8] not in why
    assert nsl.real_session("see " + OTHER_ID) is None
    assert nsl.real_session(f"cat /tmp/{FAKE_ID}/notes.md", paths_ok=True) is None
    assert nsl.real_session(f"cat /tmp/{FAKE_ID}/notes.md") is not None
    assert nsl.why_not(FAKE_ID, into_repo=False) is None
    assert nsl.why_not(FAKE_ID, into_repo=True) is not None


def test_session_ids_come_from_the_config_folder(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    (cfg / "projects" / "p1").mkdir(parents=True)
    (cfg / "projects" / "p1" / f"{OTHER_ID}.jsonl").write_text("{}\n")
    (cfg / "projects" / "p1" / "agent-1.jsonl").write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(nsl, "_sessions", None)
    assert nsl.session_ids() == {OTHER_ID, "agent-1"}


def test_in_repo_leaves_the_config_folder_out(tmp_path, repo, monkeypatch):
    assert nsl.in_repo(str(repo / "notes" / "x.md"))
    assert not nsl.in_repo(str(tmp_path / "loose.md"))
    cfg = tmp_path / "cfg"
    (cfg / ".git").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert not nsl.in_repo(str(cfg / "hooks" / "x.py"))


# --- the command line -----------------------------------------------------------------------------

def test_simple_commands_split_on_operators_and_newlines_and_drop_redirections():
    cmd = "cd /a && git add f; git commit -m 'x y' 2>&1 | tee log > /dev/null\ngit push || echo no"
    assert nsl.simple_commands(cmd) == [
        ["cd", "/a"], ["git", "add", "f"], ["git", "commit", "-m", "x y"], ["tee", "log"],
        ["git", "push"], ["echo", "no"]]
    assert nsl.simple_commands("(cd /a && git commit -m x)") == [["cd", "/a"], ["git", "commit", "-m", "x"]]
    assert nsl.simple_commands("echo 'unfinished") == []


def test_unwrap_drops_assignments_and_wrappers():
    assert nsl.unwrap(["GIT_X=1", "env", "-u", "PAGER", "nohup", "git", "commit"]) == ["git", "commit"]
    assert nsl.unwrap(["{", "git", "commit", "}"]) == ["git", "commit", "}"]
    assert nsl.unwrap(["git", "commit"]) == ["git", "commit"]


def test_git_invocations_follow_cd_and_C_and_keep_other_global_options(tmp_path, monkeypatch):
    base = tmp_path / "base"
    for d in ("base/sub", "base/other", "repo", "x", "a"):
        (tmp_path / d).mkdir(parents=True)
    invs = nsl.git_invocations("cd sub && git -c a=b -C ../other --no-pager commit -am x", str(base))
    assert len(invs) == 1
    inv = invs[0]
    assert inv["dir"] == str(tmp_path / "base" / "other")
    assert inv["globals"] == ["-c", "a=b"]
    assert inv["sub"] == "commit" and inv["args"] == ["-am", "x"]
    # -C after the subcommand is the commit's own option (reuse a message), not a folder
    inv = nsl.git_invocations("git commit -C HEAD", str(base))[0]
    assert inv["dir"] == str(base) and inv["args"] == ["-C", "HEAD"]
    # ~ and $HOME are expanded, as the shell would
    monkeypatch.setenv("HOME", str(tmp_path))
    assert nsl.git_invocations("git -C ~/repo commit", str(base))[0]["dir"] == str(tmp_path / "repo")
    assert nsl.git_invocations("git -C $HOME/repo commit", str(base))[0]["dir"] == str(tmp_path / "repo")
    # a cd with options, a cd home, a cd back, and a cd to what is not a folder (a `$(...)`, an
    # unset variable, a missing path) which leaves the shell where it was
    x, a = str(tmp_path / "x"), str(tmp_path / "a")
    assert nsl.git_invocations(f"cd -P {x} && git status", str(tmp_path))[0]["dir"] == x
    assert nsl.git_invocations("cd && git status", x)[0]["dir"] == os.path.expanduser("~")
    assert nsl.git_invocations("cd - && git status", x)[0]["dir"] == x
    assert nsl.git_invocations('cd "$(git rev-parse --show-toplevel)" && git status', x)[0]["dir"] == x
    assert nsl.git_invocations("cd $NO_SUCH_VARIABLE_HERE && git status", x)[0]["dir"] == x
    assert nsl.git_invocations("git -C /no/such/folder status", x)[0]["dir"] == x
    # pushd and popd keep a stack
    dirs = [i["dir"] for i in nsl.git_invocations(f"pushd {a} && git status && popd && git log; popd; git log", x)]
    assert dirs == [a, x, x]
    # a line continuation is removed, as the shell removes it
    assert nsl.git_invocations("git commit \\\n  -am ok", x)[0]["args"] == ["-am", "ok"]
    assert nsl.simple_commands("echo a\\\\\necho b") == [["echo", "a\\"], ["echo", "b"]]
    # a here-document's body is data, not words
    cmd = "cat > n.md <<'EOF'\nit's data, with a quote\nEOF\ngit add n.md && git commit -m ok"
    assert nsl.simple_commands(cmd) == [["cat"], ["git", "add", "n.md"], ["git", "commit", "-m", "ok"]]
    # shell words that may stand before a command
    assert nsl.git_invocations("if true; then git commit -am ok; fi", x)[0]["sub"] == "commit"
    assert nsl.git_invocations("! git commit -am ok || true", x)[0]["sub"] == "commit"
    # no subcommand, and a command that is not git
    assert nsl.git_invocations("git --version; ls", "/") == []
    assert nsl.git_invocations("/usr/bin/git commit -m x", "/")[0]["sub"] == "commit"


def test_a_trailer_that_carries_the_link_is_reported_as_the_link():
    assert nsl.offending("Claude-" + "Session: " + LINK) == "a link to a Claude Code session"


def test_commit_plan_reads_all_and_pathspecs_past_the_options():
    def plan(*args):
        return nsl.commit_plan({"sub": "commit", "args": list(args)})
    assert plan("-m", "x") == {"all": False, "paths": [], "include": False}
    assert plan("-am", "x") == {"all": True, "paths": [], "include": False}
    assert plan("-a", "-m", "x", "--", "f", "g") == {"all": True, "paths": ["f", "g"], "include": False}
    assert plan("-m", "x", "a.txt") == {"all": False, "paths": ["a.txt"], "include": False}
    assert plan("-mtext", "a.txt") == {"all": False, "paths": ["a.txt"], "include": False}
    assert plan("-uall", "-m", "x") == {"all": False, "paths": [], "include": False}
    assert plan("-S", "-am", "x") == {"all": True, "paths": [], "include": False}
    assert plan("-Skey", "-a", "-m", "x") == {"all": True, "paths": [], "include": False}
    assert plan("-u", "-am", "x") == {"all": True, "paths": [], "include": False}
    assert plan("-aS", "-m", "x") == {"all": True, "paths": [], "include": False}
    assert plan("-C", "HEAD", "-c", "HEAD~1") == {"all": False, "paths": [], "include": False}
    assert plan("--author", "a <a@example.org>", "--date", "now", "-m", "x") == {"all": False, "paths": [], "include": False}
    assert plan("-i", "a.txt", "-m", "x") == {"all": False, "paths": ["a.txt"], "include": True}
    assert plan("--dry-run", "-m", "x") is None
    assert nsl.commit_plan({"sub": "add", "args": ["f"]}) is None


def test_add_plan_reads_sweeping_forms_and_pathspecs():
    def plan(*args):
        return nsl.add_plan({"sub": "add", "args": list(args)})
    assert plan("f", "g") == {"sweeping": False, "paths": ["f", "g"], "force": False}
    assert plan(".") == {"sweeping": False, "paths": ["."], "force": False}
    assert plan("-A") == {"sweeping": True, "paths": [], "force": False}
    assert plan("-u", "src") == {"sweeping": True, "paths": ["src"], "force": False}
    assert plan("-f", "--", "-weird") == {"sweeping": False, "paths": ["-weird"], "force": True}
    assert plan("--pathspec-from-file", "list.txt") == {"sweeping": False, "paths": [], "force": False}
    assert plan("-f", "ignored.log") == {"sweeping": False, "paths": ["ignored.log"], "force": True}
    assert plan("-Af") == {"sweeping": True, "paths": [], "force": True}
    assert plan("-n", "f") is None
    assert plan("--dry-run", "f") is None


def test_added_lines_keeps_the_new_file_line_numbers():
    diff = (b"diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -0,0 +1,2 @@\n+first\n+second\n"
            b"@@ -5 +7 @@\n-old\n+seventh\ndiff --git a/gone b/gone\n--- a/gone\n+++ /dev/null\n"
            b"@@ -1 +0,0 @@\n-bye\n")
    assert nsl.added_lines(diff) == [("f.txt", 1, "first"), ("f.txt", 2, "second"), ("f.txt", 7, "seventh")]


def test_a_changed_line_that_looks_like_a_diff_header_hides_nothing():
    # inside a hunk, "+++ /dev/null" is a line of the file, not a header that ends the file
    diff = (b"diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n--- a/x\n+++ /dev/null\n"
            b"@@ -3,0 +4 @@\n+leak\n")
    assert nsl.added_lines(diff) == [("a.txt", 1, "++ /dev/null"), ("a.txt", 4, "leak")]


def test_a_forged_header_in_a_real_repository_hides_nothing(repo):
    (repo / "a.txt").write_text("-- a/x\ntwo\nthree\n")
    git(repo, "commit", "-q", "-am", "base")
    (repo / "a.txt").write_text("++ /dev/null\ntwo\nthree\n" + LINK + "\n")
    git(repo, "add", "a.txt")
    assert nsl.check(bash("git commit -m ok", repo)) == \
        "this commit would record a link to a Claude Code session in a.txt:4"


# --- what a commit would record -------------------------------------------------------------------

def test_staged_leak_is_refused_with_file_and_line_and_without_the_id(repo):
    (repo / "b.txt").write_text("fine\n" + LINK + "\nalso fine\n")
    (repo / "c.txt").write_text(FAKE_ID + "\n")
    git(repo, "add", "b.txt", "c.txt")
    why = nsl.check(bash("git commit -m ok", repo))
    assert why == "this commit would record a link to a Claude Code session in b.txt:2 (and 1 more)"
    assert nsl.commit_content("git commit -m ok", str(repo)) == [
        ("a link to a Claude Code session", "b.txt", 2), ("the id of a real Claude session", "c.txt", 1)]
    git(repo, "rm", "-q", "--cached", "b.txt")
    why = nsl.check(bash("git commit -m ok", repo))
    assert why == "this commit would record the id of a real Claude session in c.txt:1"
    assert FAKE_ID not in why and FAKE_ID[:8] not in why


def test_a_clean_commit_a_dry_run_and_a_deletion_go_through(repo):
    (repo / "b.txt").write_text("nothing to see\n")
    git(repo, "add", "b.txt")
    assert nsl.check(bash("git commit -m ok", repo)) is None
    (repo / "c.txt").write_text(LINK + "\n")
    git(repo, "add", "c.txt")
    assert nsl.check(bash("git commit --dry-run -m ok", repo)) is None
    git(repo, "rm", "-q", "--cached", "c.txt")
    git(repo, "rm", "-q", "a.txt")
    assert nsl.check(bash("git commit -m 'drop a'", repo)) is None


def test_commit_a_scans_tracked_changes_that_are_not_staged(repo):
    (repo / "a.txt").write_text("one\n" + TRAILER + "\nthree\n")
    assert nsl.check(bash("git commit -m ok", repo)) is None
    assert nsl.check(bash("git commit -am ok", repo)) == \
        "this commit would record a Claude-Session trailer in a.txt:2"
    assert nsl.check(bash("git commit --all -m ok", repo)) is not None


def test_commit_with_a_pathspec_scans_that_file(repo):
    (repo / "a.txt").write_text("one\ntwo\n" + LINK + "\n")
    assert nsl.check(bash("git commit -m ok a.txt", repo)) == \
        "this commit would record a link to a Claude Code session in a.txt:3"
    assert nsl.check(bash("git commit -m ok -- a.txt", repo)) is not None
    assert nsl.check(bash("git commit -m ok other.txt", repo)) is None


def test_an_add_on_the_same_line_is_scanned_before_it_runs(repo):
    (repo / "d.txt").write_text(LINK + "\n")
    expected = "this commit would record a link to a Claude Code session in d.txt:1"
    assert nsl.check(bash("git add d.txt && git commit -m ok", repo)) == expected
    assert nsl.check(bash("git add -A && git commit -m ok", repo)) == expected
    assert nsl.check(bash("git add . && git commit -m ok", repo)) == expected
    assert nsl.check(bash("git add d.txt\ngit commit -m ok", repo)) == expected
    assert nsl.check(bash("git add -n d.txt && git commit -m ok", repo)) is None
    assert nsl.check(bash("git add other.txt && git commit -m ok", repo)) is None
    (repo / "d.txt").unlink()
    (repo / "a.txt").write_text("one\ntwo\nthree\n" + FAKE_ID + "\n")
    assert nsl.check(bash("git add a.txt && git commit -m ok", repo)) == \
        "this commit would record the id of a real Claude session in a.txt:4"
    assert nsl.check(bash("git add -u && git commit -m ok", repo)) is not None


def test_shapes_the_shell_reads_differently_from_a_plain_split(repo):
    (repo / "a.txt").write_text("one\n" + LINK + "\nthree\n")
    expected = "this commit would record a link to a Claude Code session in a.txt:2"
    assert nsl.check(bash("git commit \\\n  -am ok", repo)) == expected
    assert nsl.check(bash("git commit -m ok \\\n  -a", repo)) == expected
    assert nsl.check(bash("git commit -m ok \\\n  -- a.txt", repo)) == expected
    assert nsl.check(bash("if true; then git commit -am ok; fi", repo)) == expected
    assert nsl.check(bash("! git commit -am ok || true", repo)) == expected
    assert nsl.check(bash("git commit -S -am ok", repo)) == expected
    # a line that cannot be read as words still gets what is staged and what -a takes scanned
    assert nsl.check(bash("git commit -am ok  # don't", repo)) == expected
    assert nsl.check(bash("timeout 60 git commit -am ok", repo)) == expected
    assert nsl.check(bash('bash -c "git commit -am ok"', repo)) == expected
    assert nsl.check(bash("echo 'git commit' > note.txt", repo)) is None
    (repo / "d.txt").write_text(LINK + "\n")
    cmd = "cat > n.md <<'EOF'\nit's fine\nEOF\ngit add d.txt && git commit -m ok"
    assert nsl.check(bash(cmd, repo)) == "this commit would record a link to a Claude Code session in d.txt:1"
    assert nsl.check(bash("git add \\\n  d.txt && git commit -m ok", repo)) is not None


def test_a_first_commit_and_a_file_marked_binary_are_scanned(tmp_path, repo):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q")
    (fresh / "f.txt").write_text("clean\n")
    git(fresh, "add", "f.txt")
    (fresh / "f.txt").write_text("clean\n" + LINK + "\n")
    assert nsl.check(bash("git commit -am ok", fresh)) == \
        "this commit would record a link to a Claude Code session in f.txt:2"
    assert nsl.check(bash("git commit -m ok f.txt", fresh)) is not None
    (repo / ".gitattributes").write_text("*.txt -diff\n")
    (repo / "b.txt").write_text("fine\n" + LINK + "\n")
    git(repo, "add", ".gitattributes", "b.txt")
    assert nsl.check(bash("git commit -m ok", repo)) == \
        "this commit would record a link to a Claude Code session in b.txt:2"
    (repo / "u.txt").write_bytes(("x\n" + LINK + "\n").encode("utf-16-le"))
    git(repo, "add", "u.txt")
    assert ("a link to a Claude Code session", "u.txt", 2) in nsl.commit_content("git commit -m ok", str(repo))


def test_a_file_named_by_a_session_id_is_refused_without_printing_it(repo):
    (repo / "notes").mkdir()
    (repo / "notes" / f"{FAKE_ID}.md").write_text("plain\n")
    git(repo, "add", ".")
    why = nsl.check(bash("git commit -m ok", repo))
    assert why == "this commit would record the id of a real Claude session in notes/<session id>.md"
    assert FAKE_ID not in why


def test_an_add_in_another_repository_is_not_this_commits(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    (other / "e.txt").write_text(LINK + "\n")
    assert nsl.check(bash(f"git -C {other} add e.txt && git commit -m ok", repo)) is None


def test_the_repository_is_found_through_C_and_cd(repo, tmp_path, monkeypatch):
    (repo / "b.txt").write_text(LINK + "\n")
    git(repo, "add", "b.txt")
    expected = "this commit would record a link to a Claude Code session in b.txt:1"
    assert nsl.check(bash(f"git -C {repo} commit -m ok", tmp_path)) == expected
    assert nsl.check(bash(f"cd {repo} && git commit -m ok", tmp_path)) == expected
    assert nsl.check(bash(f"git -C {repo} -c user.name=t commit -m ok", tmp_path)) == expected
    assert nsl.check(bash(f"GIT_X=1 nohup git -C {repo} commit -m ok 2>&1 | cat", tmp_path)) == expected
    assert nsl.check(bash(f"git -C {repo} commit -C HEAD", tmp_path)) == expected
    monkeypatch.setenv("HOME", str(tmp_path))
    assert nsl.check(bash("git -C ~/repo commit -m ok", tmp_path)) == expected


def test_outside_a_repository_or_in_a_missing_folder_the_call_goes_through(tmp_path):
    (tmp_path / "x.txt").write_text(LINK + "\n")
    assert nsl.check(bash("git commit -m ok", tmp_path)) is None
    assert nsl.check(bash("git -C /no/such/folder commit -m ok", tmp_path)) is None
    assert nsl.check(bash("git -C /no/such/folder add x && git commit -m ok", tmp_path)) is None


def test_commit_lines_gives_every_line_once_for_another_check(repo):
    (repo / "b.txt").write_text("alpha\nbeta\n")
    git(repo, "add", "b.txt")
    (repo / "a.txt").write_text("one\ntwo\nthree\nfour\n")
    rows = nsl.commit_lines("git add a.txt && git commit -am ok", str(repo))
    assert rows == [("a.txt", 4, "four"), ("b.txt", 1, "alpha"), ("b.txt", 2, "beta")]


# --- the rest of the tool call --------------------------------------------------------------------

def test_the_command_text_and_the_files_it_names_are_still_checked(repo, tmp_path):
    assert nsl.check(bash(f"git commit -m '{LINK}'", repo)) == \
        "this would put a link to a Claude Code session into git, GitHub or a file"
    assert nsl.check(bash(f"echo {LINK}", tmp_path)) is not None
    assert nsl.check(bash(f"git commit -m 'see {FAKE_ID}'", repo)) == \
        "this would put the id of a real Claude session into git, GitHub or a file"
    assert nsl.check(bash(f"git -C {repo} commit -m 'see {FAKE_ID}'", tmp_path)) is not None
    assert nsl.check(bash(f"gh pr create --body 'see {FAKE_ID}'", repo)) is not None
    assert nsl.check(bash(f"ls /tmp/{FAKE_ID}/", repo)) is None
    assert nsl.check(bash(f"git commit -m ok /tmp/{FAKE_ID}/f.txt", tmp_path)) is None
    (repo / "msg.txt").write_text("subject\n\n" + TRAILER + "\n")
    assert nsl.check(bash(f"git -C {repo} commit -F msg.txt", tmp_path)) == \
        "this would put a Claude-Session trailer from msg.txt into git or GitHub"
    (tmp_path / "body.md").write_text("body with " + FAKE_ID + "\n")
    assert nsl.check(bash("gh pr create --title t --body-file body.md", tmp_path)) is not None
    assert nsl.check(bash("gh pr create --title t --body-file=body.md", tmp_path)) is not None
    (tmp_path / "plain.md").write_text("nothing here\n")
    assert nsl.check(bash("gh pr create --title t --body-file plain.md", tmp_path)) is None


def test_writes_and_github_calls(repo, tmp_path):
    def write(path, content):
        return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}
    assert nsl.check(write(repo / "n.md", "see " + FAKE_ID)) is not None
    assert nsl.check(write(tmp_path / "n.md", "see " + FAKE_ID)) is None
    assert nsl.check(write(tmp_path / "n.md", LINK)) is not None
    assert nsl.check(write(repo / "n.md", "plain")) is None
    gh = {"tool_name": "mcp__github__create_issue", "tool_input": {"title": "t", "body": "see " + FAKE_ID}}
    assert nsl.check(gh) is not None
    assert nsl.check({"tool_name": "Read", "tool_input": {"file_path": str(repo / "n.md")}}) is None


def test_main_exit_codes_and_message(repo, monkeypatch, capsys):
    (repo / "b.txt").write_text("fine\n" + FAKE_ID + "\n")
    git(repo, "add", "b.txt")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bash("git commit -m ok", repo))))
    assert nsl.main() == 2
    err = capsys.readouterr().err
    assert "Blocked" in err and "b.txt:2" in err and "Remove it" in err
    assert FAKE_ID not in err and FAKE_ID[:8] not in err
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bash("git status", repo))))
    assert nsl.main() == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert nsl.main() == 0


def test_the_cli_offers_the_hook(repo, monkeypatch):
    assert "claude-ops hook no-session-links" in json.dumps(cli.HOOKS["PreToolUse"])
    assert cli.HOOKS["PreToolUse"][0]["matcher"] == "Bash|Write|Edit|MultiEdit|mcp__github__.*"
    (repo / "b.txt").write_text(LINK + "\n")
    git(repo, "add", "b.txt")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bash("git commit -m ok", repo))))
    assert cli.main(["hook", "no-session-links"]) == 2


def test_a_push_stops_a_commit_a_script_made(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "remote", "add", "origin", str(bare))
    (r / "a.txt").write_text("one\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-qm", "first")
    git(r, "push", "-q", "-u", "origin", "HEAD")
    assert nsl.check(bash("git push", r)) is None
    (r / "diag.txt").write_text("found " + FAKE_ID + "\n")     # a script writes, adds and commits it
    git(r, "add", "diag.txt")
    git(r, "commit", "-qm", "diagnostics")
    why = nsl.check(bash("git push", r))
    assert why == "this push would publish the id of a real Claude session in diag.txt:1"
    assert FAKE_ID not in why
    git(r, "reset", "-q", "--hard", "HEAD~1")
    git(r, "commit", "-q", "--allow-empty", "-m", "see " + LINK)
    assert nsl.check(bash("git push", r)) == "this push would publish a link to a Claude Code session in a commit message"


def test_an_escape_before_an_id_does_not_hide_it(repo):
    assert nsl.check(bash("git commit -m $'subject\\n" + FAKE_ID + "'", repo)) is not None
