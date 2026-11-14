"""The clock-ahead hook: a time of day written a few minutes ahead of the clock is refused in the text a
configured tool or command carries, and a time still to come, a time on another day or in another zone,
code, quotes and everything outside the window go through."""
import datetime as dt
import io
import json

import pytest

from claude_ops import cli
from claude_ops.hooks import chain
from claude_ops.hooks import clock_ahead as ca

NOW = dt.datetime(2026, 3, 9, 13, 17, 30)
CONFIG = {"tools": {"mcp__notes__add": ["text", "subject"]},
          "commands": [{"program": "notes.sh", "flags": ["--text", "--evidence"]}],
          "messages": True,
          "zones": ["UTC", "GMT", "Z", "EET"],
          "allow": [r"\bvalid_for \d{1,2}:\d{2}(?::\d{2})?\b"]}


def cfg(**more):
    return ca.Config({**CONFIG, **more})


def hits(text, now=NOW, **more):
    return [w for w, _m in ca.ahead(text, now, cfg(**more))]


def mcp(text, key="text", tool="mcp__notes__add"):
    return {"tool_name": tool, "tool_input": {key: text, "when": "13:40"}, "cwd": "/"}


def bash(command, cwd="/"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


@pytest.mark.parametrize("text, found", [
    ("The queue was empty (read at 13:22).", ["13:22"]),
    ("There were 22 restarts in the day before 13:20.", ["13:20"]),
    ("At 13:19:40 the list had it.", ["13:19:40"]),
    ("worked 13:00 to 13:25 on it", ["13:25"]),
    ("on 9 Mar 13:30 it ran", ["13:30"]),
    ("2026-03-09T13:30 then", ["13:30"]),
    ("it stopped at 13:20. Then at 13:31 it ran", ["13:20", "13:31"]),
])
def test_a_time_a_few_minutes_ahead_is_found(text, found):
    assert hits(text) == found


@pytest.mark.parametrize("text", [
    "At 13:18 the list had it.",                        # under a minute ahead
    "At 13:10 the list had it.",                        # behind the clock
    "The next try is after 13:49 or at a newer commit.",
    "between 13:20 and 13:30 it waits",
    "It waits until 13:25. If it is still red at 13:28, it stops.",
    "it is due 13:25",
    "A check is set for 13:30.",
    "at 13:22Z, 13:22 UTC, 13:22 EET, 13:22 -03 and 13:22+02:00",
    "2026-03-09T13:22:00+02:00 then",
    "modified: 2026-03-09T13:22:00.000Z",
    "UPDATE 9 Mar 13:22 (+02), it ran",
    "on 10 Mar 13:30 it runs",
    "Mar 8, 13:30 was the last",
    "2026-03-10T13:30 then",
    'the log says "13:40 waiting for the lock"',
    "the line `13:40 waiting` reads",
    "    13:25 an indented log line",
    "```\n13:25 in a fenced block\n```",
    "it was written at 13:06, not 13:22",
    "rows carry valid_for 13:30:00 now",
    "a plan for 14:45, an hour on",                      # past the window
    "23:58 was the last line",
    "a version 1.13:20 or a path a/13:20 or 2:13:20",
])
def test_what_is_left_alone(text):
    assert hits(text) == []


def test_the_window_is_the_configuration():
    assert hits("read at 13:40") == []                      # 22.5 minutes ahead, past the default 15
    assert hits("read at 13:40", max_minutes=30) == ["13:40"]
    assert hits("read at 13:19", min_minutes=5) == []
    assert hits("a plan for 14:45", max_minutes=90) == ["14:45"]


def test_just_after_midnight_the_evening_is_behind():
    late = dt.datetime(2026, 3, 9, 0, 5)
    assert hits("the last line was at 23:58", now=late) == []
    assert hits("the first line was at 00:09", now=late) == ["00:09"]


def test_forward_words_and_zones_come_from_the_configuration():
    assert hits("a check at 13:25", forward_words=["a check at"]) == []
    assert hits("read it until 13:25", forward_words=["after"]) == ["13:25"]
    assert hits("13:25 EET", zones=["UTC"]) == ["13:25"]


def test_the_texts_of_a_tool_call():
    c = cfg()
    assert ca.texts(mcp("read at 13:22"), c) == ["read at 13:22"]
    assert ca.texts(mcp("x", tool="mcp__other__add"), c) == []
    assert ca.texts({"tool_name": "mcp__notes__add", "tool_input": {"text": True}}, c) == []
    assert ca.texts({"tool_name": "mcp__notes__add", "tool_input": {"subject": [{"q": "at 13:22", "n": 2}]}},
                    c) == ["at 13:22"]
    assert ca.texts(bash("~/bin/notes.sh add --text 'read at 13:22' --evidence=done --other 13:24"), c) == [
        "read at 13:22", "done"]
    assert ca.texts(bash("echo --text 13:22"), c) == []


def test_check_refuses_with_the_time_and_the_clock():
    c = cfg()
    why = ca.check(mcp("The queue was empty (read at 13:22)."), c, now=NOW)
    assert why.startswith("13:22 is 4 minutes ahead of the clock, which reads 13:17.")
    assert "\n" not in why
    assert ca.check(mcp("read at 13:22", key="when"), c, now=NOW) is None      # `when` is never read
    assert ca.check(mcp("read at 13:22", key="subject"), c, now=NOW) is not None
    assert ca.check(bash("notes.sh add --text 'at 13:21 and 13:22'"), c, now=NOW).endswith(
        "(and 1 more). Read the clock and write the time it gives, or put a word such as after or until "
        "before a time still to come.")
    assert ca.check(mcp("read at 13:22"), ca.Config(), now=NOW) is None        # no configuration, no check


def test_a_commit_message_and_a_pull_request_body(tmp_path):
    msg = tmp_path / "msg.txt"
    msg.write_text("Fix the reader\n\nThe queue was empty at 13:22.\n")
    c = cfg()
    assert ca.check(bash(f"git -C {tmp_path} commit -F {msg}", tmp_path), c, now=NOW) is not None
    assert ca.check(bash("gh pr create --title t --body 'empty at 13:22'", tmp_path), c, now=NOW) is not None
    assert ca.check(bash("gh pr create --title t --body 'empty at 13:22'", tmp_path), cfg(messages=False),
                    now=NOW) is None


def test_what_a_write_adds_to_a_watched_file(tmp_path):
    notes = tmp_path / "memory"
    notes.mkdir()
    note = notes / "state.md"
    note.write_text("# State\n\nThe run ended at 13:22 on the day before.\n")
    c = cfg(files=[f"{notes}/*.md"])
    old_line = "The run ended at 13:22 on the day before."
    write = {"tool_name": "Write", "tool_input": {"file_path": str(note),
                                                  "content": f"# State\n\n{old_line}\n"}}
    assert ca.check(write, c, now=NOW) is None                    # a line the note holds is not judged again
    write["tool_input"]["content"] += "The queue was empty at 13:22.\n"
    assert ca.texts(write, c) == ["The queue was empty at 13:22."]
    assert ca.check(write, c, now=NOW).startswith("13:22 is 4 minutes ahead")
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(note), "old_string": old_line,
                                                "new_string": f"{old_line}\nRead at 13:21."}}
    assert ca.check(edit, c, now=NOW) is not None
    multi = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(note), "edits": [
        {"old_string": "# State", "new_string": "# State, read at 13:12"},
        {"old_string": old_line, "new_string": "It waits until 13:25."}]}}
    assert ca.check(multi, c, now=NOW) is None
    multi["tool_input"]["edits"].append({"old_string": "x", "new_string": "seen at 13:24"})
    assert ca.check(multi, c, now=NOW) is not None
    elsewhere = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "other.md"),
                                                      "content": "seen at 13:22"}}
    assert ca.check(elsewhere, c, now=NOW) is None                # not a watched file
    assert ca.check(edit, cfg(), now=NOW) is None                 # no `files`, no file is read
    assert not ca.Config({"files": ["~/notes/*"]}).empty


