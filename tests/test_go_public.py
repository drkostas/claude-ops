"""Make a repository public with the private-terms check in place first."""
import base64
import json
import subprocess
from pathlib import Path

import pytest

from claude_ops import go_public as g

TERMS = [r"\bprojectx\b", r"\bacme\b"]
ALLOW = [r"Copyright \(c\) \d{4} Somebody"]
REFERENCE = "example/reference"
REFERENCE_TEXT = {g.FILES[0]: "name: Private terms\n", g.FILES[1]: "print('checked')\n"}


class FakeWorld:
    """gh and git as one fake `run`: the files on the default branch, the secrets, the visibility and
    every command run are kept, so a test reads what `go` did."""

    def __init__(self, files, log, *, on_main=(), secrets=(), visibility="PRIVATE",
                 checks=(("Private terms", "pass"),), clone_times_out=False, runners=(),
                 github=None, contents=None, reporting=False):
        self.files, self.log = files, log
        self.on_main, self.secrets = set(on_main), set(secrets)
        self.visibility, self.cmds, self.stdin = visibility, [], {}
        self.branch_files = set()
        self.checks = [{"workflow": w, "bucket": b} for w, b in checks]
        self.merged = False
        self.clone_times_out = clone_times_out
        self.runners = list(runners) if runners is not None else None   # None: GitHub will not list them
        # what people wrote on GitHub: REST path -> list of items, and node id -> edit revisions
        self.github = github or {}
        self.contents = contents or {}     # path on the default branch -> its text
        self.reporting = reporting

    def __call__(self, args, *, stdin=None, cwd=None, check=True):
        self.cmds.append(args)
        out, rc = "", 0
        if args[:3] == ["gh", "repo", "view"]:
            out = json.dumps({"visibility": self.visibility, "nameWithOwner": args[3],
                              "defaultBranchRef": {"name": "main"}})
        elif args[:3] == ["gh", "repo", "clone"]:
            if self.clone_times_out:
                raise subprocess.TimeoutExpired(args, 600)
            for name, text in self.files.items():
                Path(args[4], name).parent.mkdir(parents=True, exist_ok=True)
                Path(args[4], name).write_text(text)
            Path(args[4], "logo.png").write_bytes(b"\x89PNG\x00\xff projectx")
        elif args[:2] == ["git", "ls-files"]:
            out = "\0".join([*self.files, "logo.png"]) + "\0"
        elif args[:2] == ["git", "log"]:
            out = self.log
        elif args[:3] == ["gh", "secret", "set"]:
            self.secrets.add(args[3])
            self.stdin[args[3]] = stdin
        elif args[:3] == ["gh", "api", "--paginate"]:
            path = args[3].split("/", 3)[3]
            out = "\n".join(json.dumps(i) for i in self.github.get(path.split("?")[0], []))
        elif args[:3] == ["gh", "api", "graphql"]:
            ids = [a.split("=", 1)[1] for a in args if a.startswith("ids[]=")]
            nodes = [{"id": i, "userContentEdits": {"nodes": self.github.get(i, [])}} for i in ids]
            out = json.dumps({"data": {"nodes": nodes}})
        elif args[:2] == ["gh", "api"] and args[-1].endswith("/private-vulnerability-reporting"):
            if "PUT" in args:
                self.reporting = True
            out = json.dumps({"enabled": self.reporting})
        elif args[:2] == ["gh", "api"] and "/contents/" in args[-1] and "-X" not in args:
            repo, path = args[-1].split("/contents/")
            repo, path = repo.removeprefix("repos/"), path.split("?")[0]
            if repo != REFERENCE and path in self.contents:
                out = json.dumps({"content": base64.b64encode(self.contents[path].encode()).decode()})
            elif repo == REFERENCE:
                out = json.dumps({"content": base64.b64encode(REFERENCE_TEXT[path].encode()).decode()})
            elif path in self.on_main:
                out = json.dumps({"content": base64.b64encode(b"x").decode()})
            else:
                rc, out = 1, "gh: Not Found (HTTP 404)"
        elif args[:2] == ["gh", "api"] and args[2].endswith("/actions/runners"):
            rc, out = (1, "gh: Must have admin rights (HTTP 403)") if self.runners is None else (0, "\n".join(self.runners))
        elif args[:2] == ["gh", "api"] and "/actions/secrets/" in args[-1]:
            rc = 0 if args[-1].rsplit("/", 1)[-1] in self.secrets else 1
        elif args[:2] == ["gh", "api"] and args[-1].endswith("/git/ref/heads/main"):
            out = "abc123\n"
        elif args[:4] == ["gh", "api", "-X", "PUT"]:
            self.branch_files.add(args[4].split("/contents/")[1])
        elif args[:3] == ["gh", "pr", "create"]:
            out = "https://github.com/example/repo/pull/7\n"
        elif args[:3] == ["gh", "pr", "checks"] and "--json" in args:
            out = json.dumps(self.checks)
        elif args[:3] == ["gh", "pr", "merge"]:
            self.on_main |= self.branch_files
            self.merged = True
        elif args[:3] == ["gh", "repo", "edit"]:
            self.visibility = "PUBLIC"
        elif args[:3] == ["gh", "repo", "list"]:
            out = "example/checked\nexample/unchecked\n"
        if check and rc:
            raise g.Refused(f"{args[:3]} failed: {out}")
        return subprocess.CompletedProcess(args, rc, out, "")


