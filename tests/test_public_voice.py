"""The public-voice hook: its configuration, what it refuses in a public repository (a Write, a
commit or tag message, a gh text, text a command writes into a file, what a commit would record),
and that a private repository, a personal one and a clean text go through.

Every term here is invented, and GitHub is never asked: visibility comes from VISIBILITY.
"""
import io
import json
import subprocess

import pytest

from claude_ops import cli
from claude_ops.hooks import commandline
from claude_ops.hooks import public_voice as pv

VISIBILITY = {"example-owner/thing": "PUBLIC", "example-owner/secret": "PRIVATE",
              "janedoe/janedoe.github.io": "PUBLIC"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    more = tmp_path / "more-terms.json"
    more.write_text(json.dumps({"terms": ["frobnitz"], "home": ["wibbleton"]}))
    data = {
        "terms": [r"\bzorblax\b", r"\bquuxcorp\b", r"not(a valid pattern"],
        "terms_except_personal": [r"\bjanedoe\b"],
        "personal_repositories": ["JaneDoe/janedoe.github.io"],
        "allow": [r"zorblax-public"],
        "term_files": [{"path": str(more), "key": "terms"},
                       {"path": str(more), "key": "home", "except_personal": True},
                       {"path": str(tmp_path / "missing.json"), "key": "terms"}],
        "visibility_cache": str(tmp_path / "cache" / "visibility.json"),
        "message": "Use invented examples.",
    }
    path = tmp_path / "public-voice.json"
    path.write_text(json.dumps(data))
    asked = []

    def visibility(slug):
        asked.append(slug)
        return VISIBILITY.get(slug)

    monkeypatch.setattr(pv, "repo_visibility", visibility)
    c = pv.load(str(path))
    c.asked = asked
    c.path = str(path)
    return c


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                          cwd=repo, check=True, capture_output=True, text=True)


def make_repo(tmp_path, name, slug):
    r = tmp_path / name
    r.mkdir()
    git(r, "init", "-q")
    git(r, "remote", "add", "origin", f"https://github.com/{slug}.git")
    (r / "a.txt").write_text("one\ntwo\nthree\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "first")
    return r


@pytest.fixture
def public(tmp_path):
    return make_repo(tmp_path, "public", "example-owner/thing")


@pytest.fixture
def private(tmp_path):
    return make_repo(tmp_path, "private", "example-owner/secret")


@pytest.fixture
def personal(tmp_path):
    return make_repo(tmp_path, "personal", "janedoe/janedoe.github.io")


def bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}


# --- the configuration ----------------------------------------------------------------------------

def test_terms_come_from_the_file_and_its_term_files(cfg):
    assert cfg.terms_for(personal=False) == [
        r"\bzorblax\b", r"\bquuxcorp\b", r"not(a valid pattern", "frobnitz", "wibbleton", r"\bjanedoe\b"]
    assert cfg.terms_for(personal=True) == [r"\bzorblax\b", r"\bquuxcorp\b", r"not(a valid pattern", "frobnitz"]
    assert cfg.is_personal("janedoe/JANEDOE.github.io") and not cfg.is_personal("example-owner/thing")


def test_a_missing_or_broken_file_is_no_terms(tmp_path):
    assert pv.load(str(tmp_path / "none.json")).terms_for(False) == []
    (tmp_path / "bad.json").write_text("{not json")
    assert pv.load(str(tmp_path / "bad.json")).terms_for(False) == []
    assert pv.load(str(tmp_path / "none.json")).visibility_cache.endswith("public-voice-visibility.json")


def test_offending_removes_allowed_text_ignores_case_and_skips_bad_patterns(cfg):
    terms = cfg.terms_for(False)
    assert pv.offending("see zorblax-public here", terms, cfg.allow) is None
    assert pv.offending("see ZorBlax here", terms, cfg.allow) == "ZorBlax"
    assert pv.offending("not(a valid pattern", terms, cfg.allow) is None
    assert pv.offending("", terms, cfg.allow) is None


