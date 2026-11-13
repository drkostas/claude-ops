"""The plain-voice hook: GitHub text with a paragraph broken across lines is joined before it is sent,
word for word in the command or the MCP call, or refused naming the saved text when that cannot be
done exactly; text that carries a phrase on the configured list is refused."""
import io
import json
import os
import shlex

import pytest

from claude_ops.hooks import plain_voice as pv

WRAPPED = "Title line\n\nThe first sentence is long enough that it was wrapped\nonto a second line here.\n"


@pytest.fixture(autouse=True)
def saved_here(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "SAVED", str(tmp_path / "saved"))


def bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


JOINED = "Title line\n\nThe first sentence is long enough that it was wrapped onto a second line here.\n"


def joined(command, cwd, cfg=None):
    """(the changed command, the files it now sends) for a call the hook joins; it is refused by none."""
    why, new, note = pv.review(bash(command, cwd), cfg or pv.Config())
    assert why is None and new is not None and "joined before it was sent" in note, (command, why)
    assert not any(pv.markdown.is_wrapped(t) for _w, t in pv.texts_of(bash(new["command"], cwd)))
    return new["command"], [w for w in shlex.split(new["command"].split("\n")[0]) if pv.SAVED in w]


def test_a_wrapped_body_file_is_sent_joined_from_a_saved_file_and_the_callers_file_is_left_alone(tmp_path):
    body = tmp_path / "body.md"
    body.write_text(WRAPPED)
    command, files = joined(f"gh pr create --title T --body-file {body}", tmp_path)
    assert command.startswith("gh pr create --title T --body-file ") and str(body) not in command
    assert open(files[0]).read() == JOINED and body.read_text() == WRAPPED


def test_wrapped_text_in_every_way_gh_takes_a_body_is_joined_word_for_word(tmp_path):
    cases = {
        f"gh pr create --title 'A [draft] title?' --body {shlex.quote(WRAPPED)}":
            "gh pr create --title 'A [draft] title?' --body-file ",
        f"gh pr edit 4 -b {shlex.quote(WRAPPED)}": "gh pr edit 4 --body-file ",
        f"gh release create v1 --notes={shlex.quote(WRAPPED)}": "gh release create v1 --notes-file=",
        f"gh issue comment 3 --body-file - <<'EOF'\n{WRAPPED}EOF": "gh issue comment 3 --body-file ",
        f"gh api repos/someone/demo/issues/3/comments -f body={shlex.quote(WRAPPED)}":
            "gh api repos/someone/demo/issues/3/comments -F body=@",
    }
    for command, start in cases.items():
        new, files = joined(command, tmp_path)
        assert new.startswith(start), (command, new)
        saved = files[0].split("=@")[-1].split("=")[-1]
        assert open(saved).read() == JOINED, command


def test_code_lists_and_one_line_paragraphs_are_kept_as_they_are_when_the_rest_is_joined(tmp_path):
    text = ("Title\n\nA long paragraph that someone wrapped at seventy-two columns for\nno reason at all here.\n\n"
            "- an item\n- another item\n\n```\ncode line one\ncode line two\n```\n")
    _, files = joined(f"gh pr comment 1 --body {shlex.quote(text)}", tmp_path)
    assert open(files[0]).read() == ("Title\n\nA long paragraph that someone wrapped at seventy-two columns for "
                                     "no reason at all here.\n\n- an item\n- another item\n\n"
                                     "```\ncode line one\ncode line two\n```\n")


def test_a_joined_text_that_still_has_a_listed_phrase_is_refused(tmp_path):
    cfg = pv.Config([{"regex": r"\w: ", "why": "a colon in prose"}])
    text = "Done.\n\nThe check reads the long sentence that a person wrapped here and it says\nthis: a colon.\n"
    why, new, _ = pv.review(bash(f"gh pr comment 1 --body {shlex.quote(text)}", tmp_path), cfg)
    assert new is None and "a colon in prose" in why and "this: a colon." in why


def test_a_command_that_cannot_be_changed_exactly_is_refused_with_the_saved_text(tmp_path):
    for command in (f"cd {tmp_path} && gh pr comment 1 --body {shlex.quote(WRAPPED)}",
                    f"gh issue comment 3 --body-file - <<EOF\n{WRAPPED}EOF",
                    f"gh pr comment 1 --body {shlex.quote(WRAPPED)} | cat"):
        why, new, _ = pv.review(bash(command, tmp_path), pv.Config())
        assert new is None and "broken across lines" in why and "saved in " in why, command
        saved = why.split("saved in ")[1].split(";")[0]
        assert open(saved).read() == JOINED


def test_a_body_with_one_line_per_paragraph_and_a_commit_message_pass(tmp_path):
    flat = "Title line\n\nOne paragraph on one line.\n\n- an item\n- another\n\n```\ncode\nmore code\n```\n"
    assert pv.check(bash(f"gh pr create --title T --body {shlex.quote(flat)}", tmp_path), pv.Config()) is None
    assert pv.check(bash(f"git commit -m {shlex.quote(WRAPPED)}", tmp_path), pv.Config()) is None


