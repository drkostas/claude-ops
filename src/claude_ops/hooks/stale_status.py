"""Keep a shell script from reading `$?` after a command substitution in the same command (a PreToolUse hook).

`$?` is the exit status of the most recent command, and a command substitution is a command. In
`cmd; echo "$(date +%H:%M) cmd exited $?"` the substitution runs first, so `$?` is the status of
`date`, nearly always 0, and a script that logs or tests it reports success for a command that failed.
The same holds across the words of one simple command: bash expands them from left to right, so in
`cmd; echo $(date) exited $?`, `a=$(date) rc=$?` or a here-document whose command has a substitution,
`$?` is the substitution's status too. The fix is to save the status on its own statement before
anything else runs:

    cmd; rc=$?; echo "$(date +%H:%M) cmd exited $rc"

After a pipe, bash keeps the status of each command of the pipe in `PIPESTATUS`.

This hook reads what a tool call would write into a shell script and refuses it (exit 2, with the
reason sent back to the model) when the text reads `$?` later in the same simple command as a `$(...)`
or a backtick substitution. A command ends at `;`, `&`, `|`, a parenthesis or the end of a line (a
backslash before the newline continues it), and the body of an unquoted here-document belongs to the
command that opened it, since the shell expands it after that command's words:

- a Write of a file whose name ends in `.sh` or whose first line runs sh or bash (`#!/bin/sh`,
  `#!/bin/bash`, `#!/usr/bin/env bash`);
- an Edit or MultiEdit of such a file: the edit is applied to the file as it is, and only a line the
  file did not already have is refused;
- text a Bash command writes into such a file (a here-document, `echo` or `printf` redirected, or piped
  into `tee`), read by `commandline`, shared with the other hooks.

The command line given to the Bash tool is not read. Single quotes, a quoted here-document, a comment
and an escaped `\\$?` are text. `rc=$?` on its own statement, a plain `"$?"`, a `$?` before the
substitution, a `$?` inside a substitution of its own and a `$?` inside `$(( ... ))` with no substitution
before it go through. Text with no `$?` or `${?` in it (a Write's, or the file's after an Edit) is not
scanned at all. On anything unexpected the call goes through: a broken hook must never stop work.

    claude-ops hook stale-status      # reads the tool call on stdin
    python3 -P -m claude_ops.hooks.stale_status
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import commandline

#: a first line that runs sh or bash, directly or through env (with env's own options)
SHEBANG = re.compile(r"^#!\s*(?:\S*/)?(?:env\s+(?:-\S+\s+)*)?(?:sh|bash)(?:\s|$)")
#: the characters that end a word outside quotes
BREAKS = set(" \t\n;&|<>()")
#: the ones that also end the simple command (with the newline, handled on its own)
ENDS = set(";&|()")
#: what a status read is written as
READS = ("$?", "${?")


class Word:
    """The simple command being read: whether a command substitution in it has already run."""

    def __init__(self):
        self.ran = False


class Scanner:
    """Reads shell text and records where `$?` follows a command substitution in the same command.

    Each method takes the index to read from and returns the index after what it read. `hits` holds
    the line number of each such `$?`, counted from `base` (the line before the text's first)."""

    def __init__(self, text: str, base: int = 0):
        self.t = text
        self.n = len(text)
        self.base = base
        self.hits: list[int] = []

    def line(self, i: int) -> int:
        return self.base + self.t.count("\n", 0, i) + 1

    def hit(self, i: int) -> None:
        self.hits.append(self.line(i))

    # ---- statements and words
    def script(self, i: int, closer: str | None = None) -> int:
        """Read statements from i until `closer` (")" for `$(...)`) or the end. Returns the index after
        the closer."""
        word = Word()                                  # the simple command being read
        depth = 0                                      # subshell parentheses opened inside
        heredocs: list[tuple[str, bool, bool, Word]] = []   # (delimiter, quoted, strip tabs, its command)
        fresh = True                                   # nothing of the current word read yet
        while i < self.n:
            c = self.t[i]
            if c == "\n":
                i += 1
                for delim, quoted, strip, cmd in heredocs:
                    i = self.heredoc(i, delim, quoted, strip, cmd)
                heredocs = []
                word, fresh = Word(), True
                continue
            if c == "<" and self.t.startswith("<<", i) and not self.t.startswith("<<<", i):
                i, doc = self.heredoc_start(i + 2)
                if doc:
                    heredocs.append(doc + (word,))     # its body is expanded after its command's words
                fresh = True
                continue
            if c in BREAKS:
                if c == "(":
                    depth += 1
                elif c == ")":
                    if depth:
                        depth -= 1
                    elif closer == ")":
                        return i + 1
                if c in ENDS:
                    word = Word()
                fresh = True
                i += 1
                continue
            if c == "#" and fresh:
                end = self.t.find("\n", i)
                i = self.n if end < 0 else end
                continue
            fresh = False
            i = self.word_part(i, word, top=True)
        return i

    def word_part(self, i: int, word: Word, top: bool) -> int:
        """Read one piece of a word outside double quotes: an escape, a quoted string, an expansion or
        one plain character."""
        c = self.t[i]
        if c == "\\":
            return i + 2
        if c == "'" and top:
            end = self.t.find("'", i + 1)
            return self.n if end < 0 else end + 1
        if c == '"':
            return self.quoted(i + 1, word, '"')
        if c == "`":
            return self.backtick(i, word)
        if c == "$":
            return self.dollar(i, word, top)
        return i + 1

    def quoted(self, i: int, word: Word, closer: str | None) -> int:
        """Read the inside of a double-quoted string (closer '"') or of an unquoted here-document's body
        (closer None, to the end), sharing the word's state. Returns the index after the closer."""
        while i < self.n:
            c = self.t[i]
            if closer and c == closer:
                return i + 1
            if c == "\\":
                i += 2
            elif c == "`":
                i = self.backtick(i, word)
            elif c == "$":
                i = self.dollar(i, word, top=False)
            else:
                i += 1
        return i

    # ---- expansions
    def dollar(self, i: int, word: Word, top: bool) -> int:
        t = self.t
        nxt = t[i + 1] if i + 1 < self.n else ""
        if nxt == "?":
            if word.ran:
                self.hit(i)
            return i + 2
        if t.startswith("$((", i):
            return self.arithmetic(i + 3, word)
        if nxt == "(":
            i = self.script(i + 2, closer=")")
            word.ran = True
            return i
        if nxt == "{":
            if t.startswith("${?", i) and word.ran:
                self.hit(i)
            return self.brace(i + 2, word)
        if nxt == "'" and top:                         # $'...', with its own backslash escapes
            j = i + 2
            while j < self.n and t[j] != "'":
                j += 2 if t[j] == "\\" else 1
            return j + 1
        return i + 1

    def arithmetic(self, i: int, word: Word) -> int:
        """Read the inside of `$(( ... ))`, sharing the word's state. Returns the index after `))`."""
        depth = 0
        while i < self.n:
            c = self.t[i]
            if c == "(":
                depth += 1
                i += 1
            elif c == ")":
                if depth == 0 and self.t.startswith("))", i):
                    return i + 2
                depth = max(0, depth - 1)
                i += 1
            elif c in "\\`$\"'":
                i = self.word_part(i, word, top=True)
            else:
                i += 1
        return i

    def brace(self, i: int, word: Word) -> int:
        """Read the inside of `${...}`, sharing the word's state. Returns the index after `}`."""
        while i < self.n:
            c = self.t[i]
            if c == "}":
                return i + 1
            i = self.word_part(i, word, top=True) if c in "\\`$\"'" else i + 1
        return i

    def backtick(self, i: int, word: Word) -> int:
        """Read a backtick substitution starting at i, scan its inside as a script of its own, and mark
        the word. Returns the index after the closing backtick."""
        j = i + 1
        while j < self.n and self.t[j] != "`":
            j += 2 if self.t[j] == "\\" else 1
        inner = re.sub(r"\\([`$\\])", r"\1", self.t[i + 1:j])
        sub = Scanner(inner, self.line(i) - 1)          # removing escapes keeps every newline
        sub.script(0)
        self.hits.extend(sub.hits)
        word.ran = True
        return j + 1

    # ---- here-documents
    def heredoc_start(self, i: int) -> tuple[int, tuple[str, bool, bool] | None]:
        """Read the delimiter after `<<` (or `<<-`). Returns the index after it and (delimiter, quoted,
        strip tabs), or None when there is none."""
        strip = self.t.startswith("-", i)
        i += 1 if strip else 0
        while i < self.n and self.t[i] in " \t":
            i += 1
        start = i
        while i < self.n and self.t[i] not in BREAKS:
            i += 1
        raw = self.t[start:i]
        if not raw:
            return i, None
        quoted = any(q in raw for q in "'\"\\")
        return i, (re.sub(r"['\"\\]", "", raw), quoted, strip)

    def heredoc(self, i: int, delim: str, quoted: bool, strip: bool, cmd: Word | None = None) -> int:
        """Read a here-document's body from i to its delimiter line. An unquoted body is one string, read
        as part of the command that opened it (`cmd`). Returns the index after the delimiter line, or i itself when no line ends
        it (`<<` was a shift in `(( x << 2 ))`, or the text is a piece of a script), so the text after it
        is read as statements."""
        start = i
        while i < self.n:
            end = self.t.find("\n", i)
            end = self.n if end < 0 else end
            line = self.t[i:end]
            if (line.lstrip("\t") if strip else line) == delim:
                if not quoted:
                    self.body(start, i, cmd or Word())
                return min(end + 1, self.n)
            i = end + 1
        return start

    def body(self, start: int, end: int, cmd: Word) -> None:
        sub = Scanner(self.t[start:end], self.line(start) - 1)
        sub.quoted(0, cmd, None)
        self.hits.extend(sub.hits)


def stale_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) for each line of shell text that reads `$?` after a command substitution in
    the same command, each line once."""
    if not any(r in text for r in READS):            # no status read, nothing to find
        return []
    sc = Scanner(text)
    sc.script(0)
    lines = text.splitlines()
    return [(n, lines[n - 1] if n - 1 < len(lines) else "") for n in sorted(set(sc.hits))]


def first_line(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.readline()
    except OSError:
        return ""


def is_shell(path: str, text: str = "") -> bool:
    """Whether a file is a shell script: its name ends in .sh, or its first line (the new text's when it
    starts with one, else the file's own) runs sh or bash."""
    if path.endswith(".sh"):
        return True
    head = text.split("\n", 1)[0] if text.startswith("#!") else first_line(path)
    return bool(SHEBANG.match(head))


def read(path: str) -> str | None:
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def edited(path: str, edits: list[dict]) -> tuple[str, str] | None:
    """(the file as it is, the file after these edits), or None when the file cannot be read or an
    edit's old text is not in it."""
    old = read(path)
    if old is None:
        return None
    new = old
    for e in edits:
        a, b = str(e.get("old_string", "")), str(e.get("new_string", ""))
        if not a or a not in new:
            return None
        new = new.replace(a, b) if e.get("replace_all") else new.replace(a, b, 1)
    return old, new


def new_hits(old: str, new: str) -> list[tuple[int, str]]:
    """The lines of `new` that read a stale `$?` and are not such a line in `old` already."""
    if not any(r in new for r in READS):             # the edited file reads no status: skip both scans
        return []
    before: dict[str, int] = {}
    for _, line in stale_lines(old):
        before[line] = before.get(line, 0) + 1
    out = []
    for n, line in stale_lines(new):
        if before.get(line):
            before[line] -= 1
        else:
            out.append((n, line))
    return out


def findings(payload: dict) -> list[tuple[str, int, str]]:
    """(file, line number, line) for each stale `$?` this tool call would write into a shell script."""
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    out: list[tuple[str, int, str]] = []
    if tool == "Bash":
        for path, text in commandline.writes(str(ti.get("command", "")), cwd):
            if is_shell(path, text):
                out += [(path, n, line) for n, line in stale_lines(text)]
        return out
    path = str(ti.get("file_path") or "")
    if not path or tool not in ("Write", "Edit", "MultiEdit"):
        return out
    path = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    if tool == "Write":
        text = str(ti.get("content", ""))
        return [(path, n, line) for n, line in stale_lines(text)] if is_shell(path, text) else []
    edits = [ti] if tool == "Edit" else [e for e in ti.get("edits") or [] if isinstance(e, dict)]
    if not is_shell(path):
        return out
    both = edited(path, edits)
    if both:
        return [(path, n, line) for n, line in new_hits(*both)]
    for e in edits:                                    # the file cannot be read: judge the new text alone
        out += [(path, n, line) for n, line in stale_lines(str(e.get("new_string", "")))]
    return out


def check(payload: dict) -> str | None:
    """Why this tool call is refused, or None."""
    hits = findings(payload)
    if not hits:
        return None
    path, n, line = hits[0]
    shown = line.strip()
    shown = shown if len(shown) <= 100 else shown[:97] + "..."
    more = f" (and {len(hits) - 1} more)" if len(hits) > 1 else ""
    return (f"line {n} of {path} reads $? after a command substitution in the same command{more}: {shown}. "
            "The substitution runs first, so $? is its status (0 for $(date)), not the status of the command "
            "before it. Save the status on its own statement first, cmd; rc=$? and then use $rc (after a "
            "pipe, rc=${PIPESTATUS[0]}). If you meant the status of the substitution itself, save it the "
            "same way, out=$(cmd); rc=$?")


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
        why = check(payload)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        print(f"Blocked: {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