def test_the_lists_are_printed_for_a_ci_secret(cfg, capsys):
    assert pv.main(["--config", cfg.path, "--terms"]) == 0
    assert capsys.readouterr().out.splitlines() == cfg.terms_for(False)
    assert pv.main(["--config", cfg.path, "--terms-for", "janedoe/janedoe.github.io"]) == 0
    assert capsys.readouterr().out.splitlines() == cfg.terms_for(True)
    assert pv.main(["--config", cfg.path, "--allow"]) == 0
    assert capsys.readouterr().out.splitlines() == ["zorblax-public"]


def test_the_config_comes_from_the_environment_or_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv(pv.CONFIG_ENV, str(tmp_path / "x.json"))
    assert pv.config_path() == str(tmp_path / "x.json")
    assert pv.config_path(str(tmp_path / "y.json")) == str(tmp_path / "y.json")
    monkeypatch.delenv(pv.CONFIG_ENV)
    assert pv.config_path().endswith("/.config/claude-ops/public-voice.json")


# --- which repository is public ------------------------------------------------------------------

def test_visibility_is_cached_and_an_unknown_answer_is_not_public(cfg, public, monkeypatch):
    assert pv.slug_of(str(public)) == "example-owner/thing"
    assert pv.slug_of(str(public / "no" / "such" / "folder")) == "example-owner/thing"
    assert pv.is_public("example-owner/thing", cfg) and pv.is_public("example-owner/thing", cfg)
    assert cfg.asked == ["example-owner/thing"]
    assert not pv.is_public("example-owner/unknown", cfg)
    assert not pv.is_public("example-owner/unknown", cfg)
    assert cfg.asked.count("example-owner/unknown") == 2       # an unknown answer is not cached
    monkeypatch.setattr(pv, "TTL_S", -1)
    pv.is_public("example-owner/thing", cfg)
    assert cfg.asked.count("example-owner/thing") == 2         # an expired one is asked again
    assert not pv.is_public(None, cfg)


def test_clean_text_never_asks_github(cfg, public):
    assert pv.check(write(public / "n.md", "nothing private"), cfg) is None
    assert pv.check(bash("git commit -m 'plain words'", public), cfg) is None
    assert cfg.asked == []


# --- writes, messages and GitHub calls -----------------------------------------------------------

def test_writes_and_edits_into_a_public_repository(cfg, public, private, personal, tmp_path):
    assert pv.check(write(public / "n.md", "made for zorblax"), cfg) == "'zorblax' in n.md"
    assert pv.check(write(public / "n.md", "the frobnitz list"), cfg) == "'frobnitz' in n.md"
    assert pv.check(write(public / "n.md", "the zorblax-public name"), cfg) is None
    assert pv.check(write(private / "n.md", "made for zorblax"), cfg) is None
    assert pv.check(write(tmp_path / "loose.md", "made for zorblax"), cfg) is None
    assert pv.check(write(personal / "n.md", "janedoe from wibbleton"), cfg) is None
    assert pv.check(write(personal / "n.md", "janedoe and zorblax"), cfg) == "'zorblax' in n.md"
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(public / "a.txt"), "old_string": "x",
                                                "new_string": "quuxcorp"}}
    assert pv.check(edit, cfg) == "'quuxcorp' in a.txt"
    multi = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(public / "a.txt"),
                                                      "edits": [{"new_string": "fine"}, {"new_string": "janedoe"}]}}
    assert pv.check(multi, cfg) == "'janedoe' in a.txt"


def test_commit_and_tag_messages(cfg, public, private, tmp_path):
    assert pv.check(bash("git commit -m 'about zorblax'", public), cfg) == "'zorblax' in the commit message"
    assert pv.check(bash("git commit -am 'about zorblax'", public), cfg) is not None
    assert pv.check(bash("git commit -m'about zorblax'", public), cfg) is not None
    assert pv.check(bash("git commit --message='about zorblax'", public), cfg) is not None
    assert pv.check(bash(f"cd {tmp_path} && git -C {public} tag -a v1 -m quuxcorp", tmp_path), cfg) == \
        "'quuxcorp' in the tag message"
    (public / "msg.txt").write_text("subject\n\nbody naming quuxcorp\n")
    assert pv.check(bash("git commit -F msg.txt", public), cfg) == "'quuxcorp' in the commit message"
    assert pv.check(bash("git commit --file=msg.txt", public), cfg) is not None
    assert pv.check(bash("git commit -aF msg.txt", public), cfg) is not None
    assert pv.check(bash("git commit -Szorblax -m ok", public), cfg) is None    # a signing key is not published
    assert pv.check(bash("git commit -m 'about zorblax'", private), cfg) is None
    assert pv.check(bash("git log --grep zorblax", public), cfg) is None


