"""Keep chosen text out of chosen files (a PreToolUse hook).

Some files are read again by every later session: the instructions, the skills, a session's memory
notes, a scheduled job's definition. A line written there is a standing instruction, so a path into a
folder of throwaway scripts named in one turns that folder into something every later session runs,
and nothing tracks it. A scan that reads those files afterwards finds the line only once it is saved,
when the session that wrote it has moved on. This hook refuses the write itself (exit 2, with the
reason sent back to the model):

- Write (its content), Edit (its new text), MultiEdit (each edit's new text) and NotebookEdit (the new
  cell) of a file a rule covers;
- text a Bash command writes into such a file (a here-document, `echo` or `printf` redirected, or piped
  into `tee`), read by `commandline`, shared with the other hooks. Text a program writes itself is not
  known before it runs and goes through.

Only the new text is read. An edit that leaves an old line alone goes through, and an edit whose new
text still holds such a line is refused, so the line goes the next time the text around it changes.

The rules are in a JSON file:

    {"rules": [
      {"files": ["~/.config/assistant/skills/**/*.md", "~/notes/*.md"],
       "text": "~/scratch/([A-Za-z0-9_.-]+)",
       "allow": ["README.md"],
       "message": "Name the script by what it does, and keep it where the project keeps its tools."}
    ]}

`files` are globs on the absolute path. `*` and `?` stay inside one folder, `**/` is any number of
folders (none too), and `~` is the home folder. A file is covered when its path or its real path (with
every link followed) matches, so a folder reached through a link is judged where it really is. `text` is
a regular expression, matched line by line. Its first group, or the whole match when it has none, is the
name `allow` is checked against. `message` is added to the refusal: say there what to write instead.

The file is `--config`, else `CLAUDE_OPS_KEPT_OUT_CONFIG`, else `~/.config/claude-ops/kept-out.json`.
With no rules nothing is refused. A rule that cannot be read is skipped, and on anything unexpected the
call goes through: a broken hook must never stop work.

    claude-ops hook kept-out          # reads the tool call on stdin
    python3 -P -m claude_ops.hooks.kept_out [--config FILE]
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import commandline

DEFAULT_CONFIG = "~/.config/claude-ops/kept-out.json"
CONFIG_ENV = "CLAUDE_OPS_KEPT_OUT_CONFIG"


def glob_regex(pattern: str) -> re.Pattern:
    """A glob over absolute paths as a regular expression: `**/` is any number of folders (none too),
    `**` at the end is anything, and `*` and `?` stay inside one folder."""
    p, out, i = os.path.expanduser(pattern), [], 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


class Rule:
    """One rule: the files it covers, the text kept out of them, the names allowed and what to say."""

    def __init__(self, d: dict):
        self.files = [glob_regex(str(f)) for f in d.get("files") or []]
        self.text = re.compile(str(d["text"]))
        self.allow = {str(a) for a in d.get("allow") or []}
        self.message = str(d.get("message") or "")

    def covers(self, path: str) -> bool:
        """Whether this file is one the rule keeps the text out of, by its path or its real path."""
        seen = {os.path.normpath(path)}
        try:
            seen.add(os.path.realpath(path))
        except (OSError, ValueError):
            pass
        return any(rx.match(p) for rx in self.files for p in seen)

    def named(self, text: str) -> list[tuple[int, str, str]]:
        """(line, name, the matched text) for each match in `text` whose name `allow` does not hold."""
        out = []
        for n, line in enumerate(text.splitlines(), 1):
            for m in self.text.finditer(line):
                name = (m.group(1) if self.text.groups else None) or m.group(0)
                if name not in self.allow:
                    out.append((n, name, m.group(0)))
        return out


def config_path(argv_path: str | None = None) -> str:
    return os.path.expanduser(argv_path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)


def load(path: str) -> list[Rule]:
    """The rules in this file. A missing or unreadable file has none, and a rule that cannot be read
    (no `text`, a pattern that does not compile) is left out."""
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 - no configuration, no rules
        return []
    rules = []
    for d in (data.get("rules") or []) if isinstance(data, dict) else []:
        try:
            rules.append(Rule(d))
        except Exception:  # noqa: BLE001 - one bad rule does not take the others with it
            continue
    return rules


def written(payload: dict) -> list[tuple[str, str]]:
    """(file, new text) for what this tool call would write, as absolute paths."""
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool == "Bash":
        return commandline.writes(str(ti.get("command", "")), cwd)
    path = str(ti.get("file_path") or ti.get("notebook_path") or "")
    if not path:
        return []
    path = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    if tool == "Write":
        return [(path, str(ti.get("content", "")))]
    if tool == "Edit":
        return [(path, str(ti.get("new_string", "")))]
    if tool == "MultiEdit":
        return [(path, str(e.get("new_string", ""))) for e in ti.get("edits") or [] if isinstance(e, dict)]
    if tool == "NotebookEdit":
        return [(path, str(ti.get("new_source", "")))]
    return []


def check(payload: dict, rules: list[Rule]) -> str | None:
    """Why this tool call is refused, or None."""
    if not rules:
        return None
    for path, text in written(payload):
        for rule in rules:
            if not rule.covers(path):
                continue
            hits = rule.named(text)
            if hits:
                line, _name, matched = hits[0]
                more = f" (and {len(hits) - 1} more)" if len(hits) > 1 else ""
                tail = f" {rule.message}" if rule.message else ""
                return (f"the new text for {path} names {matched!r} on its line {line}{more}, and a rule keeps "
                        f"that out of this file, because every later session reads it.{tail}")
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = None
    if argv[:1] == ["--config"] and len(argv) > 1:
        path = argv[1]
    try:
        payload = json.load(sys.stdin)
        why = check(payload, load(config_path(path)))
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
