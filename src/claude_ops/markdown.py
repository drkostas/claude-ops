"""Join the lines of each paragraph in markdown text written for GitHub.

GitHub shows every line break in the body of a pull request, an issue, a comment or a release as a
break, unlike a markdown file, which joins a paragraph's lines. Text wrapped at 72 or 100 columns,
the way commit messages are written, then reads with every sentence cut in the middle of a line, and
on a phone each cut leaves half a line empty. A pull request opened with `gh pr create --fill`, or
with a commit message as its body, looks like that.

`unwrap(text)` joins the lines of each paragraph and of each list item into one line and leaves
everything else as it was: fenced and indented code, headings, tables, block quotes, HTML (such as
`<!-- a marker -->`), horizontal rules, blank lines, a line ending in a hard break (two spaces or a
backslash), and trailer lines such as `Co-Authored-By: someone` or `Closes #12`. `wrapped(text)`
lists the lines that would be joined, so a check can say where.

    from claude_ops import markdown
    markdown.unwrap("one sentence\\nwrapped here.\\n\\n- an item\\n  wrapped too")
    # 'one sentence wrapped here.\\n\\n- an item wrapped too'
"""
from __future__ import annotations

import re

#: a fence that opens or closes a code block, also written with escaped backticks (\`\`\`), which
#: GitHub shows as text but its writer meant as code
FENCE = re.compile(r"^\s{0,3}(```|~~~|\\`\\`\\`)")
#: ⛔ ONLY A LONG LINE WAS WRAPPED. Wrapped text fills its lines to near the wrap width (72 or 100);
#: a short line followed by another is a line its writer meant as its own (a list without markers, one
#: check per line), and joining those would make the text worse, not better
MIN_WRAPPED = 45
#: ⛔ AND NO EDITOR WRAPS THIS LONG. Wrap widths are 72, 80 or 100; a longer line is a paragraph
#: already on one line (or one this module joined), so the line after it is not its continuation.
#: Without this a second unwrap joined what the first had left apart, and text unwrap had written was
#: read as wrapped again (two joined lines of 332 and 434 characters)
MAX_WRAPPED = 120
#: a line that starts a block of its own, so it is never joined to the line before
BLOCK = re.compile(r"^\s*(?:[-*+]\s|\d{1,9}[.)]\s|#{1,6}(?:\s|$)|>|\||<)")
#: a horizontal rule
RULE = re.compile(r"^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$")
#: a trailer line, as git and GitHub write them (Co-Authored-By: x, Signed-off-by: y)
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*:\s+\S")
#: a line that closes or names an issue, which GitHub reads on a line of its own
ISSUE_LINE = re.compile(r"^(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|see)\s+(?:[\w.-]+/[\w.-]+)?#\d+",
                        re.IGNORECASE)
#: a block that is never continued by the next line
CLOSED = re.compile(r"^\s*(?:#{1,6}(?:\s|$)|\||<)")


def _hard_break(line: str) -> bool:
    return line.endswith("  ") or line.endswith("\\")


def _stands_alone(stripped: str) -> bool:
    return bool(TRAILER.match(stripped) or ISSUE_LINE.match(stripped))


def unwrap(text: str) -> str:
    """`text` with the lines of each paragraph and list item joined into one line. It is idempotent:
    `unwrap(unwrap(t)) == unwrap(t)`, so text it wrote is never read as wrapped (is_wrapped)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    while True:
        joined = _unwrap_once(text)
        if joined == text:
            return joined
        text = joined


def _unwrap_once(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    joinable = False            # whether the last line out may take the next one
    last = ""                   # the line before, as written
    for line in lines:
        before, last = last, line
        if FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            joinable = False
            continue
        if in_fence:
            out.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            out.append(line)
            joinable = False
            continue
        if line.startswith("    ") and not joinable:
            out.append(line)            # indented code, after a blank line or a block
            continue
        alone = _stands_alone(stripped)
        if (joinable and MIN_WRAPPED <= len(before.rstrip()) <= MAX_WRAPPED and not alone
                and not BLOCK.match(line) and not RULE.match(line)):
            out[-1] = out[-1].rstrip() + " " + stripped
            joinable = not _hard_break(line)
            continue
        out.append(line)
        joinable = not (alone or CLOSED.match(line) or RULE.match(line) or _hard_break(line))
    return "\n".join(out)


def wrapped(text: str, limit: int = 3) -> list[str]:
    """Up to `limit` lines of `text` that `unwrap` would join to the line before, as they stand."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    joined = unwrap(text).split("\n")
    if len(joined) == len(lines):
        return []
    found, j = [], 0
    for i, line in enumerate(lines):
        if j < len(joined) and joined[j] == line:
            j += 1
            continue
        if j < len(joined) and joined[j].startswith(line.rstrip()) and line.strip():
            j += 1                       # the start of a joined paragraph
            continue
        if line.strip():
            found.append(line)
        if len(found) >= limit:
            break
    return found


def is_wrapped(text: str) -> bool:
    """Whether `text` has a paragraph or list item broken across lines."""
    return unwrap(text) != (text or "").replace("\r\n", "\n").replace("\r", "\n")