def _go(world, **kw):
    said = []
    out = g.go("example/repo", terms=TERMS, allow=ALLOW, reference=REFERENCE, run=world,
               say=said.append, **kw)
    return out, said


def test_a_term_anywhere_stops_everything_and_its_text_is_never_printed():
    world = FakeWorld({"README.md": "A tool.\nBuilt for projectx.\n",
                       "LICENSE": "Copyright (c) 2026 Somebody, acme text allowed\n"},
                      "aaa111\0first\x01bbb222\0fix the acme bug\x01")
    with pytest.raises(g.Refused) as e:
        _go(world)
    refused = str(e.value)
    assert "README.md:2 term 1" in refused and "commit bbb222" in refused
    assert "LICENSE:1 term 2" in refused          # the allowed text is removed, the rest counts
    assert "projectx" not in refused and "acme bug" not in refused
    assert "logo.png" not in refused              # a picture is not read as text
    assert not any(c[:3] in (["gh", "secret", "set"], ["gh", "repo", "edit"]) for c in world.cmds)


def test_a_clean_repository_gets_the_secrets_then_the_check_then_goes_public():
    world = FakeWorld({"README.md": "A tool.\n"}, "aaa111\0first\x01")
    after = []
    out, _ = _go(world, after=after.append)
    assert world.secrets >= set(g.SECRETS) and world.stdin["PRIVATE_TERMS"] == "\n".join(TERMS)
    assert not any(t in " ".join(map(str, c)) for c in world.cmds for t in TERMS if c[:3] == ["gh", "secret", "set"])
    assert world.on_main >= set(g.FILES) and world.merged
    assert "--merge" in next(c for c in world.cmds if c[:3] == ["gh", "pr", "merge"])
    kinds = [c[:3] for c in world.cmds]
    assert kinds.index(["gh", "pr", "create"]) > kinds.index(["gh", "secret", "set"])
    assert world.visibility == "PUBLIC" and out["public"] and after == ["example/repo"]


def test_a_registered_self_hosted_runner_stops_it_before_it_goes_public():
    world = FakeWorld({"README.md": "A tool.\n"}, "", runners=["build-box"])
    with pytest.raises(g.Refused, match=r"1 self-hosted runner\(s\) registered \(build-box\).*fork"):
        _go(world)
    assert world.visibility == "PRIVATE" and not any(c[:3] == ["gh", "repo", "edit"] for c in world.cmds)


