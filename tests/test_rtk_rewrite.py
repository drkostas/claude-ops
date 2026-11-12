"""The rtk-rewrite hook: which commands get their rtk form, and which run as written.

The cases are the ones a shell version of this hook was tested with, so the two give the same
rewrites, plus the message-file commit, which goes to git itself here.
"""
import io
import json

import pytest

from claude_ops.hooks import rtk_rewrite as hook


def rewrite(command, cwd="", major=1):
    return hook.rewrite(command, cwd, lambda: major)


@pytest.mark.parametrize("command,want", [
    # plain grep / rg: rewritten
    ("grep foo file", "rtk grep foo file"),
    ("grep foo file | grep -v bar", "rtk grep foo file | grep -v bar"),
    ("rg foo src", "rtk grep foo src"),
    ("grep foo file 2>/dev/null | head", "rtk grep foo file 2>/dev/null | head"),
    # gh: reads go through rtk
    ("gh pr list", "rtk gh pr list"),
    ("gh pr view 12", "rtk gh pr view 12"),
    ("gh pr checks 12", "rtk gh pr checks 12"),
    ("gh run view 9", "rtk gh run view 9"),
    # the rest of the rules
    ("git status", "rtk git status"),
    ("cat file.txt", "rtk read file.txt"),
    ("ls -la", "rtk ls -la"),
    ("ls", "rtk ls"),
    ("git commit -m 'one line'", "rtk git commit -m 'one line'"),
    ("cargo test", "rtk cargo test"),
    ("vitest", "rtk vitest run"),
    ("pnpm vitest --watch", "rtk vitest run --watch"),
    ("pnpm test", "rtk vitest run"),
    ("npx tsc --noEmit", "rtk tsc --noEmit"),
    ("pnpm tsc", "rtk tsc"),
    ("npx eslint src", "rtk lint src"),
    ("pnpm lint", "rtk lint"),
    ("npx prettier --check .", "rtk prettier --check ."),
    ("pnpm playwright test", "rtk playwright test"),
    ("prisma migrate", "rtk prisma migrate"),
    ("docker ps -a", "rtk docker ps -a"),
    ("kubectl get pods", "rtk kubectl get pods"),
    ("curl -s https://example.org/", "rtk curl -s https://example.org/"),
    ("pnpm outdated", "rtk pnpm outdated"),
    ("pytest -q", "rtk pytest -q"),
    ("python -m pytest tests", "rtk pytest tests"),
    ("ruff check .", "rtk ruff check ."),
    ("pip show requests", "rtk pip show requests"),
    ("uv pip list", "rtk pip list"),
    ("go vet ./...", "rtk go vet ./..."),
    ("golangci-lint run", "rtk golangci-lint run"),
    # only the first line is rewritten
    ("git status\ngit diff", "rtk git status\ngit diff"),
    ("grep foo file\ngrep bar file", "rtk grep foo file\ngrep bar file"),
    # trailing newlines go, as a shell's $(...) drops them
    ("git status\n", "rtk git status"),
])
def test_rewritten(command, want, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)     # "src" must not be a folder here
    assert rewrite(command) == want


@pytest.mark.parametrize("command", [
    # grep / rg with any option: rtk grep rejects or reinterprets the flag
    "grep -n foo file",
    "grep -oE 'x+' file | head",
    "grep -rn foo .",
    "grep -c foo file",
    "grep -l foo *.md",
    "grep -v foo file",
    "grep -h foo file",
    "grep -A 3 foo file",
    "grep -i -w foo file",
    "grep --include=*.py foo .",
    "grep -- -x file",
    "grep 'foo -n' file",
    "rg -i foo",
    "grep -n foo file\nls",
    # gh writes, and reads whose exact output a script needs, go to gh itself
    "gh pr view 12 --json body --jq .body",
    "gh pr list --json number",
    "gh pr list --template '{{.}}'",
    "gh pr edit 12 --body-file f.md",
    "gh pr edit 12 --title t",
    "gh pr create --title t --body-file f",
    "gh pr merge 12 --merge",
    "gh pr comment 12 --body x",
    "gh issue create --title t --body b",
    # already rtk, or not a command it knows
    "rtk grep foo .",
    "/usr/local/bin/rtk git status",
    "echo hi",
    "git -C repo status",
    # a here-document anywhere
    "grep -n foo <<EOF",
    "echo a\ncat <<EOF\nx\nEOF",
    # only the first line is judged: a later line never makes a rewrite
    "F=/home/user/.cache/tool-results/mail-1793007777021.txt\n"
    "grep -oE 'https?://[A-Za-z0-9._~:/?#@!$&()*+,;=%-]*(track)[A-Za-z0-9._~:/?#@!$&()*+,;=%-]*' \"$F\" | sort -u",
    "cd /tmp/x\ncurl -s \"https://example.org/\" -o e.html\ngrep -oE 'src=\"[^\"]+\"' e.html | head -20",
    "cd /tmp/x\ngit status",
    "cd /tmp/x\ngrep foo file",
    # a message file goes to git itself
    "git commit -F msg.txt",
    "git commit -aF msg.txt",
    "git commit --file=msg.txt",
    "git commit --file msg.txt",
    # a rule that matches but would change nothing gives no answer
    "git  status",
    "cat\tfile",
    "",
])
def test_left_alone(command):
    assert rewrite(command) is None


