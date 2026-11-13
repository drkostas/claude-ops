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


@pytest.mark.parametrize("command", ["claude-ops merge --help", "gh pr merge -h", "gh pr merge 7 --help"])
def test_asking_for_help_merges_nothing(clones, command):
    assert bash(command, clones["service"]) is None


# --- the approval list ------------------------------------------------------------------------------
A_SHA, B_SHA = "a" * 40, "b" * 40


@pytest.fixture()
def gate(tmp_path):
    """An approval list for example-org/library whose command approves only A_SHA, as a reviewer who read
    that head would."""
    cmd = tmp_path / "approved"
    cmd.write_text('#!/bin/sh\n[ "$3" = "%s" ] && { echo "approved $1#$2"; exit 0; }\n'
                   'echo "no approval for $1#$2 at $3" >&2\nexit 1\n' % A_SHA)
    cmd.chmod(0o755)
    return {"repositories": {"example-org/library"}, "command": [str(cmd)], "message": "Ask for one.",
            "timeout": 10.0}


def merge_call(command, cwd="/"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def github(sha):
    """A stand-in for GitHub that gives this head for every pull request, and counts its reads."""
    reads = []

    def head(repo, n):
        reads.append((repo, n))
        return sha, ""
    head.reads = reads
    return head


def test_a_merge_of_the_approved_head_goes_through(gate):
    head = github(A_SHA)
    assert mg.approve(merge_call("claude-ops merge 7 --repo example-org/library"), gate, head=head) is None
    assert head.reads == [("example-org/library", 7)]


def test_a_merge_with_no_approval_is_refused_with_the_commands_words(gate):
    why = mg.approve(merge_call("gh pr merge 7 -R example-org/library --merge"), gate, head=github(B_SHA))
    assert why and "example-org/library#7" in why and B_SHA[:12] in why
    assert f"no approval for example-org/library#7 at {B_SHA}" in why


def test_a_head_moved_after_the_approval_is_refused(gate):
    """The reviewer approved A. A push after that moved the pull request to B, and B is what GitHub
    would merge now, so B is what the command is asked about."""
    call = merge_call("gh api -X PUT repos/example-org/library/pulls/7/merge -f merge_method=merge")
    assert mg.approve(call, gate, head=github(A_SHA)) is None
    why = mg.approve(call, gate, head=github(B_SHA))
    assert why and "not approved" in why and B_SHA[:12] in why


@pytest.mark.parametrize("command, said", [
    (["/nonexistent/approved"], "could not start"),
    (["sh", "-c", "sleep 5", "approver"], "did not answer within 0.5 s"),
    (["sh", "-c", "exit 4", "approver"], "exited 4"),
    ([], "no approval command is set"),
])
def test_an_approval_command_that_fails_refuses_the_merge(gate, command, said):
    gate = {**gate, "command": command, "timeout": 0.5}
    why = mg.approve(merge_call("claude-ops merge 7 --repo example-org/library"), gate, head=github(A_SHA))
    assert why and said in why


def test_a_head_github_will_not_give_refuses_the_merge(gate):
    why = mg.approve(merge_call("claude-ops merge 7 --repo example-org/library"), gate,
                     head=lambda repo, n: (None, "HTTP 404 Not Found"))
    assert why and "could not be read from GitHub (HTTP 404 Not Found)" in why


def test_a_merge_whose_number_cannot_be_read_is_refused(gate, clones):
    why = mg.approve(merge_call("gh pr merge --merge", clones["library"]), gate, head=github(A_SHA))
    assert why and "without naming its number" in why


def test_each_pull_request_of_a_cascade_is_checked_and_option_values_are_not_numbers(gate):
    head = github(A_SHA)
    assert mg.approve(merge_call("claude-ops merge 7 8 --repo example-org/library --timeout 30 --poll 5"),
                      gate, head=head) is None
    assert head.reads == [("example-org/library", 7), ("example-org/library", 8)]


def test_a_pull_request_address_names_its_own_repository(gate, clones):
    head = github(B_SHA)
    why = mg.approve(merge_call("gh pr merge https://github.com/Example-Org/library/pull/9 --merge",
                                clones["service"]), gate, head=head)
    assert why and head.reads == [("example-org/library", 9)]


def test_the_github_mcp_merge_is_checked_too(gate):
    call = {"tool_name": "mcp__github__merge_pull_request",
            "tool_input": {"owner": "Example-Org", "repo": "library", "pullNumber": 7}}
    assert mg.approve(call, gate, head=github(A_SHA)) is None
    assert "not approved" in mg.approve(call, gate, head=github(B_SHA))


def test_other_repositories_and_reads_are_not_asked_about(gate):
    head = github(B_SHA)
    for command in ("claude-ops merge 7 --repo example-org/other", "gh pr view 7 -R example-org/library",
                    "gh api repos/example-org/library/pulls/7", "gh pr merge 7 -R example-org/library --help"):
        assert mg.approve(merge_call(command), gate, head=head) is None, command
    assert head.reads == []


def test_head_of_reads_the_sha_gh_prints(tmp_path, monkeypatch):
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\ncase "$2" in\n  repos/example-org/library/pulls/7) echo %s ;;\n'
                  '  *) echo "HTTP 404: Not Found" >&2; exit 1 ;;\nesac\n' % A_SHA)
    gh.chmod(0o755)
    monkeypatch.setattr(mg.shutil, "which", lambda name: str(gh))
    assert mg.head_of("example-org/library", 7) == (A_SHA, "")
    assert mg.head_of("example-org/library", 8) == (None, "HTTP 404: Not Found")