def test_before_public_may_remove_the_runners_and_a_runner_left_still_stops_it():
    world = FakeWorld({"README.md": "A tool.\n"}, "", runners=["build-box"])
    asked = []
    out, said = _go(world, before_public=lambda repo, names: (asked.append((repo, names)), world.runners.clear()))
    assert asked == [("example/repo", ["build-box"])] and world.visibility == "PUBLIC" and out["runners"] == []
    assert "4. no self-hosted runner is registered" in said
    world = FakeWorld({"README.md": "A tool.\n"}, "", runners=["build-box"])
    with pytest.raises(g.Refused, match="remove them first"):
        _go(world, before_public=lambda repo, names: None)
    assert world.visibility == "PRIVATE"


def test_runners_are_only_reported_with_check_only_or_when_already_public():
    world = FakeWorld({"README.md": "A tool.\n"}, "", runners=["build-box"])
    calls = []
    out, said = _go(world, check_only=True, before_public=lambda *a: calls.append(a))
    assert world.visibility == "PRIVATE" and calls == [] and out["runners"] == ["build-box"]
    assert any("they must go before it turns public" in s for s in said)
    world = FakeWorld({"README.md": "A tool.\n"}, "", visibility="PUBLIC", runners=["build-box"])
    out, said = _go(world)
    assert any("a pull request from a fork can run its jobs on them now" in s for s in said)


def test_runners_github_will_not_list_stop_it_only_when_it_would_go_public():
    world = FakeWorld({"README.md": "A tool.\n"}, "", runners=None)
    with pytest.raises(g.Refused, match="did not list the self-hosted runners"):
        _go(world)
    assert world.visibility == "PRIVATE"
    out, said = _go(FakeWorld({"README.md": "A tool.\n"}, "", runners=None), check_only=True)
    assert "4. could not list its self-hosted runners" in said and out["runners"] is None


def test_before_checks_runs_before_the_pull_request_and_only_when_one_is_needed():
    world = FakeWorld({"README.md": "A tool.\n"}, "")
    calls = []
    _go(world, check_only=True, before_checks=lambda repo, public: calls.append((repo, public, len(world.cmds))))
    made = next(i for i, c in enumerate(world.cmds) if c[:3] == ["gh", "pr", "create"])
    assert calls and calls[0][:2] == ("example/repo", False) and calls[0][2] < made
    done = FakeWorld({"README.md": "A tool.\n"}, "", on_main=g.FILES, secrets=g.SECRETS)
    calls = []
    _go(done, check_only=True, before_checks=lambda repo, public: calls.append(repo))
    assert calls == []


def test_check_only_leaves_it_private_with_the_check_in_place():
    world = FakeWorld({"README.md": "A tool.\n"}, "")
    _go(world, check_only=True)
    assert world.visibility == "PRIVATE" and not any(c[:3] == ["gh", "repo", "edit"] for c in world.cmds)
    assert world.on_main >= set(g.FILES)


def test_files_already_there_are_not_added_again():
    world = FakeWorld({"README.md": "A tool.\n"}, "", on_main=g.FILES)
    out, _ = _go(world)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in world.cmds) and not out["files_added"]


def test_an_already_public_repository_is_reported_and_the_check_still_goes_in():
    world = FakeWorld({"README.md": "Built for projectx.\n"}, "", visibility="PUBLIC",
                      checks=(("CI", "fail"), ("Private terms", "pass")))
    out, said = _go(world)
    assert out["hits"] == ["README.md:1 term 1"] and world.on_main >= set(g.FILES)
    assert "projectx" not in " ".join(said)
    assert world.merged                           # only the private-terms check counts


def test_the_check_failing_on_its_own_pull_request_stops_it():
    world = FakeWorld({"README.md": "A tool.\n"}, "", checks=(("Private terms", "fail"),))
    with pytest.raises(g.Refused, match="failed on its own pull request"):
        _go(world)
    assert not world.merged


