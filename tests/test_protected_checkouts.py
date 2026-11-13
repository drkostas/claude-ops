"""The protected-checkouts hook: a production checkout is changed only by its deploy step."""
import io
import json
import re

import pytest

from claude_ops.hooks import commandline, protected_checkouts as pc

PATTERNS = [re.compile(r"/worktrees/[^/]+-(live|next)(/|$)")]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A home folder with two protected checkouts and a session's own worktree in it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("app-live", "lib-next", "app-topic"):
        (tmp_path / "worktrees" / name).mkdir(parents=True)
    return tmp_path


def bash(command, cwd):
    return pc.check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}, PATTERNS)


@pytest.mark.parametrize("command", [
    'L=~/worktrees/app-live; SHA=abc; git -C "$L" checkout "$SHA"',
    'export L=$HOME/worktrees/app-live\ngit -C "${L}" reset --hard',
    'W=~/worktrees; cd "$W/app-live" && git pull',
    'for x in a; do git -C ~/worktrees/app-live checkout x; done',
    '(cd ~/worktrees/lib-next && git merge y)',
    'git -C ~/worktrees/app-live checkout x',
    'cd ~/worktrees/app-live && git checkout x',
    'git --work-tree ~/worktrees/app-live checkout x',
    'git -C ~/worktrees/app-topic worktree remove ~/worktrees/app-live',
    'git -C ~/worktrees/app-live stash',
    "cat > ~/worktrees/app-live/x.py <<'EOF'\nprint(1)\nEOF",
    'echo hi >> ~/worktrees/lib-next/notes.txt',
])
def test_a_change_to_a_protected_checkout_is_refused(home, command):
    assert bash(command, home), command


@pytest.mark.parametrize("command", [
    'L=~/worktrees/app-topic; git -C "$L" commit -m x',
    'L=~/worktrees/app-live; git -C "$L" log --oneline -3',
    'git -C ~/worktrees/app-live status && git -C ~/worktrees/app-live show HEAD:README.md',
    'git -C ~/worktrees/app-live fetch origin',
    'git -C ~/worktrees/app-live stash list',
    'git -C ~/worktrees/app-live worktree add ~/worktrees/app-x origin/main',
    'git -C "$UNKNOWN" checkout x',
    'cd ~/worktrees/app-topic && git checkout -b feat/x',
    # a here-document's body is data: a test file that names these commands is not one
    "cat > ~/worktrees/app-topic/t.py <<'EOF'\ngit -C ~/worktrees/app-live checkout x\nEOF",
])
def test_reading_one_and_working_in_your_own_are_allowed(home, command):
    assert bash(command, home) is None, command


def test_an_edit_inside_one_is_refused_and_outside_is_not(home):
    def edit(path):
        return pc.check({"tool_name": "Edit", "tool_input": {"file_path": path}, "cwd": str(home)}, PATTERNS)
    assert edit(str(home / "worktrees" / "app-live" / "x.py"))
    assert edit(str(home / "worktrees" / "app-topic" / "x.py")) is None


def test_with_no_patterns_nothing_is_refused(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_OPS_PROTECTED_CHECKOUTS_CONFIG", str(tmp_path / "none.json"))
    patterns, _ = pc.config([])
    call = {"tool_name": "Bash", "tool_input": {"command": "git checkout x"},
            "cwd": str(home / "worktrees" / "app-live")}
    assert patterns == [] and pc.check(call, patterns) is None


def test_the_patterns_come_from_the_file_or_the_command_line(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"patterns": ["-live(/|$)"], "message": "Use the deploy."}))
    pats, msg = pc.config(["--config", str(f)])
    assert [p.pattern for p in pats] == ["-live(/|$)"] and msg == "Use the deploy."
    assert [p.pattern for p in pc.config(["--pattern", "x-prod/"])[0]] == ["x-prod/"]


def test_main_refuses_with_exit_2_and_fails_open(home, monkeypatch, capsys):
    call = {"tool_name": "Bash", "tool_input": {"command": "git -C ~/worktrees/app-live reset --hard"},
            "cwd": str(home)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call)))
    assert pc.main(["--pattern", r"/worktrees/[^/]+-live(/|$)"]) == 2
    assert "production checkout" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert pc.main(["--pattern", "x"]) == 0


