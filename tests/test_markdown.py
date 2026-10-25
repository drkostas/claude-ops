"""markdown.unwrap: a paragraph written for GitHub is one line, and nothing else moves."""
from claude_ops import markdown

WRAPPED = """Round lid, two hinges, and a clasp that closes

The lid is built from simple shapes instead of the scanned one (the request, "why
not build it from plain geometric solids"). It is a disc
turned about the box's axis.

- an item that is long enough to need a second line when wrapped
  to wrap onto a second line
- a short item
1. a numbered item
wrapped lazily

Closes #12
Co-Authored-By: Someone <someone@example.org>
"""


def test_paragraphs_and_list_items_become_one_line_each():
    out = markdown.unwrap(WRAPPED)
    assert ('The lid is built from simple shapes instead of the scanned one (the request, "why not build '
            'it from plain geometric solids"). It is a disc turned about the box\'s axis.') in out
    assert ("- an item that is long enough to need a second line when wrapped to wrap onto a second line\n"
            "- a short item") in out
    assert "1. a numbered item\nwrapped lazily" in out      # a short line was meant as its own
    assert "\n\nCloses #12\nCo-Authored-By: Someone <someone@example.org>\n" in out
    assert out.startswith("Round lid, two hinges, and a clasp that closes\n\n")


def test_code_tables_headings_quotes_and_html_are_left_as_they_are():
    text = ("## Heading\nnext line\n\n```\ncode line one\ncode line two\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
            "> quoted\n> more\n\n<!-- a marker -->\nText after the marker that runs long enough to be wrapped\nstays one paragraph\n\n"
            "    indented code\n    more code\n\n---\nline after a rule")
    out = markdown.unwrap(text)
    assert "## Heading\nnext line" in out
    assert "```\ncode line one\ncode line two\n```" in out
    assert "| a | b |\n|---|---|\n| 1 | 2 |" in out
    assert "> quoted\n> more" in out
    assert "<!-- a marker -->\nText after the marker that runs long enough to be wrapped stays one paragraph" in out
    assert "    indented code\n    more code" in out
    assert "---\nline after a rule" in out


def test_a_hard_break_is_kept():
    assert markdown.unwrap("first line  \nsecond line") == "first line  \nsecond line"
    assert markdown.unwrap("first line\\\nsecond line") == "first line\\\nsecond line"


def test_text_already_unwrapped_is_unchanged_and_not_wrapped():
    text = "One paragraph on one line.\n\n- one item\n- two items\n\n```\na\nb\n```\n"
    assert markdown.unwrap(text) == text and not markdown.is_wrapped(text)
    assert markdown.is_wrapped(WRAPPED)
    assert markdown.unwrap(markdown.unwrap(WRAPPED)) == markdown.unwrap(WRAPPED)


def test_wrapped_names_the_lines_that_would_be_joined():
    lines = markdown.wrapped(WRAPPED)
    assert lines[0] == 'not build it from plain geometric solids"). It is a disc'
    assert markdown.wrapped("one line only") == []


def test_windows_line_ends_read_like_unix_ones():
    long = "a sentence long enough to have been wrapped by its writer"
    assert markdown.unwrap(long + "\r\nwrapped") == long + " wrapped"


def test_short_lines_meant_as_lines_are_kept():
    text = "Checked by hand\nfoo_test passed\nbar_test passed\n"
    assert markdown.unwrap(text) == text and not markdown.is_wrapped(text)


def test_an_escaped_fence_is_still_a_code_block():
    text = ("A long enough line of prose that the writer wrapped here because it\nkept going.\n\n"
            "\\`\\`\\`\nPOST /api/thing {a=1} gives {\"ok\": true} and the rest of a long request line\n"
            "GET /api/things\n\\`\\`\\`\n")
    out = markdown.unwrap(text)
    assert "wrapped here because it kept going." in out
    assert "the rest of a long request line\nGET /api/things" in out