def test_approval_config_from_the_file(tmp_path):
    f = tmp_path / "gate.json"
    f.write_text(json.dumps({"repositories": ["example-org/service"],
                             "approval": {"repositories": ["Example-Org/Library"], "command": "approved --quiet",
                                          "message": "Ask for one."}}))
    assert mg.approval_config(["--config", str(f)]) == {
        "repositories": {"example-org/library"}, "command": ["approved", "--quiet"], "message": "Ask for one.",
        "timeout": mg.APPROVAL_TIMEOUT_S}
    assert mg.config(["--config", str(f)]) == ({"example-org/service"}, "")
    f.write_text(json.dumps({"repositories": ["example-org/service"]}))
    assert mg.approval_config(["--config", str(f)]) is None


def test_main_refuses_an_unapproved_head_and_refuses_when_the_check_breaks(tmp_path, monkeypatch, capsys, gate):
    f = tmp_path / "gate.json"
    f.write_text(json.dumps({"approval": {"repositories": ["example-org/library"], "command": gate["command"],
                                          "message": "Ask the reviewer."}}))

    def run(command):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(merge_call(command))))
        return mg.main(["--config", str(f)])
    monkeypatch.setattr(mg, "head_of", lambda repo, n: (A_SHA, ""))
    assert run("claude-ops merge 7 --repo example-org/library") == 0
    monkeypatch.setattr(mg, "head_of", lambda repo, n: (B_SHA, ""))
    assert run("claude-ops merge 7 --repo example-org/library") == 2
    err = capsys.readouterr().err
    assert err.startswith("Blocked:") and "only once its head is approved" in err and "Ask the reviewer." in err

    def broken(repo, n):
        raise RuntimeError("no network")
    monkeypatch.setattr(mg, "head_of", broken)
    assert run("claude-ops merge 7 --repo example-org/library") == 2
    assert "the approval check failed (RuntimeError: no network)" in capsys.readouterr().err
    assert run("claude-ops merge 7 --repo example-org/other") == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert mg.main(["--config", str(f)]) == 0


@pytest.mark.parametrize("command, flag", [
    ("gh pr merge 7 -R example-org/library --auto", "--auto"),
    ("gh pr merge 7 -R example-org/library --merge --auto", "--auto"),
    ("claude-ops merge 7 --repo example-org/library --follow-head", "--follow-head"),
])
def test_a_merge_that_takes_a_later_head_is_refused_even_when_this_head_is_approved(gate, command, flag):
    """Auto-merge merges whatever head the pull request has when its checks pass, so a push after the
    approval would merge unread. An approval holds for one head."""
    head = github(A_SHA)
    why = mg.approve(merge_call(command), gate, head=head)
    assert why and flag in why and "one head only" in why
    assert mg.approve(merge_call(command.replace("library", "other")), gate, head=head) is None


def test_graphql_auto_merge_is_refused_while_an_approval_list_is_set(gate):
    call = merge_call("gh api graphql -f query='mutation { enablePullRequestAutoMerge(input: "
                      "{pullRequestId: \"PR_kw\"}) { clientMutationId } }'")
    assert "enablePullRequestAutoMerge" in mg.approve(call, gate, head=github(A_SHA))
    assert mg.approve(call, None) is None


def test_a_merge_the_hook_cannot_read_is_refused_when_it_names_a_listed_repository(tmp_path, monkeypatch, capsys, gate):
    f = tmp_path / "gate.json"
    f.write_text(json.dumps({"approval": {"repositories": ["example-org/library"], "command": gate["command"]}}))

    def unreadable(command, cwd):
        raise ValueError("a line the reader cannot split")
    monkeypatch.setattr(mg.commandline, "walk", unreadable)

    def run(command):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(merge_call(command))))
        return mg.main(["--config", str(f)])
    assert run("gh pr merge 7 -R example-org/library") == 2
    assert "names merge and a repository whose merges need an approval" in capsys.readouterr().err
    assert run("gh pr merge 7 -R example-org/other") == 0
    assert run("echo library") == 0
