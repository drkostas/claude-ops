"""The merge-gate hook: a gated repository merges only through its ship script."""
import io
import json
import subprocess

import pytest

from claude_ops.hooks import merge_gate as mg

REPOS = {"example-org/service"}


@pytest.fixture()
def clones(tmp_path):
    """Two clones: one of the gated repository, one of a repository that is not gated."""
    out = {}
    for name, url in (("service", "https://github.com/Example-Org/service.git"),
                      ("library", "git@github.com:example-org/library.git")):
        d = tmp_path / name
        d.mkdir()
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", url], check=True)
        out[name] = d
    return out


def bash(command, cwd):
    return mg.check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}, REPOS)


@pytest.mark.parametrize("command", [
    "claude-ops merge 12 --repo example-org/service --method merge",
    "claude-ops merge 12 --repo=Example-Org/Service",
    "gh pr merge 12 --repo example-org/service --merge",
    "gh pr merge 12 -R example-org/service",
    "cd /tmp && gh api -X PUT repos/example-org/service/pulls/12/merge -f merge_method=merge",
    "gh api --method PUT /repos/example-org/service/pulls/12/merge",
    "env GH_TOKEN=x claude-ops merge 12 --repo example-org/service",
    "echo start; claude-ops merge 12 --repo example-org/service && echo done",
])
def test_a_hand_merge_in_a_gated_repository_is_refused(clones, command):
    why = bash(command, clones["library"])
    assert why and "example-org/service" in why


def test_a_merge_with_no_repo_named_is_judged_by_the_clone_it_runs_in(clones):
    assert bash("claude-ops merge 12 --method merge", clones["service"])
    assert bash("gh pr merge 12 --merge", clones["service"])
    assert bash(f"cd {clones['service']} && gh pr merge 12", clones["library"])
    assert bash("gh pr merge 12 --merge", clones["library"]) is None


@pytest.mark.parametrize("command", [
    "claude-ops merge 7 --repo example-org/library --method merge",
    "gh pr merge 7 --repo example-org/library",
    "gh pr view 12 --repo example-org/service",
    "gh api repos/example-org/service/pulls/12/merge",
    "gh api repos/example-org/service/pulls/12",
    "bash tools/ship.sh 12 ~/worktrees/topic",
    "git merge origin/main",
    "echo 'claude-ops merge 12 --repo example-org/service'",
])
def test_other_repositories_reads_and_the_ship_script_go_through(clones, command):
    assert bash(command, clones["service"] if "ship" in command or "git merge" in command else clones["library"]) is None


def test_the_github_mcp_merge_is_refused_for_a_gated_repository_only():
    call = {"tool_name": "mcp__github__merge_pull_request",
            "tool_input": {"owner": "Example-Org", "repo": "service", "pullNumber": 12}}
    assert "pull request 12" in mg.check(call, REPOS)
    call["tool_input"]["repo"] = "library"
    assert mg.check(call, REPOS) is None
    assert mg.check({"tool_name": "mcp__github__get_file_contents",
                     "tool_input": {"owner": "example-org", "repo": "service"}}, REPOS) is None


def test_no_repositories_refuse_nothing(clones):
    assert mg.check({"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1 -R a/b"}}, set()) is None


def test_config_from_flags_and_from_a_file(tmp_path, monkeypatch):
    assert mg.config(["--repo", "A/B", "--message", "use ship"]) == ({"a/b"}, "use ship")
    f = tmp_path / "gate.json"
    f.write_text(json.dumps({"repositories": ["Example-Org/Service"], "message": "ship it"}))
    assert mg.config(["--config", str(f)]) == ({"example-org/service"}, "ship it")
    monkeypatch.setenv("CLAUDE_OPS_MERGE_GATE_CONFIG", str(tmp_path / "missing.json"))
    assert mg.config([]) == (set(), "")


def test_main_refuses_with_its_signature_and_fails_open(tmp_path, monkeypatch, capsys):
    f = tmp_path / "gate.json"
    f.write_text(json.dumps({"repositories": ["example-org/service"], "message": "Ship it with tools/ship.sh."}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 3 -R example-org/service"}, "cwd": "/"})))
    assert mg.main(["--config", str(f)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Blocked:") and "Ship it with tools/ship.sh." in err
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert mg.main(["--config", str(f)]) == 0
