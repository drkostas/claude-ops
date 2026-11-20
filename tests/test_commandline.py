"""Reading a command line: here-document bodies are data, not shell words."""
import os

from claude_ops.hooks import commandline


def test_a_heredoc_body_is_blanked_and_every_position_kept():
    line = "git commit -q -F - <<'EOF'\nTurn off the open Wi-Fi hotspot, and don't open the port\nEOF\nopen -a Safari"
    got = commandline.without_heredoc_bodies(line)
    assert len(got) == len(line)
    assert "open Wi-Fi" not in got and "don't" not in got
    assert got.index("open -a Safari") == line.index("open -a Safari")
    assert got.startswith("git commit -q -F - <<'EOF'\n") and "\nEOF\n" in got


def test_every_kind_of_heredoc_and_the_newlines_stay():
    for line in ('cat > notes.md <<EOF\nopen the door\nEOF\n',
                 'python3 - <<"PY" | tail -3\nprint("open x")\nPY\n',
                 'cat <<-END\n\topen y\n\tEND\n'):
        got = commandline.without_heredoc_bodies(line)
        assert len(got) == len(line) and got.count("\n") == line.count("\n")
        assert "open" not in got.split("\n", 1)[1], (line, got)
    assert "| tail -3" in commandline.without_heredoc_bodies('python3 - <<"PY" | tail -3\nx\nPY\n')


def test_a_line_with_no_heredoc_is_unchanged():
    for line in ("open -a Safari", "echo 'a << b'", "git log --oneline -3 && open https://example.org", ""):
        assert commandline.without_heredoc_bodies(line) == line


def test_written_paths_names_every_file_the_line_writes_or_edits(tmp_path):
    def got(line):
        return {os.path.relpath(p, tmp_path) for p in commandline.written_paths(line, str(tmp_path))}
    assert got("python3 fix.py > a.md && gh pr create --body-file a.md") == {"a.md"}
    assert got("echo x | tee -a b.md c.md") == {"b.md", "c.md"}
    assert got("sed -i '' 's/one/two/' d.md") == {"d.md"}
    assert got("perl -pi -e 's/one/two/' e.md") == {"e.md"}
    assert got("cat <<'EOF' > f.md\nx\nEOF") == {"f.md"}
    assert got("mkdir -p sub && cd sub && printf x >> g.md") == {"sub/g.md"}
    assert got("echo x 2> err.txt; echo y > /dev/null; sed 's/a/b/' h.md; cat i.md") == set()


def test_read_note_says_the_file_is_read_before_the_command_runs(tmp_path):
    body = str(tmp_path / "body.md")
    note = commandline.read_note(body, "gh pr create --title t --body-file body.md", str(tmp_path))
    assert note == ("That text was read from body.md as it is now, before the command runs. Change body.md "
                    "in a call of its own, then send the command again")
    note = commandline.read_note(body, "sed -i '' 's/a: b/a, b/' body.md && gh pr create --body-file body.md",
                                 str(tmp_path))
    assert note == ("This command also writes body.md, but the check reads body.md as it is now, before the "
                    "command runs. Write body.md in a call of its own, then send the command again")
    elsewhere = str(tmp_path / "sub")
    assert f"read from {body} as it is now" in commandline.read_note(body, "git commit -F ../body.md", elsewhere)


def test_each_command_carries_the_operator_after_it():
    steps = commandline.parse('cd x && gh pr checks 5 --watch 2>&1 | tail -3; echo "rc=$?" || true &\nnext')
    assert [(s["words"][0], s["after"]) for s in steps] == [
        ("cd", "&&"), ("gh", "|"), ("tail", ";"), ("echo", "||"), ("true", "&"), ("next", "")]
    assert [s["piped"] for s in steps] == [False, False, True, False, False, False]
    # shlex hands `;` and the newline after it over as one token, and a line break after && keeps the &&
    assert [s["after"] for s in commandline.parse("a;\nb &&\n  c\nd")] == [";", "&&", "\n", ""]
