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
- text a Bash command writes into such a file (a here-document, `echo` or `printf` redirected, piped
  into `tee`, or the replacement of a `sed -i` or `perl -i -p` substitution), read by `commandline`,
  shared with the other hooks.

The command line given to the Bash tool is not read for this, with one exception below. Single quotes, a
quoted here-document, a comment
and an escaped `\\$?` are text. `rc=$?` on its own statement, a plain `"$?"`, a `$?` before the
substitution, a `$?` inside a substitution of its own and a `$?` inside `$(( ... ))` with no substitution
before it go through. Text with no `$?` or `${?` in it (a Write's, or the file's after an Edit) is not
scanned at all. On anything unexpected the call goes through: a broken hook must never stop work.

A command run in the background (a Bash call with `run_in_background`) is reported by its exit status,
which is the status of the last command it ran. So a watch on a check (`gh pr checks <n> --watch`, or
`gh run watch <id> --exit-status`) followed by `echo "rc=$?" >> log`, or piped into `tail`, is reported
as a success when the check failed. Such a call is refused when the gate's status does not reach the
command's exit: another command runs after it, its output is piped (without pipefail), or `||` runs a
command other than a failing exit when it fails. These go through: the watch as the only or last
command, joined to the next with `&&`, tested in an if, elif, while or until condition or after `!`,
with `set -e` before it, piped after `set -o pipefail`, followed by `|| exit 1`, and a command whose
first exit after the watch is `exit "$rc"` of a variable saved with `rc=$?` straight after the watch, or
an `exit $?` straight after it. A watch in the body of a for, while or until loop is refused unless
`|| exit 1` after it or `set -e` before it ends the loop at the first failure, since a loop gives only
its last round's status. A command in the foreground is not read, since its output and status are read
when it ends.

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


# --- a background command that hides a gate's exit ------------------------------------------------

#: commands that wait for something and exit non-zero when it failed: the words they start with and the
#: flag that makes them wait and report (`gh pr checks` alone answers at once, and `gh run watch` without
#: --exit-status exits 0 whatever the run did)
GATES = ((("gh", "pr", "checks"), "--watch"), (("gh", "run", "watch"), "--exit-status"))
#: keywords that open a condition, whose status the shell tests instead of reporting it
CONDITIONS = {"if", "elif", "while", "until"}
#: keywords that end a condition
BODIES = {"then", "do"}
#: words that are shell syntax, not a command that runs
SYNTAX = CONDITIONS | BODIES | {"!", "done", "fi", "else", "esac", "{", "}"}
#: a word that is one variable: what `exit "$rc"` or `exit ${rc}` hands over once quotes are removed
ONE_VARIABLE = re.compile(r"^\$(?:\?|[A-Za-z_][A-Za-z0-9_]*|\{[?A-Za-z_][^}]*\})$")
#: the name of the variable such a word reads (`?` for `$?`)
VARIABLE_NAME = re.compile(r"^\$\{?(\?|[A-Za-z_][A-Za-z0-9_]*)")
#: an assignment of the status to a variable, `rc=$?` (quotes are gone once the line is split)
SAVES_STATUS = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=\$(?:\?|\{\?\})$")
#: words that may stand before such an assignment
DECLARE = {"local", "export", "declare", "typeset", "readonly"}


def leading(words: list[str]) -> tuple[list[str], list[str]]:
    """(the keywords in front of a command, the rest)."""
    i = 0
    while i < len(words) and words[i] in SYNTAX:
        i += 1
    return words[:i], words[i:]


def gate_name(words: list[str]) -> str:
    """The gate a command runs (`gh pr checks`), or "" when it runs none."""
    w = commandline.unwrap(words)
    for start, flag in GATES:
        if tuple(w[:len(start)]) == start and any(x == flag or x.startswith(flag + "=") for x in w):
            return " ".join(start)
    return ""


def shell_options(words: list[str]) -> tuple[bool, bool]:
    """(whether this command sets errexit, whether it sets pipefail): `set -e`, `set -euo pipefail`."""
    if words[:1] != ["set"]:
        return False, False
    short = any(w.startswith("-") and not w.startswith("--") and "e" in w[1:] for w in words[1:])
    return short or "errexit" in words, "pipefail" in words


def exits_with(words: list[str]) -> str | None:
    """The argument of an `exit` or `return` command ("" when it has none), or None for any other."""
    _, w = leading(words)
    if w[:1] not in (["exit"], ["return"]):
        return None
    return w[1] if len(w) > 1 else ""


def saved_status(words: list[str]) -> str | None:
    """The variable a command saves `$?` into (`rc=$?`, `local rc=$?`), or None."""
    _, w = leading(words)
    while w and (w[0] in DECLARE or (w[0].startswith("-") and len(w) > 1)):
        w = w[1:]
    m = SAVES_STATUS.match(w[0]) if len(w) == 1 else None
    return m.group(1) if m else None