def test_a_github_mcp_call_that_writes_is_joined_and_a_read_is_left_alone():
    write = {"tool_name": "mcp__github__add_issue_comment", "tool_input": {"owner": "a", "repo": "b", "body": WRAPPED}}
    read = {"tool_name": "mcp__github__get_file_contents", "tool_input": {"owner": "a", "repo": "b", "path": WRAPPED}}
    why, new, _ = pv.review(write, pv.Config())
    assert why is None and new == {"owner": "a", "repo": "b", "body": JOINED}
    assert pv.review(read, pv.Config()) == (None, None, "")


def test_a_listed_phrase_in_the_prose_is_refused_and_code_urls_and_allowed_text_are_not(tmp_path):
    cfg = pv.Config([{"regex": "—", "why": "an em-dash"}, {"regex": r"\w: ", "why": "a colon in prose"}],
                    [r"\b\d{1,2}:\d{2}\b"])
    say = lambda text: pv.check(bash(f"gh pr comment 1 --body {shlex.quote(text)}", tmp_path), cfg)  # noqa: E731
    assert "an em-dash" in say("One thing — another.")
    assert "a colon in prose" in say("Checked: the suite passed.")
    assert say("It ran at 13:58 and passed.") is None
    assert say("See `a: b` and https://example.org/x: y and [a](https://example.org/a:b).") is None
    assert say("```\nkey: value\n```") is None
    assert say("| a: b | c |") is None


def test_a_gh_api_json_input_is_judged_by_its_title_and_body_only(tmp_path):
    cfg = pv.Config([{"regex": r"\w: ", "why": "a colon in prose"}])
    say = lambda command: pv.check(bash(command, tmp_path), cfg)  # noqa: E731
    pr = {"title": "One plain title", "head": "a-branch", "base": "main", "body": "One paragraph.\n\nAnd a second."}
    f = tmp_path / "pr.json"
    f.write_text(json.dumps(pr))                  # json.dumps writes '"title": "One plain title"'
    assert '"title": ' in f.read_text()
    assert say(f"gh api repos/someone/demo/pulls --input {f}") is None
    f.write_text(json.dumps({**pr, "body": "The fix: it works."}))
    assert "a colon in prose" in say(f"gh api repos/someone/demo/pulls --input {f}")
    f.write_text(json.dumps({**pr, "title": "Note: a title"}))
    assert "a colon in prose" in say(f"gh api repos/someone/demo/pulls --input {f}")
    heredoc = "gh api repos/someone/demo/pulls --input - <<'EOF'\n{}\nEOF"
    assert say(heredoc.format(json.dumps(pr))) is None
    assert "a colon in prose" in say(heredoc.format(json.dumps({**pr, "body": "Why: this."})))
    notes = tmp_path / "notes.txt"
    notes.write_text("Checked: the suite passed.\n")
    assert "a colon in prose" in say(f"gh api repos/someone/demo/issues/3/comments --input {notes}")
    assert pv.shown(json.dumps(pr)) == [pr["title"], pr["body"]] and pv.shown("[1, 2]") == ["[1, 2]"]


def test_the_patterns_come_from_the_file(tmp_path, monkeypatch):
    f = tmp_path / "plain.json"
    f.write_text(json.dumps({"patterns": [{"regex": "—", "why": "an em-dash"}], "allow": ["x"]}))
    cfg = pv.load(["--config", str(f)])
    assert [p.pattern for p, _ in cfg.patterns] == ["—"] and [a.pattern for a in cfg.allow] == ["x"]
    monkeypatch.setenv("CLAUDE_OPS_PLAIN_VOICE_CONFIG", str(tmp_path / "missing.json"))
    assert pv.load([]).patterns == []


def test_main_joins_refuses_with_exit_2_and_fails_open(tmp_path, monkeypatch, capsys):
    payload = bash(f"gh pr create --title T --body {shlex.quote(WRAPPED)}", tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert pv.main(["--config", os.devnull]) == 0
    spec = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert spec["updatedInput"]["command"].startswith("gh pr create --title T --body-file ")
    assert "permissionDecision" not in spec and "joined before it was sent" in spec["additionalContext"]
    cfg = tmp_path / "plain.json"
    cfg.write_text(json.dumps({"patterns": [{"regex": "—", "why": "an em-dash"}]}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bash("gh pr comment 1 --body 'One — two.'", tmp_path))))
    assert pv.main(["--config", str(cfg)]) == 2 and "an em-dash" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert pv.main([]) == 0


def test_a_file_is_checked_with_file_and_an_unreadable_one_is_refused(tmp_path, capsys):
    cfg = tmp_path / "plain.json"
    cfg.write_text(json.dumps({"patterns": [{"regex": "—", "why": "an em-dash"}]}))
    good, bad = tmp_path / "good.md", tmp_path / "bad.md"
    good.write_text("Title\n\nOne paragraph on one line.\n")
    bad.write_text("Title\n\nOne thing — another.\n")
    assert pv.main(["--config", str(cfg), "--file", str(good)]) == 0
    assert pv.main(["--config", str(cfg), "--file", str(bad)]) == 2 and "an em-dash" in capsys.readouterr().err
    assert pv.main(["--file", str(tmp_path / "missing.md")]) == 2 and "could not be read" in capsys.readouterr().err
    assert pv.check_text(WRAPPED, pv.Config(), "the body").startswith("the body has a paragraph broken")