def test_what_a_bash_line_writes_into_a_watched_file(tmp_path):
    notes = tmp_path / "memory"
    notes.mkdir()
    c = cfg(files=[f"{notes}/*"])
    here = f"cat >> {notes}/state.md <<'EOF'\nThe queue was empty at 13:22.\nEOF"
    assert ca.check(bash(here, tmp_path), c, now=NOW) is not None
    assert ca.check(bash(f"echo 'read at 13:22' >> {notes}/state.md", tmp_path), c, now=NOW) is not None
    assert ca.check(bash(f"cd {notes} && printf 'read at 13:22' > state.md", tmp_path), c, now=NOW) is not None
    assert ca.check(bash(f"echo 'read at 13:22' >> {tmp_path}/other.md", tmp_path), c, now=NOW) is None
    assert ca.check(bash(f"echo 'after 13:22 it runs' >> {notes}/state.md", tmp_path), c, now=NOW) is None


def _ahead_now(minutes):
    """A time of day `minutes` ahead of the real clock, or a skip when that crosses midnight."""
    now = dt.datetime.now()
    t = now + dt.timedelta(minutes=minutes)
    if t.date() != now.date():
        pytest.skip("the clock is too close to midnight")
    return t.strftime("%H:%M")


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "clock-ahead.json"
    path.write_text(json.dumps(CONFIG))
    return path


def test_main_reads_the_config_and_says_blocked(config, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(mcp(f"read at {_ahead_now(5)}"))))
    assert ca.main(["--config", str(config)]) == 2
    assert capsys.readouterr().err.startswith("Blocked: ")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(mcp(f"after {_ahead_now(5)}"))))
    assert ca.main(["--config", str(config)]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert ca.main(["--config", str(config)]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(mcp(f"read at {_ahead_now(5)}"))))
    assert ca.main(["--config", str(config.parent / "missing.json")]) == 0
    monkeypatch.setenv(ca.CONFIG_ENV, str(config))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(mcp(f"read at {_ahead_now(5)}"))))
    assert ca.main([]) == 2


def test_the_cli_and_the_chain_offer_the_hook(config, monkeypatch):
    assert any(h.get("matcher") == "Bash|Write|Edit|MultiEdit|mcp__.*" and h["hooks"][0]["command"] == "claude-ops hook clock-ahead"
               for h in cli.HOOKS["PreToolUse"])
    monkeypatch.setenv(ca.CONFIG_ENV, str(config))
    call = json.dumps(mcp(f"read at {_ahead_now(5)}"))
    monkeypatch.setattr("sys.stdin", io.StringIO(call))
    assert cli.main(["hook", "clock-ahead"]) == 2
    rc, out, err = chain.run_module("claude_ops.hooks.clock_ahead", ["--config", str(config)], call)
    assert rc == 2 and err.startswith("Blocked:") and out == ""
    rc, out, err = chain.run_module("claude_ops.hooks.clock_ahead", ["--config", str(config)],
                                    json.dumps(mcp("nothing timed")))
    assert rc == 0 and err == ""
