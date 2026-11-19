"""The refused-tools hook: a tool named in the configuration is refused with its message, every other
tool goes through, and a configuration that cannot be read refuses nothing."""
import io
import json

from claude_ops.hooks import refused_tools as rt

MESSAGE = "Ask through the ticket tool instead, with the whole question in it."


def write(tmp_path, body):
    p = tmp_path / "refused.json"
    p.write_text(body if isinstance(body, str) else json.dumps(body))
    return str(p)


def run(monkeypatch, capsys, payload, argv):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = rt.main(argv)
    return rc, capsys.readouterr().err


def test_a_named_tool_is_refused_with_its_message(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, {"refuse": [{"tools": "AskUserQuestion", "message": MESSAGE}]})
    rc, err = run(monkeypatch, capsys, {"tool_name": "AskUserQuestion", "tool_input": {}}, ["--config", cfg])
    assert rc == 2 and err.startswith("Blocked: AskUserQuestion is not used here. ") and MESSAGE in err


def test_every_other_tool_goes_through(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, {"refuse": [{"tools": "AskUserQuestion", "message": MESSAGE}]})
    for name in ("Bash", "AskUserQuestionLater", "mcp__tickets__ask"):
        rc, err = run(monkeypatch, capsys, {"tool_name": name}, ["--config", cfg])
        assert rc == 0 and err == "", name


def test_the_pattern_matches_the_whole_name_and_the_first_entry_wins(tmp_path):
    cfg = write(tmp_path, {"refuse": [{"tools": "mcp__old__.*", "message": "first"},
                                      {"tools": "mcp__old__ask", "message": "second"}]})
    configured = rt.entries(["--config", cfg])
    assert rt.check({"tool_name": "mcp__old__ask"}, configured) == "first"
    assert rt.check({"tool_name": "x_mcp__old__ask"}, configured) is None


def test_the_config_comes_from_the_environment_too(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(rt.CONFIG_ENV, write(tmp_path, {"refuse": [{"tools": "WebSearch", "message": MESSAGE}]}))
    rc, _err = run(monkeypatch, capsys, {"tool_name": "WebSearch"}, [])
    assert rc == 2


def test_a_broken_or_missing_config_refuses_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(rt.CONFIG_ENV, raising=False)
    for argv in ([], ["--config", str(tmp_path / "none.json")], ["--config", write(tmp_path, "{not json")],
                 ["--config", write(tmp_path, {"refuse": [{"tools": "AskUserQuestion"}]})],
                 ["--config", write(tmp_path, {"refuse": [{"tools": "(", "message": MESSAGE}]})]):
        rc, err = run(monkeypatch, capsys, {"tool_name": "AskUserQuestion"}, argv)
        assert rc == 0 and err == "", argv


def test_a_payload_that_is_not_json_goes_through(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert rt.main([]) == 0


def test_it_runs_as_a_chain_step(tmp_path, monkeypatch, capsys):
    from claude_ops.hooks import chain
    cfg = write(tmp_path, {"refuse": [{"tools": "AskUserQuestion", "message": MESSAGE}]})
    chain_cfg = tmp_path / "chain.json"
    chain_cfg.write_text(json.dumps({"steps": [{"module": "claude_ops.hooks.refused_tools", "args": ["--config", cfg],
                                                "tools": "AskUserQuestion"}]}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "AskUserQuestion", "tool_input": {},
                                                              "hook_event_name": "PreToolUse"})))
    rc = chain.main(["--config", str(chain_cfg)])
    out = capsys.readouterr()
    assert rc == 2 and MESSAGE in out.err
