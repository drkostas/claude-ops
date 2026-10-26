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


def test_a_file_outside_every_repository_written_from_a_public_one(cfg, public, tmp_path):
    # a scratch file named through a variable the line assigned is judged where it goes, not as a
    # file under the public repository the command runs in
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    line = f"S={scratch} && cat > $S/run.mjs <<'EOF'\nconst who = 'zorblax';\nEOF"
    assert pv.check(bash(line, public), cfg) is None
    assert pv.check(bash(f"cd {public} && export S={scratch}; echo zorblax > \"$S/out.txt\"", tmp_path), cfg) is None
    assert pv.check(bash(f"S={scratch}; tee $S/t.txt <<EOF\nquuxcorp\nEOF", public), cfg) is None
    assert pv.check(write(scratch / "run.mjs", "const who = 'zorblax';"), cfg) is None
    # the same variable naming a folder inside the public repository is still refused
    assert pv.check(bash(f"S={public}/docs && cat > $S/n.md <<'EOF'\nzorblax\nEOF", tmp_path), cfg) == \
        "'zorblax' in the text written to n.md"
    # and a variable the line did not assign is still read as a path under the current folder
    assert pv.check(bash("cat > $UNSET_IN_THIS_TEST/n.md <<'EOF'\nzorblax\nEOF", public), cfg) is not None


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
        {"words": ["cat"], "writes": ["a.md"], "heredoc": "line one\n", "piped": False},
        {"words": ["echo", "hi"], "writes": ["b.txt"], "heredoc": None, "piped": False},
        {"words": ["tee", "-a", "c.md"], "writes": [], "heredoc": None, "piped": True},
    ]
    assert commandline.simple_commands("cat > a.md <<'EOF'\nit's data\nEOF\ngit add a.md") == \
        [["cat"], ["git", "add", "a.md"]]


def test_writes_resolves_targets_from_the_folder_the_command_runs_in(tmp_path):
    (tmp_path / "sub").mkdir()
    got = commandline.writes(f"cd {tmp_path}/sub && cat <<EOF > x.md\nhello\nEOF\necho bye >> /dev/null", "/")
    assert got == [(str(tmp_path / "sub" / "x.md"), "hello\n")]
    assert commandline.writes("ls > list.txt", str(tmp_path)) == []
    # a target named through a variable the line assigned goes where the variable points
    got = commandline.writes(f"D={tmp_path}/sub && cat > $D/y.md <<EOF\nhi\nEOF", "/elsewhere")
    assert got == [(str(tmp_path / "sub" / "y.md"), "hi\n")]
    got = commandline.writes(f"export D={tmp_path}; echo hi | tee \"$D/t.txt\"; echo yo > ${{D}}/e.txt", "/")
    assert got == [(str(tmp_path / "t.txt"), "hi"), (str(tmp_path / "e.txt"), "yo")]


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


# --- shapes found in review ----------------------------------------------------------------------

def remote_repo(tmp_path, name):
    """A repository whose origin is a bare folder (fetch) and whose push URL names the public slug.
    The hook only reads that URL; nothing is ever pushed to it."""
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    r = make_repo(tmp_path, name, "example-owner/thing")
    git(r, "remote", "set-url", "origin", str(bare))
    git(r, "push", "-q", "-u", "origin", "HEAD")
    git(r, "remote", "set-url", "--push", "origin", "https://github.com/example-owner/thing.git")
    return r


