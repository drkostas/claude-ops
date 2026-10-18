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
                 checks=(("Private terms", "pass"),), clone_times_out=False):
        self.files, self.log = files, log
        self.on_main, self.secrets = set(on_main), set(secrets)
        self.visibility, self.cmds, self.stdin = visibility, [], {}
        self.branch_files = set()
        self.checks = [{"workflow": w, "bucket": b} for w, b in checks]
        self.merged = False
        self.clone_times_out = clone_times_out

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
        elif args[:2] == ["gh", "api"] and "/contents/" in args[-1] and "-X" not in args:
            repo, path = args[-1].split("/contents/")
            repo, path = repo.removeprefix("repos/"), path.split("?")[0]
            if repo == REFERENCE:
                out = json.dumps({"content": base64.b64encode(REFERENCE_TEXT[path].encode()).decode()})
            elif path in self.on_main:
                out = json.dumps({"content": base64.b64encode(b"x").decode()})
            else:
                rc, out = 1, "gh: Not Found (HTTP 404)"
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
