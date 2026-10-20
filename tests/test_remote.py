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


def test_the_account_script_reads_the_signed_in_account():
    assert _page(remote.account_js(), "mine") == {"account": "mine"}
    assert _page(remote.account_js(), None) == {"account": None}


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


class _Session:
    """A fake sessions API: GET answers the title, PUT sets it, any other session is 404."""

    def __init__(self, title, *, put_status=200, ignore_put=False):
        self.title, self.put_status, self.ignore_put = title, put_status, ignore_put
        self.calls = []

    def __call__(self, req, timeout=15):
        import io
        import urllib.error
        self.calls.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        if not req.full_url.endswith("/v1/code/sessions/cse_A"):
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b""))
        if req.get_method() == "PUT":
            if self.put_status != 200:
                raise urllib.error.HTTPError(req.full_url, self.put_status, "no", {}, io.BytesIO(b""))
            if not self.ignore_put:
                self.title = json.loads(req.data)["title"]
            body = b"{}"
        else:
            body = json.dumps({"response_shape": {"id": "cse_A", "title": self.title}}).encode()

        class Answer:
            status = 200

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Answer()


def test_the_api_id_of_a_row():
    assert remote.api_id("session_A") == "cse_A" and remote.api_id("cse_A") == "cse_A"


def test_set_title_puts_the_name_and_reads_it_back():
    api = _Session("laptop.local")
    assert remote.set_title("session_A", "build", "tok", fetch=api) == {
        "ok": True, "title": "build", "changed": True}
    assert [c[0] for c in api.calls] == ["GET", "PUT", "GET"]
    assert all(c[2] == "Bearer tok" for c in api.calls)


def test_a_session_already_named_is_left_alone():
    api = _Session("build")
    assert remote.set_title("session_A", "build", "tok", fetch=api) == {
        "ok": True, "title": "build", "changed": False}
    assert [c[0] for c in api.calls] == ["GET"]


def test_a_session_the_login_cannot_see_says_so():
    got = remote.set_title("session_OTHER", "build", "tok", fetch=_Session("x"))
    assert got == {"ok": False, "why": remote.NOT_FOUND}


def test_a_refused_login_and_a_title_that_did_not_take_are_failures():
    assert remote.set_title("session_A", "build", "tok", fetch=_Session("x", put_status=401)) == {
        "ok": False, "why": remote.REFUSED}
    got = remote.set_title("session_A", "build", "tok", fetch=_Session("x", ignore_put=True))
    assert got == {"ok": False, "title": "x", "changed": True}
