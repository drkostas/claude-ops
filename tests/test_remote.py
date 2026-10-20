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


def test_the_session_names_its_owner(tmp_path):
    t = tmp_path / "c.jsonl"
    t.write_text('{"bridgeSessionId": "cse_OLD1", "ownerAccountUuid": "aaaa-1"}\n'
                 '{"type": "bridge-session", "bridgeSessionId":"cse_NEW2", "ownerAccountUuid":"bbbb-2"}\n')
    assert remote.remote_session(t) == {"row": "session_NEW2", "account": "bbbb-2"}
    (tmp_path / "old.jsonl").write_text('{"bridgeSessionId": "cse_X"}\n')
    assert remote.remote_session(tmp_path / "old.jsonl") == {"row": "session_X", "account": None}
    assert remote.remote_session(None) is None


def _page(js, signed_in, rows=()):
    """Run a rename script under node against a fake page: an account endpoint and a list."""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    fake = """
globalThis.setTimeout = (f) => { f(); return 0; };
globalThis.fetch = async (p) => ({json: async () => ({uuid: %s})});
globalThis.document = {querySelector: (q) => %s.some(r => q.includes(r)) ? {} : null,
                       querySelectorAll: () => []};
Promise.resolve(%s).then(r => console.log(JSON.stringify(r)));
""" % (json.dumps(signed_in), json.dumps(list(rows)), js)
    out = subprocess.run([node, "-e", fake], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_page_of_another_account_says_so_before_waiting_for_the_row():
    got = _page(remote.rename_js("session_A", "build", account="mine"), "theirs")
    assert got == {"ok": False, "why": remote.OTHER_ACCOUNT, "signed_in": "theirs"}


def test_a_page_of_the_same_account_still_looks_for_the_row():
    got = _page(remote.rename_js("session_A", "build", account="mine"), "mine")
    assert got == {"ok": False, "why": remote.NOT_LISTED}
    assert _page(remote.rename_js("session_A", "build"), "theirs")["why"] == remote.NOT_LISTED


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