def test_messages_given_the_ways_claude_code_and_people_give_them(cfg, public, tmp_path):
    std = "git commit -m \"$(cat <<'EOF'\nAdd a thing\n\nmade for zorblax\nEOF\n)\""
    assert pv.check(bash(std, public), cfg) == "'zorblax' in the commit message"
    assert pv.check(bash("gh pr create --title t --body \"$(cat <<'EOF'\n- for zorblax\nEOF\n)\"", public), cfg) == \
        "'zorblax' in the gh pr text"
    assert pv.check(bash("git commit -F - <<'EOF'\nquuxcorp\nEOF", public), cfg) == "'quuxcorp' in the commit message"
    assert pv.check(bash("gh pr create --title t --body-file - <<'EOF'\nquuxcorp\nEOF", public), cfg) is not None
    msg = tmp_path / "m.txt"
    assert pv.check(bash(f"cat > {msg} <<'EOF'\nzorblax subject\nEOF\ngit commit -F {msg}", public), cfg) == \
        "'zorblax' in the commit message"
    (public / "msg.txt").write_text("about quuxcorp\n")
    assert pv.check(bash('git commit -m "$(cat msg.txt)"', public), cfg) == "'quuxcorp' in the commit message"
    assert pv.check(bash("git commit --allow-empty -m ok --trailer 'Refs: zorblax'", public), cfg) is not None
    assert pv.check(bash("git merge --no-ff -m 'merge zorblax' side", public), cfg) == "'zorblax' in the merge message"
    assert pv.check(bash("git notes add -m zorblax", public), cfg) == "'zorblax' in the notes message"
    assert pv.check(bash("git commit -m $'subject\\n\\nzorblax body'", public), cfg) is not None


def test_wrappers_and_comments(cfg, public):
    assert pv.check(bash("rtk proxy git commit -m zorblax", public), cfg) == "'zorblax' in the commit message"
    assert pv.check(bash("timeout 60 git commit -m zorblax", public), cfg) is not None
    assert pv.check(bash("timeout 30 gh pr create --body zorblax", public), cfg) is not None
    (public / "a.txt").write_text("one\ntwo\nzorblax\n")              # changed, not staged
    assert pv.check(bash("timeout 60 git commit -qm ok", public), cfg) is None
    assert pv.check(bash("echo hi > f.md  # zorblax comes later", public), cfg) is None
    assert pv.check(bash("git commit -qm ok  # don't forget\ngit status", public), cfg) is None


def test_the_command_is_read_when_a_part_of_it_cannot_be(cfg, public):
    got = pv.check(bash('MSG="for zorblax"; git commit -m "$MSG"', public), cfg)
    assert got.startswith("'zorblax' in the command, which publishes to a public repository")
    assert pv.check(bash("bash -c 'git commit -m zorblax'", public), cfg) is not None
    assert pv.check(bash(f"git -C {public} commit -m 'plain words'", public.parent), cfg) is None


def test_more_gh_forms(cfg, public, private):
    assert pv.check(bash("gh -R example-owner/thing pr create --title t --body zorblax", private), cfg) is not None
    assert pv.check(bash("gh pr -R example-owner/thing create --title t --body zorblax", private), cfg) is not None
    assert pv.check(bash("GH_REPO=example-owner/thing gh issue create --body zorblax", private), cfg) is not None
    assert pv.check(bash("gh api repos/example-owner/thing/issues -f title=t -f body=zorblax", private), cfg) == \
        "'zorblax' in the gh api call"
    assert pv.check(bash("gh api -X GET repos/example-owner/thing/issues -f q=zorblax", private), cfg) is None
    assert pv.check(bash("gh api repos/example-owner/secret/issues -f body=zorblax", public), cfg) is None
    assert pv.check(bash("gh repo edit example-owner/thing --description 'zorblax tools'", private), cfg) == \
        "'zorblax' in the gh repo text"
    assert pv.check(bash("gh repo create example-owner/brand-new --public -d zorblax", private), cfg) is not None
    assert pv.check(bash("gh repo create example-owner/brand-new --private -d zorblax", private), cfg) is None
    assert pv.check(bash("gh pr merge 3 --merge --subject zorblax", public), cfg) is not None
    assert pv.check(bash("gh pr checkout 12 -b zorblax-fix", public), cfg) is None      # a local branch
    assert pv.check(bash("gh issue list -m zorblax", public), cfg) is None              # a read


