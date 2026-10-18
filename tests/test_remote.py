"""Which Remote Control session a transcript belongs to, and the claude.ai rename script."""
import json

from claude_ops import remote, update


def test_the_newest_bridge_session_names_the_row(tmp_path):
    t = tmp_path / "c.jsonl"
    t.write_text('{"bridgeSessionId": "cse_OLD1"}\n{"type": "user"}\n{"bridgeSessionId":"cse_NEW2"}\n')
    assert remote.remote_id(t) == "session_NEW2"
    assert remote.remote_id(tmp_path / "missing.jsonl") is None and remote.remote_id(None) is None
    (tmp_path / "plain.jsonl").write_text('{"type": "user"}\n')
    assert remote.remote_id(tmp_path / "plain.jsonl") is None


def test_only_the_tail_is_read(tmp_path):
    t = tmp_path / "c.jsonl"
    t.write_text('{"bridgeSessionId": "cse_FAR"}\n' + "x" * 5000 + "\n")
    assert remote.remote_id(t, tail=1000) is None and remote.remote_id(t) == "session_FAR"


def test_the_rename_script_cannot_be_broken_out_of():
    js = remote.rename_js('session_A"); alert(1); ("', 'name "quoted"')
    assert json.dumps('session_A"); alert(1); ("') in js and json.dumps('name "quoted"') in js
    assert "const ID = " in js and "Rename" in js


def test_continue_after_an_error_once_and_only_while_recent():
    err = {"uuid": "e1", "age": 60, "text": "API Error: Connection lost"}
    assert update.continue_after_error(err, None, 0)[0] == "tell"
    assert update.continue_after_error(err, "e1", 0)[0] == "nothing"
    assert update.continue_after_error(err, None, 3)[0] == "stuck"
    assert update.continue_after_error({**err, "age": 7200}, None, 0)[0] == "stuck"
    assert update.continue_after_error({**err, "age": 200000}, None, 0)[0] == "nothing"
    assert update.continue_after_error(None, None, 0)[0] == "nothing"
