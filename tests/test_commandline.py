"""Reading a command line: here-document bodies are data, not shell words."""
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
