import json

from claude_ops.hooks import ask_what_you_need as ask
from claude_ops.hooks import context_monitor as cm


def turn(text, asked=False):
    recs = [{"type": "user", "message": {"content": "do it"}}]
    if asked:
        recs.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion"}]}})
        recs.append({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    recs.append({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    return recs


REPORT = "### What I did\nstuff\n\n### What I need from you\nPick red or blue.\n\n### What's next\nmore"


def test_section_is_found_in_any_heading_style():
    assert ask.section_text(REPORT) == "Pick red or blue."
    assert ask.section_text("**What I need from you**\nA key.") == "A key."
    assert ask.section_text("What I need from you:\nA key.\n---\nend") == "A key."
    assert ask.section_text("no such section") is None


def test_blocks_an_unasked_request_only():
    assert "Pick red or blue" in ask.check(turn(REPORT))
    assert ask.check(turn(REPORT, asked=True)) is None
    assert ask.check(turn(REPORT.replace("Pick red or blue.", "Nothing, going on with X."))) is None
    assert ask.check(turn("plain answer")) is None


def test_a_tool_result_is_not_the_user_speaking():
    recs = turn(REPORT, asked=True)
    # the AskUserQuestion happened after the user's message, before a tool result, so it counts
    assert ask.check(recs) is None


def test_custom_heading():
    text = "## Open questions\nWhich port?"
    assert ask.check(turn(text)) is None
    assert ask.check(turn(text), heading="Open questions") is not None


def test_ask_main_fails_open_and_respects_stop_hook_active(monkeypatch, tmp_path, capsys):
    import io
    t = tmp_path / "t.jsonl"
    t.write_text("".join(json.dumps(r) + "\n" for r in turn(REPORT)))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(t)})))
    assert ask.main() == 2
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(t), "stop_hook_active": True})))
    assert ask.main() == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert ask.main() == 0


def setup_monitor(tmp_path, monkeypatch, threshold=100):
    monkeypatch.setattr(cm, "STATE", tmp_path / "state")
    monkeypatch.setattr(cm, "THRESHOLD", threshold)
    t = tmp_path / "t.jsonl"
    t.write_text("".join(json.dumps({"message": {"content": "word " * 50}}) + "\n" for _ in range(20)))
    return {"transcript_path": str(t), "session_id": "s1"}, t


def test_small_transcripts_make_no_api_call(tmp_path, monkeypatch):
    ev, _ = setup_monitor(tmp_path, monkeypatch, threshold=10**9)
    assert cm.check(ev, counter=lambda t: 1 / 0) is None


def test_warns_once_then_again_after_a_compaction(tmp_path, monkeypatch):
    ev, t = setup_monitor(tmp_path, monkeypatch)
    out = cm.check(ev, counter=lambda text: 5000)
    assert "5,000" in out["hookSpecificOutput"]["additionalContext"]
    assert cm.check(ev, counter=lambda text: 5000) is None  # once
    cm.mark(ev)
    assert (tmp_path / "state" / "s1.compact-offset").read_text() == str(t.stat().st_size)
    assert cm.check(ev, counter=lambda text: 1 / 0) is None  # nothing live after the mark
    with t.open("a") as f:
        f.write("".join(json.dumps({"message": {"content": "new " * 50}}) + "\n" for _ in range(20)))
    seen = []
    assert cm.check(ev, counter=lambda text: seen.append(text) or 5000) is not None
    assert "word" not in seen[0] and "new" in seen[0]  # only what came after the compaction


def test_below_threshold_and_a_failed_count_stay_quiet(tmp_path, monkeypatch):
    ev, _ = setup_monitor(tmp_path, monkeypatch)
    assert cm.check(ev, counter=lambda t: 10) is None
    assert cm.check(ev, counter=lambda t: (_ for _ in ()).throw(OSError("offline"))) is None


def test_compact_prompt_is_included(tmp_path, monkeypatch):
    p = tmp_path / "prompt.md"
    p.write_text("KEEP EVERYTHING")
    monkeypatch.setattr(cm, "PROMPT_FILE", str(p))
    assert "KEEP EVERYTHING" in cm.nudge(900_000)
