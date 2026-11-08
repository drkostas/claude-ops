"""sign_in: a new Claude login for one account, made without touching the login other sessions use.

Every Claude Code session on a Mac reads one login, the keychain item `Claude Code-credentials`, and
`/login` replaces it. A program that keeps a saved copy of each account's login (see `accounts`)
sometimes needs a fresh copy of one account, for example when that copy's refresh token was revoked,
while the sessions keep running on the login in use.

A session started with `CLAUDE_CONFIG_DIR` keeps its login apart: in a keychain item named after that
folder (`side_service`) and in that folder's own `.claude.json`. `SignIn.start` runs
`claude auth login --claudeai` in a new folder of that kind, with a stand-in `open` first on its PATH,
so no browser opens. The page to sign in on is `url()`, which the caller shows wherever the person is
already signed in to Claude. When the person allows it there, the page sends the code to the
program's own local listener, and `wait()` returns the new login and its `oauthAccount` block. The side
item and the folder are deleted then, or by `cancel()`.

⛔ The live item and the config file of every other session are never read or written here. Writing
the new login where it belongs (a saved copy, or the live item when the live login is dead) is the
caller's decision, with `accounts.write_item` and `accounts.write_account`.
"""
from __future__ import annotations

from dataclasses import dataclass
import getpass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unicodedata
from typing import Mapping

from .accounts import LIVE_SERVICE, Unreadable, account_email, read_account, read_item

#: the item Claude Code uses for a session whose config folder is not the default one
SIDE_PREFIX = LIVE_SERVICE + "-"
#: written as the program's `open` (and `BROWSER`): it only records the address it was asked to open
STAND_IN = '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$(dirname "$0")/../opened"\n'
#: how long the page may wait for the person, by default
WAIT_S = 600.0


class SignInFailed(RuntimeError):
    """The sign-in did not give a login. The side item and the folder are already deleted."""


class WrongAccount(SignInFailed):
    """The person allowed the sign-in on another account than the one asked for. The login was not
    kept: the side item and the folder are already deleted."""

    def __init__(self, wanted: str, got: "str | None"):
        super().__init__(f"the page was signed in as {got or 'an unknown account'}, not {wanted}")
        self.wanted, self.got = wanted, got


def side_service(folder: "os.PathLike | str") -> str:
    """The keychain item of a Claude Code session whose config folder is `folder`: the live item's
    name, a dash and the first 8 hex digits of the SHA-256 of the folder (in Unicode NFC). It is only
    that name when `CLAUDE_SECURESTORAGE_CONFIG_DIR` is the same folder, which `SignIn.start` sets."""
    name = unicodedata.normalize("NFC", str(folder))
    return SIDE_PREFIX + hashlib.sha256(name.encode()).hexdigest()[:8]


@dataclass
class SignedIn:
    login: dict           # the keychain item's JSON, as Claude Code wrote it
    block: "dict | None"  # the folder's oauthAccount block (who is signed in, no secret)
    email: "str | None"   # the email that block names


def _security_delete(service: str, account: str, run, env) -> None:
    run(["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True, text=True, env=env)


class SignIn:
    """One sign-in in a folder of its own. Make it with `SignIn.start`."""

    def __init__(self, proc, folder: Path, account: str, email: "str | None", run, env):
        self.proc, self.folder, self.account, self.email = proc, folder, account, email
        self._run, self._env = run, env
        self.config = folder / "config"
        self.service = side_service(self.config)
        self._done = False

    @classmethod
    def start(cls, email: "str | None" = None, *, claude: str = "claude", account: "str | None" = None,
              env: "Mapping[str, str] | None" = None, folder: "os.PathLike | str | None" = None,
              popen=subprocess.Popen, run=subprocess.run) -> "SignIn":
        """Run `claude auth login --claudeai` (with `--email` when given, which fills in the address
        on the page) in a new folder. `account` is the keychain account Claude Code writes under,
        the user's name by default. `env` adds to or replaces the program's few variables."""
        base = Path(os.path.realpath(folder or tempfile.mkdtemp(prefix="claude-sign-in-")))
        (base / "bin").mkdir(parents=True, exist_ok=True)
        (base / "config").mkdir(exist_ok=True)
        opener = base / "bin" / "open"
        opener.write_text(STAND_IN)
        opener.chmod(0o700)
        account = account or os.environ.get("USER") or getpass.getuser()
        config = str(base / "config")
        child = {"HOME": os.path.expanduser("~"), "USER": account, "LANG": "en_US.UTF-8", "TERM": "dumb",
                 "PATH": f"{base / 'bin'}:/usr/bin:/bin", "BROWSER": str(opener),
                 "CLAUDE_CONFIG_DIR": config, "CLAUDE_SECURESTORAGE_CONFIG_DIR": config}
        child.update(env or {})
        args = [claude, "auth", "login", "--claudeai"] + (["--email", email] if email else [])
        with open(base / "output", "wb") as out:
            proc = popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         env=child, cwd=str(base))
        return cls(proc, base, account, email, run, {"PATH": "/usr/bin:/bin", "HOME": child["HOME"]})

    def output(self, last: int = 600) -> str:
        """The end of what the program printed (its messages, never a token)."""
        try:
            return (self.folder / "output").read_text(errors="replace")[-last:]
        except OSError:
            return ""

    def url(self, timeout: float = 30.0, *, sleep=time.sleep, clock=time.monotonic) -> str:
        """The address of the sign-in page, as the program asked for it to be opened. Raises
        SignInFailed when the program ends or `timeout` passes without asking."""
        until = clock() + timeout
        while True:
            try:
                for line in (self.folder / "opened").read_text().splitlines():
                    found = next((w for w in line.split() if w.startswith("https://")), None)
                    if found:
                        return found
            except OSError:
                pass
            if self.proc.poll() is not None:
                why = self.output().strip()
                self._cleanup()
                raise SignInFailed(f"claude auth login ended before it opened a page: {why or 'no output'}")
            if clock() >= until:
                self.cancel()
                raise SignInFailed(f"claude auth login opened no page in {timeout:.0f} s")
            sleep(0.2)

    def running(self) -> bool:
        return self.proc.poll() is None

    def wait(self, timeout: float = WAIT_S) -> SignedIn:
        """Wait for the person to allow the sign-in, and return the new login. Raises SignInFailed
        when the program fails or `timeout` passes, and WrongAccount when the login is another
        account's than the email given to `start`. Every way out deletes the side item and the
        folder, so the new login exists only in what this returns."""
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            raise SignInFailed(f"nobody allowed the sign-in within {timeout:.0f} s") from None
        if code != 0:
            why = self.output().strip()
            self._cleanup()
            raise SignInFailed(f"claude auth login exited {code}: {why or 'no output'}")
        try:
            login = read_item(self.service, self.account, run=self._run, env=self._env, strict=True)
        except Unreadable as e:
            self._cleanup()
            raise SignInFailed(f"the keychain did not answer for the new login: {e}") from None
        block = read_account(self.config / ".claude.json")
        self._cleanup()
        if not login:
            raise SignInFailed(f"claude auth login said it signed in, but {self.service} holds no login")
        got = account_email(block)
        if self.email and (got or "").lower() != self.email.lower():
            raise WrongAccount(self.email, got)
        return SignedIn(login, block, got)

    def cancel(self) -> None:
        """End the program if it still runs, and delete the side item and the folder."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._cleanup()

    def _cleanup(self) -> None:
        if self._done:
            return
        self._done = True
        _security_delete(self.service, self.account, self._run, self._env)
        shutil.rmtree(self.folder, ignore_errors=True)
