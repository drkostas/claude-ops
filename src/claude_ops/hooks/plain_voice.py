"""Keep text written for GitHub readable as it will be shown (a PreToolUse hook).

GitHub shows every line break in the body of a pull request, an issue, a comment or a release. Text
wrapped at 72 or 100 columns, as a commit message is, then reads with every sentence cut in the middle
of a line, and on a phone each cut leaves half a line empty. A model often writes a pull request's
body from its commit message, so this hook joins such a body before the call runs: the same text with
its paragraphs joined (`markdown.unwrap`) is saved to a file, and the call is changed to send that file
(an `updatedInput`, with a line of `additionalContext` saying so). The hook gives no permission of its
own, so the person's permission rules judge the changed call as they would the original.

A GitHub MCP call gets the joined text in place of its own. A gh command is changed word for word:
an inline `--body` or `--notes` becomes `--body-file` or `--notes-file` with the saved file, a body
file or a here-document (`--body-file -`) is pointed at the saved file (the caller's own file is never
edited, since it may be a repository's CHANGELOG), and a `gh api` field becomes `-F key=@file`. Only
the words that carry the text change, so the rest of the command runs exactly as written. When that
cannot be done exactly (the text is built with `$` or backticks, or the gh call shares its line with
another command), the call is refused as before (exit 2), naming the saved file to send instead.

It also refuses a phrase a person does not want in what they read, in the joined text too, from a list in a JSON file: a
pattern, and the words that say why and what to write instead. The patterns are matched against the
prose only: fenced and indented code, inline code, links' targets, bare URLs, HTML comments and table
rows are left out first, and `allow` patterns are removed before matching.

    {
      "patterns": [{"regex": "\\u2014", "why": "an em-dash; use a comma or a full stop"}],
      "allow": ["\\b\\d{1,2}:\\d{2}\\b"]
    }

It reads the text of `gh pr|issue|release|repo` commands that write (title, body, notes, and their
files or here-documents), `gh api` calls to a repository, and the text arguments of GitHub MCP calls
that write, with the command-line reading the public-voice hook uses. A `gh api --input` file or
here-document that is a JSON object is judged by its title and body values only, so the JSON's own
`"key": ` separators never read as prose; input that is not JSON is judged as text. Commit messages are not
checked: git shows them as they were written. The configuration is `--config`, else
`CLAUDE_OPS_PLAIN_VOICE_CONFIG`, else `~/.config/claude-ops/plain-voice.json`; without it, only the
line-break check runs. On anything unexpected the call goes through: a broken hook must never stop
work.

    python3 -P -m claude_ops.hooks.plain_voice [--config FILE]     # reads the tool call on stdin
"""
from __future__ import annotations

import hashlib
import shlex
import json
import os
import re
import sys
import tempfile

from claude_ops import markdown
from claude_ops.hooks.public_voice import GH_FILE_FLAGS, GITHUB_READS, published

DEFAULT_CONFIG = "~/.config/claude-ops/plain-voice.json"
#: where the joined text of a refused body is saved, for the model to send instead
SAVED = os.path.join(tempfile.gettempdir(), "claude-ops-unwrapped")
#: the keys of a GitHub MCP call whose value is text a person reads on GitHub
MCP_TEXT_KEYS = ("body", "title", "comment")
#: what is not prose: fenced code, inline code, link targets, bare URLs, HTML comments, table rows
_FENCED = re.compile(r"^\s{0,3}(```|~~~).*?^\s{0,3}\1[^\n]*$", re.S | re.M)
_INDENTED = re.compile(r"^(?: {4}|\t).*$", re.M)
_INLINE = re.compile(r"`[^`\n]*`")
_LINK_TARGET = re.compile(r"\]\([^)\s]*\)")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TABLE = re.compile(r"^\s*\|.*$", re.M)


class Config:
    def __init__(self, patterns=(), allow=()):
        self.patterns = [(re.compile(p["regex"]), str(p.get("why") or "a phrase on the list"))
                         for p in patterns if isinstance(p, dict) and p.get("regex")]
        self.allow = [re.compile(a) for a in allow if a]