def test_a_clone_that_times_out_refuses_while_private_and_reports_while_public():
    with pytest.raises(g.Refused, match="took longer than ten minutes"):
        _go(FakeWorld({}, "", clone_times_out=True))
    world = FakeWorld({}, "", clone_times_out=True, visibility="PUBLIC")
    out, said = _go(world)
    assert world.merged and out["hits"] == [] and "not scanned" in said[0]


def test_no_terms_is_refused():
    with pytest.raises(g.Refused, match="no terms"):
        g.go("example/repo", terms=[], allow=[], reference=REFERENCE, run=FakeWorld({}, ""), say=print)


def test_refresh_sets_the_secrets_again_only_where_the_check_runs():
    set_on = []

    def run(args, *, stdin=None, check=True, **kw):
        if args[:3] == ["gh", "repo", "list"]:
            return subprocess.CompletedProcess(args, 0, "example/checked\nexample/unchecked\n", "")
        if args[:2] == ["gh", "api"] and "/actions/secrets/" in args[-1]:
            return subprocess.CompletedProcess(args, 0 if "example/checked/" in args[-1] else 1, "", "")
        if args[:3] == ["gh", "secret", "set"]:
            set_on.append((args[5], args[3], stdin))
        return subprocess.CompletedProcess(args, 0, "", "")

    got = g.refresh("example", terms_for=lambda repo: TERMS, allow=ALLOW, run=run, say=lambda s: None)
    assert got == ["example/checked"]
    assert set_on == [("example/checked", "PRIVATE_TERMS", "\n".join(TERMS)),
                      ("example/checked", "PRIVATE_ALLOW", "\n".join(ALLOW))]


def test_sh_kills_the_whole_group_on_a_timeout(tmp_path):
    marker = tmp_path / "child.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        g.sh(["bash", "-c", f"sleep 30 & echo $! > {marker}; wait"], timeout=1)
    pid = int(marker.read_text())
    import os
    import time
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


PR = {"number": 3, "pull_request": {}, "html_url": "https://github.com/example/repo/pull/3",
      "title": "A fix", "body": "Plain words.", "node_id": "PR_3"}
TALK = {
    "issues": [PR, {"number": 4, "html_url": "https://github.com/example/repo/issues/4",
                    "title": "Crash with projectx", "body": "", "node_id": "I_4"}],
    "issues/3/timeline": [{"event": "renamed", "rename": {"from": "acme fix"}}],
    "issues/4/timeline": [],
    "pulls/3/reviews": [{"html_url": "https://github.com/example/repo/pull/3#review-1", "body": "ok",
                         "node_id": "R_1"}],
    "issues/comments": [{"html_url": "https://github.com/example/repo/pull/3#c-1", "body": "fine",
                         "node_id": "C_1"}],
    "pulls/comments": [],
    "comments": [],
    "releases": [{"tag_name": "v1.0.0", "name": "v1.0.0", "body": "Thanks to acme.",
                  "html_url": "https://github.com/example/repo/releases/tag/v1.0.0"}],
    "PR_3": [{"diff": "First draft for projectx.", "editedAt": "2026-01-02T03:04:05Z", "deletedAt": None},
             {"diff": None, "editedAt": "2026-01-01T00:00:00Z", "deletedAt": "2026-01-03T00:00:00Z"}],
}


