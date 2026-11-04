"""A child's environment is declared, never inherited; and ssh comes back one way when unreachable."""
import json
import subprocess

import pytest

from claude_ops import environ, ssh


def test_child_env_starts_from_nothing():
    outer = {"PATH": "/outer", "HOME": "/home/u", "PYTHONPATH": "/tmp/x", "GIT_DIR": "/tmp/g",
             "SOMETHING_NEW": "1", "LANG": "C.UTF-8"}
    env = environ.child_env(path="/usr/bin:/bin", environ=outer)
    assert env == {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "C.UTF-8"}


def test_no_redirect_survives_whatever_the_caller_has():
    outer = {name: "x" for name in environ.REDIRECTS} | {"HOME": "/h"}
    env = environ.child_env(path="/bin", environ=outer)
    assert not set(environ.REDIRECTS) & set(env)


def test_keep_and_add_widen_at_the_call_site():
    env = environ.child_env(path="/bin", keep=("DATABASE_URL", "UNSET_ONE"), add={"FOO": "bar"},
                            environ={"DATABASE_URL": "postgres://example.org/db"})
    assert env["DATABASE_URL"] == "postgres://example.org/db" and env["FOO"] == "bar"
    assert "UNSET_ONE" not in env


@pytest.mark.parametrize("arg", ["keep", "add"])
def test_a_redirect_is_refused_in_keep_and_add(arg):
    with pytest.raises(ValueError, match="PYTHONPATH redirects"):
        environ.child_env(path="/bin", **{arg: ("PYTHONPATH",) if arg == "keep" else {"PYTHONPATH": "/x"}})


def test_utf8_only_when_no_locale_is_named():
    assert environ.child_env(path="/bin", environ={})["LC_CTYPE"] == environ.UTF8
    assert "LC_CTYPE" not in environ.child_env(path="/bin", environ={"LC_ALL": "C"})


def test_path_is_required():
    with pytest.raises(TypeError):
        environ.child_env()  # type: ignore[call-arg]


def _fake(seen, result=None, raises=None):
    def run(argv, **k):
        seen.append((argv, k))
        if raises:
            raise raises
        return result or subprocess.CompletedProcess(argv, 0, "ok\n", "")
    return run


def test_run_never_prompts_and_carries_env(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", _fake(seen))
    r = ssh.run("host", "uptime", env={"PATH": "/bin"})
    argv, k = seen[0]
    assert r.returncode == 0 and argv[:1] == ["ssh"] and "BatchMode=yes" in argv and argv[-2:] == ["host", "uptime"]
    assert k["env"] == {"PATH": "/bin"} and k["stdin"] is subprocess.DEVNULL and "input" not in k


def test_stdin_is_fed_not_put_on_the_command_line(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", _fake(seen))
    ssh.run("host", "sudo -S true", stdin="secret\n")
    argv, k = seen[0]
    assert k["input"] == "secret\n" and "secret" not in " ".join(argv)


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("ssh", 1), FileNotFoundError("ssh")])
def test_an_unreachable_host_comes_back_as_255_never_raised(monkeypatch, exc):
    monkeypatch.setattr(subprocess, "run", _fake([], raises=exc))
    for r in (ssh.run("host", "x"), ssh.copy("host", ["/path/to/a"], "/tmp/")):
        assert r.returncode == ssh.UNREACHABLE and type(exc).__name__ in r.stderr


def test_copy_names_the_destination_on_the_host(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", _fake(seen))
    ssh.copy("host", ["/path/to/a", "/path/to/b"], "/tmp/", env={"PATH": "/bin"})
    argv, k = seen[0]
    assert argv[0] == "scp" and argv[-3:] == ["/path/to/a", "/path/to/b", "host:/tmp/"] and k["env"] == {"PATH": "/bin"}


def _tail(ip, peers):
    def run(argv, **k):
        if argv[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(argv, 0, f"user u\nhostname {ip}\nport 22\n", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"Peer": peers}), "")
    return run


def test_peer_online_is_tailscales_view_of_the_host():
    peers = {"a": {"TailscaleIPs": ["100.64.0.2"], "Online": True},
             "b": {"TailscaleIPs": ["100.64.0.3"], "Online": False}}
    assert ssh.peer_online("one", run=_tail("100.64.0.2", peers)) is True
    assert ssh.peer_online("two", run=_tail("100.64.0.3", peers)) is False
    assert ssh.peer_online("lan", run=_tail("192.0.2.5", peers)) is None


def test_peer_online_is_none_when_it_cannot_be_told():
    def broken(argv, **k):
        raise FileNotFoundError(argv[0])
    assert ssh.peer_online("host", run=broken) is None
    assert ssh.peer_online("host", run=lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", "")) is None


def _said(code, err, seen=None):
    def run(argv, **k):
        if seen is not None:
            seen.append((argv, k))
        return subprocess.CompletedProcess(argv, code, "", err)
    return run


def test_greet_asks_with_no_command_and_never_prompts():
    seen = []
    r = ssh.greet("git@example.org", env={"PATH": "/bin"},
                  run=_said(1, "Hi user! You've successfully authenticated, but there is no shell access.\n", seen))
    argv, k = seen[0]
    assert argv[:2] == ["ssh", "-T"] and "BatchMode=yes" in argv and argv[-1] == "git@example.org"
    assert k["stdin"] is subprocess.DEVNULL and k["env"] == {"PATH": "/bin"}
    assert r.returncode == 1 and "successfully authenticated" in r.stderr


def test_greet_passes_on_a_refusal_with_its_words():
    r = ssh.greet("git@example.org", run=_said(255, "git@example.org: Permission denied (publickey).\n"))
    assert r.returncode == ssh.UNREACHABLE and "Permission denied (publickey)" in r.stderr


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("ssh", 1), FileNotFoundError("ssh")])
def test_greet_that_cannot_finish_comes_back_as_255_never_raised(exc):
    def broken(argv, **k):
        raise exc
    r = ssh.greet("git@example.org", run=broken)
    assert r.returncode == ssh.UNREACHABLE and type(exc).__name__ in r.stderr


def test_identity_files_in_ssh_order_with_home_expanded(monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
    out = "user git\nidentityfile ~/.ssh/id_ed25519\nidentityfile /keys/other\nport 22\n"
    files = ssh.identity_files("example.org", run=lambda argv, **k: subprocess.CompletedProcess(argv, 0, out, ""))
    assert files == ["/home/u/.ssh/id_ed25519", "/keys/other"]


def test_identity_files_is_empty_when_ssh_cannot_say():
    def broken(argv, **k):
        raise FileNotFoundError(argv[0])
    assert ssh.identity_files("example.org", run=broken) == []
    assert ssh.identity_files("example.org", run=lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", "bad")) == []
