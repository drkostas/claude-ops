"""The open-question hook: a session's open question box is known before its transcript holds it."""
import io
import json

from claude_ops.hooks import open_question as oq

QUESTIONS = [{"question": "Which one?", "header": "Pick", "multiSelect": False,
              "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]}]
SESSION = "0d0e0f10-1111-4222-8333-444455556666"


def pre(call="toolu_1", session=SESSION, **extra):
    return {"hook_event_name": "PreToolUse", "session_id": session, "tool_name": "AskUserQuestion",
            "tool_use_id": call, "tool_input": {"questions": QUESTIONS},
            "transcript_path": f"/tmp/{session}.jsonl", **extra}


def post(call="toolu_1", session=SESSION):
    return {"hook_event_name": "PostToolUse", "session_id": session, "tool_name": "AskUserQuestion",
            "tool_use_id": call, "tool_input": {"questions": QUESTIONS}, "tool_response": {}}


def result(call):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": call, "content": "answered"}]}}


def test_a_box_is_known_from_the_moment_it_opens(tmp_path):
    assert oq.record(pre(), tmp_path) == "kept"
    got = oq.open_question(SESSION, [], state=tmp_path)
    assert got["id"] == "toolu_1" and got["questions"] == QUESTIONS
    assert got["transcript"] == f"/tmp/{SESSION}.jsonl"
    assert (tmp_path / f"{SESSION}.json").stat().st_mode & 0o777 == 0o600


def test_its_result_closes_it(tmp_path):
    oq.record(pre(), tmp_path)
    assert oq.record(post(), tmp_path) == "dropped"
    assert oq.open_question(SESSION, state=tmp_path) is None


def test_a_result_in_the_transcript_closes_it_with_no_post_hook(tmp_path):
    oq.record(pre(), tmp_path)
    assert oq.open_question(SESSION, [result("toolu_0")], state=tmp_path) is not None
    assert oq.open_question(SESSION, [result("toolu_1")], state=tmp_path) is None


def test_the_result_of_an_older_call_keeps_the_newer_one(tmp_path):
    oq.record(pre("toolu_1"), tmp_path)
    oq.record(pre("toolu_2"), tmp_path)
    assert oq.record(post("toolu_1"), tmp_path) is None
    assert oq.open_question(SESSION, state=tmp_path)["id"] == "toolu_2"


def test_a_new_start_or_an_end_drops_it(tmp_path):
    for event in ("SessionStart", "SessionEnd"):
        oq.record(pre(), tmp_path)
        assert oq.record({"hook_event_name": event, "session_id": SESSION}, tmp_path) == "dropped"
        assert oq.open_question(SESSION, state=tmp_path) is None


def test_other_tools_sessions_and_subagents_are_left_alone(tmp_path):
    assert oq.record({**pre(), "tool_name": "Bash"}, tmp_path) is None
    assert oq.record(pre(agent_id="a1"), tmp_path) is None
    assert oq.record(pre(session="../x"), tmp_path) is None
    assert oq.record({**pre(), "tool_input": {}}, tmp_path) is None
    oq.record(pre(session="other"), tmp_path)
    assert oq.open_question(SESSION, state=tmp_path) is None
    assert list(p.name for p in tmp_path.iterdir()) == ["other.json"]


def test_a_broken_file_reads_as_none(tmp_path):
    (tmp_path / f"{SESSION}.json").write_text("{not json")
    assert oq.open_question(SESSION, state=tmp_path) is None


def test_the_hook_never_prints_or_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_OPS_QUESTIONS", str(tmp_path))
    for stdin in ("not json", json.dumps(pre())):
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        assert oq.main() == 0
    assert capsys.readouterr() == ("", "")
    assert oq.open_question(SESSION)["id"] == "toolu_1"
