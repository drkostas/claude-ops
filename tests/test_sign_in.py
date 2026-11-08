"""sign_in, with a stand-in `claude` program and a fake keychain. Nothing here opens a browser or
touches a real keychain item."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unicodedata

import pytest

from claude_ops import sign_in
from claude_ops.accounts import LIVE_SERVICE

URL = "https://claude.example.org/oauth/authorize?code=true&redirect_uri=http%3A%2F%2Flocalhost%3A5555%2Fcallback"

# The stand-in for `claude auth login`: it asks for the page through `open` (found on its PATH, as
# the real program does), then behaves as its mode says.
STUB = f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys, time
mode = os.environ.get("STUB_MODE", "ok")
here = pathlib.Path.cwd()
(here / "args").write_text(json.dumps(sys.argv[1:]))
(here / "env").write_text(json.dumps({{k: os.environ.get(k) for k in
    ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "PATH", "BROWSER", "USER")}}))
if mode == "noopen":
    print("no network"); sys.exit(2)
subprocess.run(["open", {URL!r}])
print("Opening browser to sign in")
sys.stdout.flush()
if mode == "fail":
    print("Login failed: the request was denied"); sys.exit(1)
if mode == "hang":
    time.sleep(60); sys.exit(0)
while not (here / "allow").exists():
    time.sleep(0.05)
config = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
config.write_text(json.dumps({{"oauthAccount": {{"emailAddress": os.environ.get("STUB_EMAIL", "a@example.org"),
                                              "accountUuid": "u1"}}}}))
print("Login successful.")
"""


class Keychain:
    """A fake `security`: items by (service, account), and every call it was given."""

    def __init__(self):
        self.items, self.calls = {}, []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        verb, service, account = args[1], args[args.index("-s") + 1], args[args.index("-a") + 1]
        if verb == "find-generic-password":
            if (service, account) in self.items:
                return subprocess.CompletedProcess(args, 0, json.dumps(self.items[(service, account)]), "")
            return subprocess.CompletedProcess(args, 44, "", "not found")
        if verb == "delete-generic-password":
            found = self.items.pop((service, account), None)
            return subprocess.CompletedProcess(args, 0 if found else 44, "", "")
        raise AssertionError(f"unexpected security call {args}")

    def services(self):
        return {c[c.index("-s") + 1] for c in self.calls}


@pytest.fixture
def claude(tmp_path):
    stub = tmp_path / "claude"
    stub.write_text(STUB)
    stub.chmod(0o755)
    return str(stub)


def start(claude, tmp_path, keychain, mode="ok", email=None, stub_email=None):
    env = {"STUB_MODE": mode}
    if stub_email:
        env["STUB_EMAIL"] = stub_email
    return sign_in.SignIn.start(email, claude=claude, account="someone", env=env,
                                folder=tmp_path / "work", run=keychain)


def test_side_service_is_the_live_name_and_the_folder_hash():
    folder = "/tmp/café"  # an "é" written as two code points
    nfc = unicodedata.normalize("NFC", folder)
    assert sign_in.side_service(folder) == LIVE_SERVICE + "-" + hashlib.sha256(nfc.encode()).hexdigest()[:8]
    assert sign_in.side_service(folder) != LIVE_SERVICE


def test_the_page_comes_from_the_stand_in_open_and_the_program_runs_in_its_own_folder(claude, tmp_path):
    keychain = Keychain()
    s = start(claude, tmp_path, keychain, email="a@example.org")
    assert s.url(timeout=10) == URL
    work = Path(os.path.realpath(tmp_path / "work"))
    env = json.loads((work / "env").read_text())
    assert env["CLAUDE_CONFIG_DIR"] == env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(work / "config")
    assert env["PATH"].split(":")[0] == str(work / "bin")
    assert env["USER"] == "someone"
    assert json.loads((work / "args").read_text()) == ["auth", "login", "--claudeai", "--email", "a@example.org"]
    s.cancel()
    assert not work.exists()


def test_wait_returns_the_new_login_and_deletes_the_side_item_and_the_folder(claude, tmp_path):
    keychain = Keychain()
    s = start(claude, tmp_path, keychain, email="A@example.org")
    s.url(timeout=10)
    keychain.items[(s.service, "someone")] = {"claudeAiOauth": {"accessToken": "t", "refreshToken": "r"}}
    (s.folder / "allow").write_text("")
    got = s.wait(timeout=20)
    assert got.login == {"claudeAiOauth": {"accessToken": "t", "refreshToken": "r"}}
    assert got.email == "a@example.org" and got.block["accountUuid"] == "u1"
    assert keychain.items == {}
    assert not s.folder.exists()
    assert LIVE_SERVICE not in keychain.services()


def test_a_login_on_another_account_is_not_kept(claude, tmp_path):
    keychain = Keychain()
    s = start(claude, tmp_path, keychain, email="a@example.org", stub_email="b@example.org")
    s.url(timeout=10)
    keychain.items[(s.service, "someone")] = {"claudeAiOauth": {"accessToken": "t"}}
    (s.folder / "allow").write_text("")
    with pytest.raises(sign_in.WrongAccount) as e:
        s.wait(timeout=20)
    assert e.value.got == "b@example.org" and e.value.wanted == "a@example.org"
    assert keychain.items == {} and not s.folder.exists()


def test_a_failed_sign_in_says_what_the_program_said(claude, tmp_path):
    keychain = Keychain()
    s = start(claude, tmp_path, keychain, mode="fail")
    s.url(timeout=10)
    with pytest.raises(sign_in.SignInFailed, match="denied"):
        s.wait(timeout=20)
    assert not s.folder.exists()


def test_a_program_that_ends_before_asking_for_a_page_is_a_failure(claude, tmp_path):
    keychain = Keychain()
    s = start(claude, tmp_path, keychain, mode="noopen")
    with pytest.raises(sign_in.SignInFailed, match="no network"):
        s.url(timeout=10)
    assert not s.folder.exists()


def test_nobody_allowing_it_ends_the_program_and_keeps_nothing(claude, tmp_path):
    keychain = Keychain()
    s = start(claude, tmp_path, keychain, mode="hang")
    s.url(timeout=10)
    with pytest.raises(sign_in.SignInFailed, match="within 1 s"):
        s.wait(timeout=1)
    assert not s.running() and not s.folder.exists()


def test_a_sign_in_that_left_no_login_is_a_failure(claude, tmp_path):
    keychain = Keychain()
    s = start(claude, tmp_path, keychain)
    s.url(timeout=10)
    (s.folder / "allow").write_text("")
    with pytest.raises(sign_in.SignInFailed, match="holds no login"):
        s.wait(timeout=20)
    assert LIVE_SERVICE not in keychain.services()