def load(argv: list[str]) -> Config:
    path = None
    if "--config" in argv and argv.index("--config") + 1 < len(argv):
        path = argv[argv.index("--config") + 1]
    path = os.path.expanduser(path or os.environ.get("CLAUDE_OPS_PLAIN_VOICE_CONFIG") or DEFAULT_CONFIG)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return Config()
    return Config(data.get("patterns") or [], data.get("allow") or [])


def prose(text: str) -> str:
    """`text` without what is not prose: code, link targets, URLs, HTML comments and table rows."""
    for pattern in (_FENCED, _COMMENT, _INLINE, _LINK_TARGET, _URL, _TABLE, _INDENTED):
        text = pattern.sub(" ", text)
    return text


def phrase_hit(text: str, cfg: Config) -> tuple[str, str] | None:
    """(the line, why) for the first listed phrase in the prose of `text`, or None."""
    if not cfg.patterns:
        return None
    for line in prose(text).splitlines():
        cleaned = line
        for a in cfg.allow:
            cleaned = a.sub(" ", cleaned)
        for pattern, why in cfg.patterns:
            if pattern.search(cleaned):
                return line.strip(), why
    return None


def save(text: str) -> str:
    """Write `text` to a file named after its contents, and return its path."""
    os.makedirs(SAVED, exist_ok=True)
    path = os.path.join(SAVED, hashlib.sha256(text.encode()).hexdigest()[:12] + ".md")
    with open(path, "w") as f:
        f.write(text)
    return path


#: an inline text flag of a gh command, and the flag that reads the same text from a file
FILE_FOR = {"--body": "--body-file", "-b": "--body-file", "--notes": "--notes-file", "-n": "--notes-file"}
#: the fields of a `gh api` call that carry text
API_FIELDS = ("-f", "--raw-field", "-F", "--field")
#: what the shell reads apart from words, which a call this hook may change never holds
_OPERATORS = set(";|&()<>`\n")
_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
#: what said a person's text was joined, for the model's next call
JOINED_NOTE = ("The text this call sends to GitHub had paragraphs broken across lines, so they were joined "
               "before it was sent ({path}). GitHub shows every line break, so write each paragraph on one "
               "line next time.")


class Word:
    """A word of a shell command: where it stands in the command, what the shell passes, and whether
    that is all the shell does with it (no expansion, glob or tilde to apply)."""

    def __init__(self, start: int, end: int, value: str, plain: bool):
        self.start, self.end, self.value, self.plain = start, end, value, plain


def words(command: str) -> tuple[list[Word], str | None, bool] | None:
    """The words of a one-line shell command (a quoted word may hold newlines), with the text of the
    here-document it reads, if any, and whether that text is passed as written (a quoted delimiter).
    None for anything else: an operator, a redirection other than a here-document, or quotes left open."""
    out: list[Word] = []
    i, n, delimiter, literal = 0, len(command), None, True
    while i < n:
        c = command[i]
        if c in " \t":
            i += 1
            continue
        if c == "\n":
            rest = command[i + 1:]
            if delimiter is None:
                return (out, None, True) if not rest.strip() else None
            lines = rest.split("\n")
            ends = [k for k, line in enumerate(lines) if line.strip("\t") == delimiter]
            if not ends or any(line.strip() for line in lines[ends[0] + 1:]):
                return None
            return out, "".join(line + "\n" for line in lines[:ends[0]]), literal
        if command.startswith("<<", i) and delimiter is None:
            i += 2 + (command[i + 2:i + 3] == "-")
            while i < n and command[i] in " \t":
                i += 1
            m = re.match(r"(['\"]?)([A-Za-z_]\w*)\1", command[i:])
            if not m:
                return None
            delimiter, literal = m.group(2), bool(m.group(1))
            i += m.end()
            continue
        if c in _OPERATORS:
            return None
        start, value, plain = i, [], True
        while i < n and command[i] not in " \t\n":
            c = command[i]
            if c in _OPERATORS:
                return None
            if c == "'":
                j = command.find("'", i + 1)
                if j < 0:
                    return None
                value.append(command[i + 1:j])
                i = j + 1
            elif c == '"':
                i += 1
                while i < n and command[i] != '"':
                    if command[i] == "\\" and i + 1 < n and command[i + 1] in '"\\$`\n':
                        value.append(command[i + 1])
                        i += 2
                        continue
                    plain = plain and command[i] not in "$`"
                    value.append(command[i])
                    i += 1
                if i >= n:
                    return None
                i += 1
            elif c == "\\":
                if i + 1 >= n:
                    return None
                value.append(command[i + 1])
                i += 2
            else:
                plain = plain and c not in "$*?[{" and not (c == "~" and i == start)
                value.append(c)
                i += 1
        out.append(Word(start, i, "".join(value), plain))
    return (out, None, True) if delimiter is None else None