def shown(words: list[str], limit: int = 40) -> str:
    text = " ".join(leading(words)[1])
    return text if len(text) <= limit else text[:limit - 3] + "..."


def after_list(steps: list[dict], j: int) -> list[dict]:
    """The commands that run after the and-or list ending at step j, leaving out shell syntax alone and
    the other branches of an if the list sits in (an else runs instead of it, not after it)."""
    out, skip = [], 0
    for st in steps[j + 1:]:
        keys, words = leading(st["words"])
        if skip:
            skip += keys.count("if")
            skip -= keys.count("fi")
            continue
        if "else" in keys or "elif" in keys:
            skip = 1
            continue
        if words:
            out.append(st)
    return out


def hides(steps: list[dict], g: int, errexit: bool, pipefail: bool, in_loop: bool = False) -> str | None:
    """How the command hides the status of the gate at step g, or None when its exit still reports it.
    `in_loop`: the gate is in the body of a for, while or until loop, whose status is its last round's."""
    name = gate_name(steps[g]["words"])
    j = g
    while steps[j]["after"] == "|":
        if not pipefail:
            return (f"pipes {name} into `{shown(steps[j + 1]['words'])}`, and a pipe's status is its last "
                    "command's")
        j += 1
    while steps[j]["after"] in ("&&", "||"):
        op, j = steps[j]["after"], j + 1
        if op == "||":
            arg = exits_with(steps[j]["words"])
            if arg is not None and arg not in ("", "0"):
                return None                            # `|| exit 1` ends the command as a failure
            return f"runs `|| {shown(steps[j]['words'])}` when {name} fails, which then gives the status"
        while steps[j]["after"] == "|":                 # `gate && a | b` runs only after a pass
            j += 1
    stops = errexit and steps[g]["after"] not in ("&&", "||")   # set -e ends the command when the gate fails
    if in_loop and not stops:
        return f"runs {name} in a loop, which gives only its last round's status"
    rest = after_list(steps, j)
    if not rest or stops:
        return None                                    # the gate's list is the last thing the command runs
    saved = saved_status(rest[0]["words"])             # `rc=$?` straight after the gate
    for k, st in enumerate(rest):
        arg = exits_with(st["words"])
        if arg is None:
            continue
        var = VARIABLE_NAME.match(arg) if ONE_VARIABLE.match(arg) else None
        if var and (var.group(1) == "?" and k == 0 or var.group(1) == saved):
            return None                                # `exit $?` straight after the gate, or exit "$rc" of rc=$?
        break
    return f"then runs `{shown(rest[-1]['words'])}`, whose status the command ends with"


def hidden_gate(command: str) -> str | None:
    """Why a command run in the background would report a gate's failure as its last command's status,
    or None. A gate in a condition (if, elif, while, until) or after `!` is tested by the command itself,
    and passes."""
    steps = commandline.parse(command)
    condition = errexit = pipefail = False
    loops = 0                                          # loop bodies the step is in (do ... done)
    for g, st in enumerate(steps):
        keys, words = leading(st["words"])
        if any(k in CONDITIONS for k in keys):
            condition = True
        if any(k in BODIES for k in keys):
            condition = False
        loops = max(0, loops + keys.count("do") - keys.count("done"))
        e, p = shell_options(words)
        errexit, pipefail = errexit or e, pipefail or p
        if condition or "!" in keys or not gate_name(words):
            continue
        how = hides(steps, g, errexit, pipefail, in_loop=loops > 0)
        if how:
            return how
    return None


def background_refusal(payload: dict) -> str | None:
    """Why a Bash call run in the background is refused for a gate whose exit it hides, or None."""
    if payload.get("tool_name") != "Bash":
        return None
    ti = payload.get("tool_input") or {}
    if ti.get("run_in_background") not in (True, "true", "True", 1):
        return None
    command = str(ti.get("command", ""))
    how = hidden_gate(command)
    if not how:
        return None
    name = next((gate_name(s["words"]) for s in commandline.parse(command) if gate_name(s["words"])), "the gate")
    if how.startswith("pipes"):
        fix = "put set -o pipefail; before it, or run the watch as the only command"
    elif " in a loop" in how:
        fix = f"end the loop at the first failure, {name} ... || exit 1, or put set -e; before the loop"
    else:
        fix = (f"run the watch as the only command, or save its status straight after it and end with it, "
               f"{name} ...; rc=$?; echo \"rc=$rc\" >> log; exit $rc")
    return (f"this background command {how}. A background command reports the exit status of its last "
            f"command, so a failed {name} would be reported as a success. To keep its status, {fix}")


def check(payload: dict) -> str | None:
    """Why this tool call is refused, or None."""
    hits = findings(payload)
    if not hits:
        return background_refusal(payload)
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