def test_survey_reads_files_commits_and_github_text_and_changes_nothing():
    world = FakeWorld({"README.md": "Built for projectx.\n"}, "aaa111\0first\x01", github=TALK)
    said = []
    out = g.survey("example/repo", terms=TERMS, allow=ALLOW, run=world, say=said.append)
    hits = out["hits"]
    assert "README.md:1 term 1" in hits
    assert "issue #4 title term 1 https://github.com/example/repo/issues/4" in hits
    assert "pull request #3 earlier title term 2 https://github.com/example/repo/pull/3" in hits
    assert "release v1.0.0 term 2 https://github.com/example/repo/releases/tag/v1.0.0" in hits
    edit = [h for h in hits if "edit revision" in h]
    assert len(edit) == 1 and edit[0].startswith("pull request #3 description edit revision of 2026-01-02")
    assert "cannot be deleted through the API" in edit[0]   # a person's step, said with its link
    text = " ".join(hits + said)
    assert "projectx" not in text and "acme" not in text and "First draft" not in text
    changed = [c for c in world.cmds if c[:3] in (["gh", "secret", "set"], ["gh", "repo", "edit"],
                                                  ["gh", "pr", "create"]) or "-X" in c]
    assert changed == [] and world.visibility == "PRIVATE" and out["public"] is False


def test_github_text_is_scanned_only_when_asked_and_then_stops_a_private_repository():
    quiet = FakeWorld({"README.md": "A tool.\n"}, "", github=TALK)
    _go(quiet, check_only=True)
    assert not any(c[:3] == ["gh", "api", "--paginate"] for c in quiet.cmds)
    loud = FakeWorld({"README.md": "A tool.\n"}, "", github=TALK)
    with pytest.raises(g.Refused) as e:
        _go(loud, github=True)
    assert "issue #4 title term 1" in str(e.value) and "projectx" not in str(e.value)
    assert loud.visibility == "PRIVATE" and not any(c[:3] == ["gh", "secret", "set"] for c in loud.cmds)


def test_clean_github_text_is_said_in_step_one():
    world = FakeWorld({"README.md": "A tool.\n"}, "", github={"issues": [PR], "issues/3/timeline": [],
                                                              "pulls/3/reviews": []})
    out, said = _go(world, github=True)
    assert "issues, pull requests, comments, reviews and releases" in said[0] and out["public"]


def test_private_reporting_is_turned_on_when_security_md_points_to_it():
    policy = "Please report it as a private security advisory on GitHub, not as a public issue.\n"
    world = FakeWorld({"README.md": "A tool.\n"}, "", contents={"SECURITY.md": policy})
    out, said = _go(world, private_reporting=True)
    assert out["reporting"] is True and world.reporting and "5. private vulnerability reporting is on" in said
    elsewhere = FakeWorld({"README.md": "A tool.\n"}, "", contents={".github/SECURITY.md": policy})
    assert _go(elsewhere, private_reporting=True)[0]["reporting"] is True


def test_private_reporting_is_left_alone_without_a_pointer_with_check_only_or_unasked():
    mail = FakeWorld({"README.md": "A tool.\n"}, "", contents={"SECURITY.md": "Write to security@example.org.\n"})
    out, said = _go(mail, private_reporting=True)
    assert out["reporting"] is False and not mail.reporting and "does not point to it" in said[-1]
    policy = {"SECURITY.md": "Use GitHub's private vulnerability reporting.\n"}
    kept = FakeWorld({"README.md": "A tool.\n"}, "", contents=policy)
    out, _ = _go(kept, private_reporting=True, check_only=True)
    assert out["reporting"] is None and not kept.reporting
    plain = FakeWorld({"README.md": "A tool.\n"}, "", contents=policy)
    out, _ = _go(plain)
    assert out["reporting"] is None and not plain.reporting
    assert not any("private-vulnerability-reporting" in " ".join(c) for c in plain.cmds)


def test_edit_history_asks_fifty_nodes_a_query():
    world = FakeWorld({}, "", github={f"N_{i}": [{"diff": "x", "editedAt": "t", "deletedAt": None}]
                                      for i in range(120)})
    got = g.edit_history({f"N_{i}": ("comment", f"u{i}") for i in range(120)}, run=world)
    asked = [c for c in world.cmds if c[:3] == ["gh", "api", "graphql"]]
    assert [sum(a.startswith("ids[]=") for a in c) for c in asked] == [50, 50, 20]
    assert len(got) == 120 and got[0] == {"place": "comment", "url": "u0", "text": "x", "edit": "t"}