def join_command(command: str, cwd: str) -> tuple[str, list[str]] | None:
    """`command`, a gh call, with each text it sends that has a paragraph broken across lines replaced
    by a saved file holding the joined text, and the files saved; None when that cannot be done word for
    word. Only the words that carry a text are changed."""
    got = words(command)
    if got is None:
        return None
    ws, heredoc, literal = got
    g = 0
    while g < len(ws) and _ASSIGNMENT.match(ws[g].value):
        g += 1
    if g >= len(ws) or os.path.basename(ws[g].value) != "gh" or g + 1 >= len(ws):
        return None
    api = ws[g + 1].value == "api"
    edits: list[tuple[int, int, str]] = []
    saved: list[str] = []

    def joined(text: str) -> str:
        path = save(markdown.unwrap(text))
        saved.append(path)
        return path

    def file_text(w: Word) -> str | None:
        if w.value == "-":
            return heredoc if heredoc is not None and literal else None
        if not w.plain:
            return None
        try:
            with open(os.path.join(cwd, w.value), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    i = g + 2
    while i < len(ws):
        w, nxt = ws[i], (ws[i + 1] if i + 1 < len(ws) else None)
        flag, _, attached = w.value.partition("=")
        if api and nxt is not None and w.value in API_FIELDS:
            key, _, value = nxt.value.partition("=")
            text = file_text(Word(0, 0, value[1:], nxt.plain)) if w.value in ("-F", "--field") and value.startswith("@") \
                else (value if nxt.plain else None)
            if text is not None and markdown.is_wrapped(text):
                edits += [(w.start, w.end, "-F"), (nxt.start, nxt.end, shlex.quote(f"{key}=@{joined(text)}"))]
            elif text is None and markdown.is_wrapped(value):
                return None
            i += 2
            continue
        if not api and nxt is not None and w.value in FILE_FOR:
            if markdown.is_wrapped(nxt.value):
                if not nxt.plain:
                    return None
                edits += [(w.start, w.end, FILE_FOR[w.value]), (nxt.start, nxt.end, shlex.quote(joined(nxt.value)))]
            i += 2
            continue
        if not api and attached and flag in FILE_FOR:
            if markdown.is_wrapped(attached):
                if not w.plain:
                    return None
                edits.append((w.start, w.end, shlex.quote(f"{FILE_FOR[flag]}={joined(attached)}")))
            i += 1
            continue
        if not api and nxt is not None and w.value in GH_FILE_FLAGS:
            text = file_text(nxt)
            if text is not None and markdown.is_wrapped(text):
                edits.append((nxt.start, nxt.end, shlex.quote(joined(text))))
            i += 2
            continue
        if not api and attached and flag in GH_FILE_FLAGS:
            text = file_text(Word(0, 0, attached, w.plain))
            if text is not None and markdown.is_wrapped(text):
                edits.append((w.start, w.end, shlex.quote(f"{flag}={joined(text)}")))
            i += 1
            continue
        i += 1
    if not edits:
        return None
    for start, end, new in sorted(edits, reverse=True):
        command = command[:start] + new + command[end:]
    return command, saved


def joined_input(payload: dict) -> tuple[dict, list[str]] | None:
    """The tool input with every text it sends to GitHub joined, and the files saved, or None when it
    cannot be changed exactly or a text is still broken across lines afterwards."""
    tool, ti = payload.get("tool_name", ""), dict(payload.get("tool_input") or {})
    if tool.startswith("mcp__github__"):
        keys = [k for k in MCP_TEXT_KEYS if isinstance(ti.get(k), str) and markdown.is_wrapped(ti[k])]
        if not keys:
            return None
        return {**ti, **{k: markdown.unwrap(ti[k]) for k in keys}}, [save(markdown.unwrap(ti[keys[0]]))]
    if tool != "Bash":
        return None
    got = join_command(str(ti.get("command", "")), payload.get("cwd") or os.getcwd())
    if got is None:
        return None
    new = {**ti, "command": got[0]}
    if any(markdown.is_wrapped(text) for _where, text in texts_of({**payload, "tool_input": new})):
        return None
    return new, got[1]


#: the keys of a `gh api --input` JSON object whose text GitHub shows to a reader
SHOWN_KEYS = ("title", "body")


def shown(text: str) -> list[str]:
    """The texts of a `gh api` input that GitHub shows: the title and body string values when it is a
    JSON object, so the JSON's own `"key": ` separators are never judged as prose, else the text."""
    try:
        data = json.loads(text)
    except ValueError:
        return [text]
    if not isinstance(data, dict):
        return [text]
    return [data[k] for k in SHOWN_KEYS if isinstance(data.get(k), str)]


def texts_of(payload: dict) -> list[tuple[str, str]]:
    """(where, text) for every text a tool call would show on GitHub."""
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    if tool == "Bash":
        cwd = payload.get("cwd") or os.getcwd()
        out = []
        for where, _target, text in published(str(ti.get("command", "")), cwd):
            if where == "the gh api call":
                out += [(where, t) for t in shown(text)]
            elif where.startswith("the gh"):
                out.append((where, text))
        return out
    if tool.startswith("mcp__github__") and not tool[len("mcp__github__"):].startswith(GITHUB_READS):
        return [("the GitHub call", ti[k]) for k in MCP_TEXT_KEYS if isinstance(ti.get(k), str)]
    return []


def check_text(text: str, cfg: Config, where: str = "the text") -> str | None:
    """Why `text` may not go to GitHub, or None."""
    if markdown.is_wrapped(text):
        at = (markdown.wrapped(text, 1) or [""])[0]
        path = save(markdown.unwrap(text))
        return (f"{where} has a paragraph broken across lines, at {at!r}. GitHub shows every line "
                f"break, so each sentence is cut in the middle of a line, and on a phone half the line "
                f"stays empty. Write each paragraph on one line. The same text with its paragraphs "
                f"joined is saved in {path}; send that file instead (for gh, --body-file {path})")
    hit = phrase_hit(text, cfg)
    if hit:
        return f"{where} has {hit[1]}, in {hit[0]!r}. Rewrite that sentence and send it again"
    return None


def review(payload: dict, cfg: Config) -> tuple[str | None, dict | None, str]:
    """(why this tool call is refused, None, "") or (None, the tool input with its texts joined, the
    note for the model) or (None, None, "") when the call goes as it is. A listed phrase is looked for in
    the joined text, so joining never lets one through."""
    texts = texts_of(payload)
    for where, text in texts:
        hit = phrase_hit(markdown.unwrap(text) if markdown.is_wrapped(text) else text, cfg)
        if hit:
            return f"{where} has {hit[1]}, in {hit[0]!r}. Rewrite that sentence and send it again", None, ""
    wrapped = [(where, text) for where, text in texts if markdown.is_wrapped(text)]
    if not wrapped:
        return None, None, ""
    got = joined_input(payload)
    if got is None:
        return check_text(wrapped[0][1], cfg, wrapped[0][0]), None, ""
    return None, got[0], JOINED_NOTE.format(path=", ".join(got[1]))


def check(payload: dict, cfg: Config) -> str | None:
    """Why this tool call is refused, or None (a text that can be joined is not refused)."""
    return review(payload, cfg)[0]


def main(argv: list[str] | None = None) -> int:
    """As a hook, the tool call on stdin. With `--file PATH`, the text in that file: a script that
    posts to GitHub itself (no tool call a hook could see) checks its body before it sends it. A file
    that cannot be read is refused (exit 2), where a broken hook lets the call through."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--file" in argv:
        i = argv.index("--file")
        try:
            with open(argv[i + 1], encoding="utf-8") as f:
                why = check_text(f.read(), load(argv))
        except (IndexError, OSError) as e:
            print(f"Blocked: the text to check could not be read ({e}).", file=sys.stderr)
            return 2
        if why:
            print(f"Blocked: {why}.", file=sys.stderr)
            return 2
        return 0
    try:
        cfg = load(argv)
        payload = json.load(sys.stdin)
        why, updated, note = review(payload, cfg)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}.", file=sys.stderr)
        return 2
    if updated is not None:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated,
                                                 "additionalContext": note}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
