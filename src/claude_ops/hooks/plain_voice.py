"""Keep text written for GitHub readable as it will be shown (a PreToolUse hook).

GitHub shows every line break in the body of a pull request, an issue, a comment or a release. Text
wrapped at 72 or 100 columns, as a commit message is, then reads with every sentence cut in the middle
of a line, and on a phone each cut leaves half a line empty. A model often writes a pull request's
body from its commit message, so this hook refuses a body with a paragraph broken across lines
(exit 2, with the reason sent back to the model) and saves the same text with its paragraphs joined
(`markdown.unwrap`) to a file the model can send instead.

It also refuses a phrase a person does not want in what they read, from a list in a JSON file: a
pattern, and the words that say why and what to write instead. The patterns are matched against the
prose only: fenced and indented code, inline code, links' targets, bare URLs, HTML comments and table
rows are left out first, and `allow` patterns are removed before matching.

    {
      "patterns": [{"regex": "\\u2014", "why": "an em-dash; use a comma or a full stop"}],
      "allow": ["\\b\\d{1,2}:\\d{2}\\b"]
    }

It reads the text of `gh pr|issue|release|repo` commands that write (title, body, notes, and their
files or here-documents), `gh api` calls to a repository, and the text arguments of GitHub MCP calls
that write, with the command-line reading the public-voice hook uses. Commit messages are not
checked: git shows them as they were written. The configuration is `--config`, else
`CLAUDE_OPS_PLAIN_VOICE_CONFIG`, else `~/.config/claude-ops/plain-voice.json`; without it, only the
line-break check runs. On anything unexpected the call goes through: a broken hook must never stop
work.

    python -m claude_ops.hooks.plain_voice [--config FILE]     # reads the tool call on stdin
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile

from claude_ops import markdown
from claude_ops.hooks.public_voice import GITHUB_READS, published

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


def texts_of(payload: dict) -> list[tuple[str, str]]:
    """(where, text) for every text a tool call would show on GitHub."""
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    if tool == "Bash":
        cwd = payload.get("cwd") or os.getcwd()
        return [(where, text) for where, _target, text in published(str(ti.get("command", "")), cwd)
                if where.startswith("the gh")]
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


def check(payload: dict, cfg: Config) -> str | None:
    """Why this tool call is refused, or None."""
    for where, text in texts_of(payload):
        why = check_text(text, cfg, where)
        if why:
            return why
    return None


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
        why = check(payload, cfg)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