def test_gh_texts_follow_the_named_repository(cfg, public, private):
    assert pv.check(bash("gh pr create --title t --body 'for zorblax'", public), cfg) == \
        "'zorblax' in the gh pr text"
    assert pv.check(bash("gh pr create --title t --body 'for zorblax'", private), cfg) is None
    assert pv.check(bash("gh issue create -R example-owner/thing --title=zorblax", private), cfg) is not None
    assert pv.check(bash("gh release create v1 --repo=example-owner/secret -n zorblax", public), cfg) is None
    (private / "body.md").write_text("zorblax\n")
    assert pv.check(bash("gh pr create -R example-owner/thing --body-file body.md", private), cfg) is not None
    assert pv.check(bash("gh pr create -R example-owner/thing --body-file=body.md", private), cfg) is not None


def test_github_calls(cfg):
    call = {"tool_name": "mcp__github__create_issue",
            "tool_input": {"owner": "example-owner", "repo": "thing", "title": "t", "body": "zorblax"}}
    assert pv.check(call, cfg) == "'zorblax' in the GitHub call"
    call["tool_input"]["repo"] = "secret"
    assert pv.check(call, cfg) is None
    path_only = {"tool_name": "mcp__github__get_file_contents",
                 "tool_input": {"owner": "example-owner", "repo": "thing", "path": "zorblax.md"}}
    assert pv.check(path_only, cfg) is None


def test_text_a_command_writes_into_a_public_file(cfg, public, private):
    heredoc = "cat > README.md <<'EOF'\nIntro\nbuilt for quuxcorp\nEOF"
    assert pv.check(bash(heredoc, public), cfg) == "'quuxcorp' in the text written to README.md"
    assert pv.check(bash(heredoc, private), cfg) is None
    assert pv.check(bash("cat <<EOF > notes.md\nzorblax\nEOF\ngit status", public), cfg) is not None
    assert pv.check(bash("tee -a docs.md <<-EOF\n\tzorblax\n\tEOF", public), cfg) is not None
    assert pv.check(bash("echo zorblax > out.txt", public), cfg) is not None
    assert pv.check(bash("printf '%s\\n' quuxcorp >> out.txt", public), cfg) is not None
    assert pv.check(bash("echo zorblax > /dev/null", public), cfg) is None
    assert pv.check(bash("echo zorblax 2> err.txt", public), cfg) is None
    assert pv.check(bash(f"echo zorblax > {public}/x.txt", private), cfg) is not None
    assert pv.check(bash("cat > README.md <<'EOF'\nnothing private\nEOF", public), cfg) is None


# --- what a commit would record ------------------------------------------------------------------

def test_what_a_commit_records_in_a_public_repository(cfg, public):
    (public / "b.txt").write_text("fine\nmade for zorblax\n")
    git(public, "add", "b.txt")
    expected = "'zorblax' in b.txt:2, which this commit would record"
    assert pv.check(bash("git commit -m ok", public), cfg) == expected
    assert pv.check(bash(f"git -C {public} commit -m ok", public.parent), cfg) == expected
    git(public, "rm", "-q", "--cached", "b.txt")
    assert pv.check(bash("git commit -m ok", public), cfg) is None
    assert pv.check(bash("git add b.txt && git commit -m ok", public), cfg) == expected
    assert pv.check(bash("git add -A && git commit -m ok", public), cfg) == expected
    (public / "b.txt").unlink()
    (public / "a.txt").write_text("one\ntwo\nquuxcorp\n")
    assert pv.check(bash("git commit -am ok", public), cfg) == \
        "'quuxcorp' in a.txt:3, which this commit would record"
    assert pv.check(bash("timeout 60 git commit -am ok", public), cfg) is not None
    assert pv.check(bash("git commit --dry-run -am ok", public), cfg) is None


def test_a_file_name_is_recorded_too(cfg, public):
    (public / "zorblax-notes.md").write_text("plain\n")
    git(public, "add", "zorblax-notes.md")
    assert pv.check(bash("git commit -m ok", public), cfg) == \
        "'zorblax' in the file name zorblax-notes.md, which this commit would record"


