"""The plain-voice hook: GitHub text is refused when a paragraph is broken across lines, or when it
carries a phrase on the configured list, and the joined text is saved for the model to send."""
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


def test_a_wrapped_pr_body_is_refused_and_its_joined_text_saved(tmp_path):
    body = tmp_path / "body.md"
    body.write_text(WRAPPED)
    why = pv.check(bash(f"gh pr create --title T --body-file {body}", tmp_path), pv.Config())
    assert why and "broken across lines" in why and "onto a second line here." in why
    saved = why.split("saved in ")[1].split(";")[0]
    assert open(saved).read() == ("Title line\n\nThe first sentence is long enough that it was wrapped "
                                  "onto a second line here.\n")


def test_wrapped_text_in_every_way_gh_takes_a_body_is_refused(tmp_path):
    for command in (f"gh pr create --title T --body {shlex.quote(WRAPPED)}",
                    f"gh issue comment 3 --body-file - <<'EOF'\n{WRAPPED}EOF",
                    f"gh pr edit 4 -b {shlex.quote(WRAPPED)}",
                    f"gh api repos/someone/demo/issues/3/comments -f body={shlex.quote(WRAPPED)}"):
        assert pv.check(bash(command, tmp_path), pv.Config()), command


def test_a_body_with_one_line_per_paragraph_and_a_commit_message_pass(tmp_path):
    flat = "Title line\n\nOne paragraph on one line.\n\n- an item\n- another\n\n```\ncode\nmore code\n```\n"
    assert pv.check(bash(f"gh pr create --title T --body {shlex.quote(flat)}", tmp_path), pv.Config()) is None
    assert pv.check(bash(f"git commit -m {shlex.quote(WRAPPED)}", tmp_path), pv.Config()) is None


def test_a_github_mcp_call_that_writes_is_checked_and_a_read_is_not():
    write = {"tool_name": "mcp__github__add_issue_comment", "tool_input": {"owner": "a", "repo": "b", "body": WRAPPED}}
    read = {"tool_name": "mcp__github__get_file_contents", "tool_input": {"owner": "a", "repo": "b", "path": WRAPPED}}
    assert pv.check(write, pv.Config()) and pv.check(read, pv.Config()) is None


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


def test_the_patterns_come_from_the_file(tmp_path, monkeypatch):
    f = tmp_path / "plain.json"
    f.write_text(json.dumps({"patterns": [{"regex": "—", "why": "an em-dash"}], "allow": ["x"]}))
    cfg = pv.load(["--config", str(f)])
    assert [p.pattern for p, _ in cfg.patterns] == ["—"] and [a.pattern for a in cfg.allow] == ["x"]
    monkeypatch.setenv("CLAUDE_OPS_PLAIN_VOICE_CONFIG", str(tmp_path / "missing.json"))
    assert pv.load([]).patterns == []


def test_main_refuses_with_exit_2_and_fails_open(tmp_path, monkeypatch, capsys):
    payload = bash(f"gh pr create --title T --body {shlex.quote(WRAPPED)}", tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert pv.main(["--config", os.devnull]) == 2 and "broken across lines" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert pv.main([]) == 0