def test_commandline_follows_a_variable_the_line_assigned(home):
    invs = commandline.git_invocations('L=~/worktrees/app-live; git -C "$L" log', str(home))
    assert invs and invs[0]["dir"] == str(home / "worktrees" / "app-live")
    assert commandline.assignments(["export", "A=1", "B=$A/x"], {}) == {"A": "1", "B": "1/x"}
    assert commandline.assignments(["git", "status"], {}) is None


@pytest.fixture()
def unset(monkeypatch):
    """The variable names these lines use are not in the hook's own environment either."""
    for name in ("F", "T", "L", "X", "UNSET"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("command", [
    # a `)` glued to the separator after it hid the next command from every hook
    'L=$(cd ~/worktrees/app-live && pwd); git -C "$L" checkout x',
    'X=$(date)&& git -C ~/worktrees/app-live reset --hard',
    '(cd /tmp); git -C ~/worktrees/app-live checkout x',
    '(cd /tmp)\ngit -C ~/worktrees/app-live checkout x',
    'echo $(date)|tee ~/worktrees/app-live/a',
    'T=$(mktemp -d); git -C ~/worktrees/app-live checkout x',
    'cd ~/worktrees && echo x > app-live/kernel/a',
    # $PWD is the folder the line is in, not the hook's
    'cd ~/worktrees/app-live && echo x > "$PWD/a"',
    # a known start inside a protected folder stays refused, whatever the variable after it holds
    'cd ~/worktrees/app-live && echo x > "kernel/$UNSET"',
    'echo x > ~/worktrees/app-live/"$UNSET"',
    # an unknown start, with the rest naming a protected folder
    'echo x > "$UNSET/worktrees/app-live/x"',
])
def test_a_variable_or_a_subshell_does_not_hide_a_change(home, unset, command):
    assert bash(command, home), command


@pytest.mark.parametrize("command", [
    # the line that was refused: F holds an absolute path outside the protected folder
    'cd ~/worktrees/app-live && python3 -m tool note abc "x" | tail -1; T=$(date \'+%H:%M\'); '
    'F=~/notes/n.md; printf \'%s\\n\' "y" >> "$F"',
    # a variable nobody on the line assigned is an unknown folder, not one under the cd
    'cd ~/worktrees/app-live && printf x >> "$UNSET"',
    "T=$(mktemp -d); cd ~/worktrees/app-live && cat > \"$T/x.md\" <<'EOF'\nhi\nEOF",
])
def test_a_write_held_in_a_variable_goes_where_the_variable_points(home, unset, command):
    assert bash(command, home) is None, command


def test_an_unresolved_start_is_named_in_the_refusal(home, unset):
    why = bash('echo x > "$UNSET/worktrees/app-live/x"', home)
    assert "$UNSET/worktrees/app-live/x" in why and "name the path in full" in why


def test_commandline_splits_a_parenthesis_from_the_separator_after_it(home, unset):
    assert commandline.simple_commands("X=$(date); git status") == [["X=$", "date"], ["git", "status"]]
    assert commandline.simple_commands("(cd a)&&ls|wc") == [["cd", "a"], ["ls"], ["wc"]]
    assert commandline.split_punctuation(")&&") == [")", "&&"]
    assert commandline.split_punctuation(">(") == ["("]
    assert commandline.split_punctuation("&&") == ["&&"]
    line = 'T=$(mktemp -d); F=/tmp/n.md; cat > "$T/a.md" <<EOF\nhi\nEOF\necho y >> "$F"'
    assert [(t, text) for _here, t, text in commandline.write_targets(line, str(home))] == \
        [("$T/a.md", "hi\n"), ("/tmp/n.md", "y")]
    assert commandline.unknown_start("$T/a.md") and not commandline.unknown_start("/tmp/n.md")
    assert commandline.after_unknown("$T/worktrees/x") == "/worktrees/x"
    assert commandline.after_unknown("$(mktemp -d)/x") == "/x"
    assert commandline.after_unknown("$F") == ""
    assert commandline.writes('cd /tmp/x && echo hi > "$PWD/a"', "/") == [("/tmp/x/a", "hi")]