def test_a_plain_grep_on_a_folder_gets_r_from_rtk_1(tmp_path):
    folder = tmp_path / "dir"
    folder.mkdir()
    (tmp_path / "plain.txt").write_text("x")
    assert rewrite(f"grep foo {folder}") == f"rtk grep -r foo {folder}"
    assert rewrite(f"grep foo {folder} | head") == f"rtk grep -r foo {folder} | head"
    assert rewrite(f"grep foo '{folder}'") == f"rtk grep -r foo '{folder}'"
    assert rewrite(f"grep foo {folder}/missing") == f"rtk grep foo {folder}/missing"
    assert rewrite(f"grep foo {tmp_path}/plain.txt") == f"rtk grep foo {tmp_path}/plain.txt"
    # a relative folder is read against the call's working folder
    assert rewrite("grep foo dir", cwd=str(tmp_path)) == "rtk grep -r foo dir"
    # rtk 0.x searched a folder by itself and rejects -r
    assert rewrite(f"grep foo {folder}", major=0) == f"rtk grep foo {folder}"
    assert rewrite(f"grep foo {folder}", major=None) == f"rtk grep foo {folder}"
    # a redirection in the path's place is not a path
    assert rewrite(f"grep foo >{folder}") == f"rtk grep foo >{folder}"


def test_the_version_is_asked_only_for_a_folder(tmp_path):
    asked = []
    hook.rewrite("grep foo file", "", lambda: asked.append(1) or 1)
    hook.rewrite("git status", "", lambda: asked.append(1) or 1)
    assert asked == []
    hook.rewrite(f"grep foo {tmp_path}", "", lambda: asked.append(1) or 1)
    assert asked == [1]


def test_rtk_major_reads_the_version_line(tmp_path):
    fake = tmp_path / "rtk"
    fake.write_text("#!/bin/sh\necho 'rtk 1.1.0'\n")
    fake.chmod(0o755)
    assert hook.rtk_major(str(fake)) == 1
    fake.write_text("#!/bin/sh\necho 'rtk 0.15.2'\n")
    assert hook.rtk_major(str(fake)) == 0
    fake.write_text("#!/bin/sh\necho 'something else'\n")
    assert hook.rtk_major(str(fake)) is None
    assert hook.rtk_major(str(tmp_path / "missing")) is None


@pytest.fixture
def on_path(tmp_path, monkeypatch):
    """A stand-in rtk on PATH, so the hook runs."""
    fake = tmp_path / "rtk"
    fake.write_text("#!/bin/sh\necho 'rtk 1.1.0'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    return fake


def run(payload, argv=(), monkeypatch=None):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hook.main(list(argv)) == 0
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def test_the_answer_changes_only_the_command(on_path, monkeypatch):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status", "description": "d", "timeout": 5}}
    got = run(payload, monkeypatch=monkeypatch)
    assert got == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {
        "command": "rtk git status", "description": "d", "timeout": 5}}}


def test_allow_answers_allow(on_path, monkeypatch):
    got = run({"tool_name": "Bash", "tool_input": {"command": "ls"}}, ["--allow"], monkeypatch)
    spec = got["hookSpecificOutput"]
    assert spec["permissionDecision"] == "allow" and spec["updatedInput"]["command"] == "rtk ls"


@pytest.mark.parametrize("payload", [
    {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
    {"tool_name": "Write", "tool_input": {"command": "git status"}},
    {"tool_name": "Bash", "tool_input": {}},
    {"tool_name": "Bash", "tool_input": {"command": 5}},
    {"tool_name": "Bash"},
])
def test_no_answer(on_path, monkeypatch, payload):
    assert run(payload, monkeypatch=monkeypatch) is None


def test_no_rtk_no_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert run({"tool_name": "Bash", "tool_input": {"command": "git status"}}, monkeypatch=monkeypatch) is None


def test_rtk_names_the_binary(on_path, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    assert run(payload, monkeypatch=monkeypatch) is None
    assert run(payload, ["--rtk", str(on_path)], monkeypatch)["hookSpecificOutput"]["updatedInput"] == {
        "command": "rtk git status"}


def test_a_broken_call_never_stops_work(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.main([]) == 0


@pytest.mark.parametrize("command", [
    "rbw list | rtk proxy grep pypi",
    "cat f | rtk proxy grep x",
    "grep foo file | rtk proxy grep bar",
    "rtk proxy bash -c 'rbw list | grep pypi'",
    "echo hi | env rtk proxy grep hi",
])
def test_it_never_refuses(on_path, monkeypatch, command):
    got = run({"tool_name": "Bash", "tool_input": {"command": command}}, ["--allow"], monkeypatch)
    assert got is None or got["hookSpecificOutput"].get("permissionDecision") != "deny"