def test_a_private_or_personal_repository_may_record_its_own_words(cfg, private, personal):
    (private / "b.txt").write_text("zorblax\n")
    git(private, "add", "b.txt")
    assert pv.check(bash("git commit -m ok", private), cfg) is None
    (personal / "b.txt").write_text("janedoe lives in wibbleton\n")
    git(personal, "add", "b.txt")
    assert pv.check(bash("git commit -m ok", personal), cfg) is None
    (personal / "c.txt").write_text("frobnitz\n")
    git(personal, "add", "c.txt")
    assert pv.check(bash("git commit -m ok", personal), cfg) == \
        "'frobnitz' in c.txt:1, which this commit would record"


def test_each_commit_is_checked_against_its_own_repository(cfg, public, private):
    (private / "b.txt").write_text("zorblax\n")
    git(private, "add", "b.txt")
    (public / "b.txt").write_text("clean\n")
    git(public, "add", "b.txt")
    line = f"git -C {private} commit -m ok && git -C {public} commit -m ok"
    assert pv.check(bash(line, public.parent), cfg) is None
    (public / "b.txt").write_text("quuxcorp\n")
    git(public, "add", "b.txt")
    assert pv.check(bash(line, public.parent), cfg) == "'quuxcorp' in b.txt:1, which this commit would record"


# --- the shared parser's additions ---------------------------------------------------------------

def test_parse_keeps_here_documents_and_write_targets():
    cmds = commandline.parse("cat > a.md <<'EOF'\nline one\nEOF\necho hi 2>&1 1>b.txt 2> err | tee -a c.md")
    assert cmds == [
        {"words": ["cat"], "writes": ["a.md"], "heredoc": "line one\n"},
        {"words": ["echo", "hi"], "writes": ["b.txt"], "heredoc": None},
        {"words": ["tee", "-a", "c.md"], "writes": [], "heredoc": None},
    ]
    assert commandline.simple_commands("cat > a.md <<'EOF'\nit's data\nEOF\ngit add a.md") == \
        [["cat"], ["git", "add", "a.md"]]


def test_writes_resolves_targets_from_the_folder_the_command_runs_in(tmp_path):
    (tmp_path / "sub").mkdir()
    got = commandline.writes(f"cd {tmp_path}/sub && cat <<EOF > x.md\nhello\nEOF\necho bye >> /dev/null", "/")
    assert got == [(str(tmp_path / "sub" / "x.md"), "hello\n")]
    assert commandline.writes("ls > list.txt", str(tmp_path)) == []


def test_commit_repos_and_commit_lines_for_one_repository(cfg, public, private):
    (public / "b.txt").write_text("p\n")
    git(public, "add", "b.txt")
    (private / "b.txt").write_text("q\n")
    git(private, "add", "b.txt")
    line = f"git -C {public} commit -m x; git -C {private} commit -m y; git -C {public} commit --dry-run"
    tops = commandline.commit_repos(line, "/")
    assert tops == sorted([str(public.resolve()), str(private.resolve())])
    assert commandline.commit_lines(line, "/", repo=str(private.resolve())) == [("b.txt", 1, "q")]
    assert len(commandline.commit_lines(line, "/")) == 2


# --- the hook as Claude Code runs it -------------------------------------------------------------

def test_main_refuses_with_the_configured_message_and_fails_open(cfg, public, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(write(public / "n.md", "zorblax"))))
    assert pv.main(["--config", cfg.path]) == 2
    err = capsys.readouterr().err
    assert "Blocked: 'zorblax' in n.md would go into a public repository" in err
    assert err.rstrip().endswith("Use invented examples.")
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert pv.main(["--config", cfg.path]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(write(public / "n.md", "zorblax"))))
    assert pv.main(["--config", str(public / "no-config.json")]) == 0


def test_the_cli_offers_the_hook(cfg, public, monkeypatch):
    commands = [h["command"] for e in cli.HOOKS["PreToolUse"] for h in e["hooks"]]
    assert "claude-ops hook public-voice" in commands and "claude-ops hook no-session-links" in commands
    monkeypatch.setenv(pv.CONFIG_ENV, cfg.path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(write(public / "n.md", "quuxcorp"))))
    assert cli.main(["hook", "public-voice"]) == 2
