"""The raise-gate hook: a turn with a failed call to a watched tool may not end until the report says what
was raised about it, and everything else (the session's own mistakes, ordinary shell errors, a pass with a
reason) goes through."""
import io
import json

from claude_ops.hooks import raise_gate as rg

CFG = rg.Config({"heading": "Raised upstream",
                 "tool_prefixes": ["mcp__svc__"],
                 "programs": ["toolA"], "modules": ["tool_a"],
                 "bash_shapes": ["Traceback", "usage:", "Blocked:"],
                 "ignore": ["was not in your prompt", "Blocked: sleep"]})


def user(text="go"):
    return {"type": "user", "message": {"content": text}}


def call(i, name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def result(i, text, error=True):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i,
                                                      "is_error": error, "content": text}]}}


def says(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def turn(*middle, final="All done.\n\nRaised upstream: none, nothing failed."):
    return [user(), *middle, says(final)]


FAULT = [call("a", "mcp__svc__open", item="x"), result("a", "refused: no such item")]


def test_fault_without_a_line_holds():
    why = rg.check(turn(*FAULT, final="Done.\n\nNothing else."), CFG)
    assert why and "mcp__svc__open" in why and "refused: no such item" in why


def test_item_id_passes():
    assert rg.check(turn(*FAULT, final="Done.\n\n7. **Raised upstream** 5e186818 and b3bbefd5"), CFG) is None


def test_none_with_a_reason_passes_and_bare_none_holds():
    assert rg.check(turn(*FAULT, final="Raised upstream: none, my own typo"), CFG) is None
    assert rg.check(turn(*FAULT, final="Raised upstream: none"), CFG)
    assert rg.check(turn(*FAULT, final="Raised upstream: none."), CFG)


def test_word_that_is_not_an_id_holds():
    assert rg.check(turn(*FAULT, final="Raised upstream: decade, facade"), CFG)
    assert rg.check(turn(*FAULT, final="Raised upstream: abcdefab"), CFG)  # no digit


def test_no_fault_no_line_passes():
    assert rg.check(turn(call("a", "mcp__svc__open"), result("a", "ok", error=False), final="Done."), CFG) is None


def test_session_own_mistake_is_ignored():
    mid = [call("a", "mcp__svc__note"), result("a", "note has no argument item. This tool's input schema was not in your prompt")]
    assert rg.check(turn(*mid, final="Done."), CFG) is None


def test_unwatched_tool_and_ordinary_bash_do_not_count():
    mid = [call("a", "Edit"), result("a", "no match"), call("b", "Bash", command="pytest -q"), result("b", "usage: x")]
    assert rg.check(turn(*mid, final="Done."), CFG) is None


def test_watched_program_with_a_shape_counts_and_without_does_not():
    mid = [call("a", "Bash", command="cd /x && toolA frob"), result("a", "usage: toolA frob")]
    assert rg.check(turn(*mid, final="Done."), CFG)
    quiet = [call("a", "Bash", command="toolA frob"), result("a", "1 finding")]
    assert rg.check(turn(*quiet, final="Done."), CFG) is None


def test_module_form_and_wrappers():
    mid = [call("a", "Bash", command="VAR=1 python3 -P -m tool_a.sub --x"), result("a", "Traceback ...")]
    assert rg.check(turn(*mid, final="Done."), CFG)
    other = [call("a", "Bash", command="python3 -m other"), result("a", "Traceback ...")]
    assert rg.check(turn(*other, final="Done."), CFG) is None


def test_a_success_that_prints_a_traceback_is_not_seen():
    mid = [call("a", "Bash", command="toolA x"), result("a", "Traceback ...", error=False)]
    assert rg.check(turn(*mid, final="Done."), CFG) is None


def test_only_the_current_turn_counts():
    recs = [user("first"), *FAULT, says("ok"), user("second"), says("fine")]
    assert rg.check(recs, CFG) is None


def test_no_config_watches_nothing():
    assert rg.check(turn(*FAULT, final="x"), rg.Config({})) is None


def run(tmp_path, monkeypatch, records, cfg=None, active=False, config=True):
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in records))
    c = tmp_path / "c.json"
    c.write_text(json.dumps(cfg or {"heading": "Raised upstream", "tool_prefixes": ["mcp__svc__"]}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(t), "stop_hook_active": active})))
    return rg.main(["--config", str(c)] if config else ["--config", str(tmp_path / "none.json")])


def test_main_prints_feedback_and_exits_zero(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch, turn(*FAULT, final="Done.")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "Raised upstream" in out["hookSpecificOutput"]["additionalContext"]


def test_main_second_stop_missing_config_and_garbage_go_through(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch, turn(*FAULT, final="Done."), active=True) == 0
    assert run(tmp_path, monkeypatch, turn(*FAULT, final="Done."), config=False) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert rg.main([]) == 0
    assert capsys.readouterr().out == ""
