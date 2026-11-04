"""The guarded-labels hook: a guarded label is added only by the sessions its rule names."""
import io
import json
import subprocess

import pytest

from claude_ops.hooks import guarded_labels as gl

ALLOWED = "0d0e0f10-1111-4222-8333-444455556666"
OTHER = "0d0e0f10-1111-4222-8333-777788889999"
RULES = [{"repositories": {"example-org/service"}, "labels": {"queue:first"}, "sessions": {ALLOWED},
          "message": "Ask the release session."}]


@pytest.fixture()
def clones(tmp_path):
    """Two clones: one of the guarded repository, one of a repository that is not guarded."""
    out = {}
    for name, url in (("service", "https://github.com/Example-Org/service.git"),
                      ("library", "git@github.com:example-org/library.git")):
        d = tmp_path / name
        d.mkdir()
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", url], check=True)
        out[name] = d
    return out


def bash(command, cwd, session=OTHER, rules=RULES):
    return gl.check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd),
                     "session_id": session}, rules)


@pytest.mark.parametrize("command", [
    "gh pr edit 12 --repo example-org/service --add-label queue:first",
    "gh pr edit 12 -R Example-Org/Service --add-label=Queue:First",
    "gh pr edit 12 -R example-org/service --add-label kind:bug,queue:first",
    "gh pr edit 12 -R example-org/service --add-label kind:bug --add-label queue:first",
    "gh issue edit 12 -R example-org/service --add-label queue:first",
    "gh pr create -R example-org/service --title t --body b -l queue:first",
    "gh pr create -R example-org/service --title t --body b --label=queue:first",
    "gh pr edit https://github.com/example-org/service/pull/12 --add-label queue:first",
    "GH_REPO=example-org/service gh pr edit 12 --add-label queue:first",
    "echo go && env GH_TOKEN=x gh pr edit 12 -R example-org/service --add-label queue:first",
    "gh api repos/example-org/service/issues/12/labels -f 'labels[]=queue:first'",
    "gh api -X POST /repos/example-org/service/issues/12/labels --raw-field labels[]=queue:first",
    "gh api --method PATCH repos/example-org/service/issues/12 -f labels[]=queue:first",
])
def test_a_guarded_label_added_by_another_session_is_refused(clones, command):
    why = bash(command, clones["library"])
    assert why and "queue:first" in why and "Ask the release session." in why


def test_an_add_with_no_repo_named_is_judged_by_the_clone_it_runs_in(clones):
    assert bash("gh pr edit 12 --add-label queue:first", clones["service"])
    assert bash(f"cd {clones['service']} && gh pr edit 12 --add-label queue:first", clones["library"])
    assert bash("gh pr edit 12 --add-label queue:first", clones["library"]) is None


def test_the_sessions_the_rule_names_may_add_it(clones):
    assert bash("gh pr edit 12 -R example-org/service --add-label queue:first", clones["library"],
                session=ALLOWED) is None


def test_a_rule_without_sessions_refuses_every_session_and_one_without_repositories_every_repository(clones):
    rules = [{"repositories": None, "labels": {"queue:first"}, "sessions": set(), "message": ""}]
    assert bash("gh pr edit 3 -R example-org/library --add-label queue:first", clones["library"],
                session=ALLOWED, rules=rules)


@pytest.mark.parametrize("command", [
    "gh pr edit 12 -R example-org/service --add-label kind:bug",
    "gh pr edit 12 -R example-org/service --remove-label queue:first",
    "gh pr edit 12 -R example-org/library --add-label queue:first",
    "gh pr view 12 -R example-org/service --json labels",
    "gh pr edit --help",
    "gh api repos/example-org/service/issues/12/labels",
    "gh api -X DELETE repos/example-org/service/issues/12/labels/queue:first",
    "gh api repos/example-org/service/issues/12/labels -f 'labels[]=kind:bug'",
    "gh label create queue:first -R example-org/service",
    "python3 -m tools.queue --first 12",
    "echo 'gh pr edit 12 -R example-org/service --add-label queue:first'",
])
def test_other_labels_removals_reads_and_programs_go_through(clones, command):
    assert bash(command, clones["library"]) is None