def test_more_write_forms(cfg, public, tmp_path):
    assert pv.check(bash("echo zorblax | tee notes.md", public), cfg) == "'zorblax' in the text written to notes.md"
    assert pv.check(bash("cat <<'EOF' | tee -a notes.md\nquuxcorp\nEOF", public), cfg) is not None
    assert pv.check(bash("cat > notes.md <<'END-DOC'\nit's for zorblax\nEND-DOC", public), cfg) is not None
    assert pv.check(bash("psql <<'EOF' > out.txt\nselect 'zorblax';\nEOF", public), cfg) is None
    assert pv.check(bash("echo zorblax > .git/info/exclude", public), cfg) is None
    assert pv.check(write(public / ".git" / "hooks" / "pre-commit", "zorblax"), cfg) is None
    (public / ".gitignore").write_text(".env.local\nnode_modules/\n")
    assert pv.check(write(public / ".env.local", "TOKEN_FOR=zorblax"), cfg) is None
    assert pv.check(write(public / "node_modules" / "x" / "index.js", "zorblax"), cfg) is None
    new = tmp_path / "brand-new-folder"
    assert pv.check(bash(f"mkdir -p {new} && cd {new} && echo zorblax > notes.md", public), cfg) is None


def test_more_commit_forms(cfg, public):
    (public / ".gitignore").write_text("local.md\n")
    git(public, "add", ".gitignore")
    git(public, "commit", "-qm", "ignore")
    (public / "local.md").write_text("zorblax\n")
    assert pv.check(bash("git add local.md && git commit -qm ok", public), cfg) is None    # git refuses that add
    assert pv.check(bash("git add -f local.md && git commit -qm ok", public), cfg) == \
        "'zorblax' in local.md:1, which this commit would record"
    (public / "quuxcorp").mkdir()
    (public / "quuxcorp" / ".gitkeep").write_text("")
    assert pv.check(bash("git add quuxcorp/.gitkeep && git commit -qm ok", public), cfg) == \
        "'quuxcorp' in the file name quuxcorp/.gitkeep, which this commit would record"
    (public / "other.md").write_text("zorblax\n")
    git(public, "add", "other.md")
    (public / "a.txt").write_text("one\ntwo\nthree\nfour\n")
    assert pv.check(bash("git commit -qm ok a.txt", public), cfg) is None      # other.md is not committed
    assert pv.check(bash("git commit -qm ok -i a.txt", public), cfg) is not None


def test_a_push_reads_the_commits_no_remote_has(cfg, tmp_path):
    r = remote_repo(tmp_path, "pushed")
    git(r, "commit", "-q", "--allow-empty", "-m", "clean")
    assert pv.check(bash("git push", r), cfg) is None
    (r / "gen.md").write_text("generated for zorblax\n")      # as a script would, out of sight
    git(r, "add", "gen.md")
    git(r, "commit", "-qm", "generated")
    assert pv.check(bash("git push", r), cfg) == "'zorblax' in gen.md:1, which this push would publish"
    assert pv.check(bash("git push origin HEAD:main", r), cfg) is not None
    assert pv.check(bash("git push --dry-run", r), cfg) is None
    assert pv.check(bash("git push origin --delete old-branch", r), cfg) is None
    r2 = remote_repo(tmp_path, "pushed-message")
    git(r2, "commit", "-q", "--allow-empty", "-m", "about quuxcorp")
    assert pv.check(bash("git push", r2), cfg) == "'quuxcorp' in a commit message, which this push would publish"
    r3 = remote_repo(tmp_path, "pushed-private")
    git(r3, "remote", "set-url", "--push", "origin", "https://github.com/example-owner/secret.git")
    git(r3, "commit", "-q", "--allow-empty", "-m", "about quuxcorp")
    assert pv.check(bash("git push", r3), cfg) is None


def test_github_calls_that_only_read_and_a_branch_that_is_public(cfg):
    read = {"tool_name": "mcp__github__get_file_contents",
            "tool_input": {"owner": "example-owner", "repo": "thing", "ref": "feat/zorblax"}}
    assert pv.check(read, cfg) is None
    branch = {"tool_name": "mcp__github__create_branch",
              "tool_input": {"owner": "example-owner", "repo": "thing", "branch": "zorblax-notes"}}
    assert pv.check(branch, cfg) == "'zorblax' in the GitHub call"


def test_a_single_term_written_without_its_list(tmp_path):
    (tmp_path / "c.json").write_text(json.dumps({"terms": "zorblax", "allow": "zorblax-public"}))
    c = pv.load(str(tmp_path / "c.json"))
    assert c.terms == ["zorblax"] and c.allow == ["zorblax-public"]