def test_the_json_a_gh_api_call_sends_is_read(clones, tmp_path):
    body = tmp_path / "body.json"
    body.write_text(json.dumps({"labels": [{"name": "Queue:First"}]}))
    assert bash(f"gh api repos/example-org/service/issues/12/labels --input {body}", clones["library"])
    assert bash("gh api repos/example-org/service/issues/12/labels --input - <<'EOF'\n"
                '{"labels": ["queue:first"]}\nEOF', clones["library"])
    body.write_text(json.dumps({"labels": ["kind:bug"]}))
    assert bash(f"gh api repos/example-org/service/issues/12/labels --input {body}", clones["library"]) is None


def test_a_github_mcp_call_that_sets_it_is_refused_for_the_guarded_repository_only():
    call = {"tool_name": "mcp__github__issue_write", "session_id": OTHER,
            "tool_input": {"method": "update", "owner": "Example-Org", "repo": "service", "issue_number": 12,
                           "labels": ["kind:bug", "queue:first"]}}
    assert "queue:first" in gl.check(call, RULES)
    call["session_id"] = ALLOWED
    assert gl.check(call, RULES) is None
    call["session_id"], call["tool_input"]["repo"] = OTHER, "library"
    assert gl.check(call, RULES) is None
    assert gl.check({"tool_name": "mcp__github__get_file_contents",
                     "tool_input": {"owner": "example-org", "repo": "service"}}, RULES) is None


def test_no_rules_refuse_nothing(clones):
    assert bash("gh pr edit 1 -R example-org/service --add-label queue:first", clones["library"], rules=[]) is None


def test_config_from_a_file(tmp_path, monkeypatch):
    f = tmp_path / "labels.json"
    f.write_text(json.dumps({"rules": [
        {"repositories": ["Example-Org/Service"], "labels": ["Queue:First"], "sessions": [ALLOWED], "message": "m"},
        {"labels": []},
        "not a rule",
    ]}))
    assert gl.config(["--config", str(f)]) == [
        {"repositories": {"example-org/service"}, "labels": {"queue:first"}, "sessions": {ALLOWED}, "message": "m"}]
    monkeypatch.setenv("CLAUDE_OPS_GUARDED_LABELS_CONFIG", str(f))
    assert len(gl.config([])) == 1
    monkeypatch.setenv("CLAUDE_OPS_GUARDED_LABELS_CONFIG", str(tmp_path / "missing.json"))
    assert gl.config([]) == []
    f.write_text("[1, 2]")
    assert gl.config(["--config", str(f)]) == []


def test_main_refuses_with_its_signature_and_fails_open(tmp_path, monkeypatch, capsys):
    f = tmp_path / "labels.json"
    f.write_text(json.dumps({"rules": [{"repositories": ["example-org/service"], "labels": ["queue:first"],
                                        "message": "Ask the release session."}]}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "session_id": OTHER, "cwd": "/",
         "tool_input": {"command": "gh pr edit 3 -R example-org/service --add-label queue:first"}})))
    assert gl.main(["--config", str(f)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Blocked:") and err.rstrip().endswith("Ask the release session.")
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert gl.main(["--config", str(f)]) == 0


def test_the_cli_and_the_chain_offer_the_hook(tmp_path, monkeypatch):
    from claude_ops import cli
    from claude_ops.hooks import chain
    assert any(h.get("matcher") == "Bash|mcp__github__.*" and h["hooks"][0]["command"] == "claude-ops hook guarded-labels"
               for h in cli.HOOKS["PreToolUse"])
    f = tmp_path / "labels.json"
    f.write_text(json.dumps({"rules": [{"labels": ["queue:first"]}]}))
    monkeypatch.setenv("CLAUDE_OPS_GUARDED_LABELS_CONFIG", str(f))
    call = json.dumps({"tool_name": "Bash", "session_id": OTHER, "cwd": "/",
                       "tool_input": {"command": "gh pr edit 3 -R example-org/service --add-label queue:first"}})
    monkeypatch.setattr("sys.stdin", io.StringIO(call))
    assert cli.main(["hook", "guarded-labels"]) == 2
    rc, out, err = chain.run_module("claude_ops.hooks.guarded_labels", ["--config", str(f)], call)
    assert rc == 2 and err.startswith("Blocked:") and out == ""
