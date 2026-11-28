"""Refuse a time of day written a little ahead of the clock (a PreToolUse hook).

A model that reports what it saw often writes the time of a reading from memory ("the queue was empty at
13:22") instead of from a clock it read. The guess is nearly always a few minutes late in the day, because
the work since the reading feels longer than it was, and the reader of a report, a ticket or a commit
compares that time with the log and goes to the wrong minute. This hook refuses text that holds a time of
day between `min_minutes` (1) and `max_minutes` (15) ahead of the local clock (exit 2, with one line sent
back to the model that names the time and the clock).

Only a bare time of day (`13:22`, `13:22:05`) is judged. A dotted one (`13.22`, `13.22.05`) is judged
only where a time is expected, since the same digits are as often a decimal or a version: right after
"at" or "update" (`State at 13.22`, `UPDATE 13.22`), right after a date (`9 Mar 13.22`), or as the first
word of a line, a heading or a list item (`- 13.22 shipped`, `## 13.22`). A dotted number with a unit
after it (`13.25 h`, `13.25 GB`, `13.25%`) or a third part (`13.22.1`) is never a time. These are left
alone:

- a time with a zone or an offset after it (`08:46Z`, `05:25 UTC`, `10:00 -03`, `10:00+02:00`, and the
  words in `zones`);
- a time with a date before it that is not today (`15 Nov 10:00`, `2026-11-15T10:00`);
- text in code (fenced, indented or inline) and in quotes, which is usually a log line or a name;
- a time with one of `forward_words` before it in the same sentence (`after 17:49`, `until 15:00`,
  `between 14:00 and 15:30`), since a time still to come is written ahead on purpose;
- a time right after "not" (`written at 10:06, not 10:10`), since a correction names the wrong time;
- what an `allow` pattern matches.

Ahead of the clock means later today. Over a month of a team's notes, a time written up to 15 minutes
ahead was a slip about nine times in ten, and one written 15 to 30 minutes ahead was a plan more often
than not (a check set for later, an estimate), so the window stops at 15 minutes. It also keeps a time
written just after midnight about the evening before (23:58 written at 00:05) from reading as ahead.

What it reads, from a JSON file:

    {
      "tools": {"mcp__notes__add": ["text", "subject"]},
      "commands": [{"program": "notes.sh", "flags": ["--text", "--subject"]}],
      "messages": true,
      "files": ["~/notes/*.md"],
      "zones": ["UTC", "GMT", "Z"],
      "forward_words": ["after", "until", "by", "between"],
      "allow": ["\\\\bthe \\\\d{1,2}:\\\\d{2} run\\\\b"],
      "min_minutes": 1,
      "max_minutes": 15
    }

`tools` maps a tool's name to the arguments of it that hold text a person reads (never one that holds
when to do something). An argument that is a list or an object is read by every string inside it.
`commands` names programs run in Bash and the flags whose next word is such text (`--text value` or
`--text=value`). With `messages`, a git commit message and the text of a gh command that writes (a
pull request's or an issue's title and body, a comment, a `gh api` body) are read too, the way the
public-voice hook reads them. `files` holds glob patterns (`~` is expanded, and `*` also matches `/`)
of files whose written text is read: what a Write, an Edit or a MultiEdit puts in such a file, and
what a Bash line writes into one (a here-document given to `cat` or `tee`, `echo` and `printf`
sent to it with `>` or `>>`, and the new text of each substitution a `sed -i` or a `perl -i -p`
makes in it). Only the lines the file does not hold yet are judged, so rewriting a
note does not judge the times already in it. Without `forward_words`, `zones` or the window, the
defaults here are used.

The file is `--config`, else `CLAUDE_OPS_CLOCK_AHEAD_CONFIG`, else `~/.config/claude-ops/clock-ahead.json`.
With no file nothing is read and nothing is refused. On anything unexpected the call goes through: a
broken hook must never stop work.

    claude-ops hook clock-ahead          # reads the tool call on stdin
    python3 -P -m claude_ops.hooks.clock_ahead [--config FILE]
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
import re
import sys

from . import commandline

DEFAULT_CONFIG = "~/.config/claude-ops/clock-ahead.json"
CONFIG_ENV = "CLAUDE_OPS_CLOCK_AHEAD_CONFIG"
DEFAULT_ZONES = ("UTC", "GMT", "Z")
DEFAULT_FORWARD = ("after", "until", "till", "by", "from", "due", "next", "at the earliest", "between",
                   "waits for", "not before", "no earlier than", "tomorrow", "tonight", "if", "will",
                   "set for", "scheduled", "planned", "expected", "estimate", "likely", "roughly", "around",
                   "wakes")
MIN_MINUTES, MAX_MINUTES = 1.0, 15.0
#: how far back from a time the same sentence is searched for a date, in characters
LOOKBACK = 80
#: how many words right before a time are searched for a forward word (a longer phrase widens it, so a phrase
#: such as "at the earliest" is counted whole)
FORWARD_WORDS_BACK = 3
#: words that mark the whole sentence as a condition, a future or another day, wherever they sit before the
#: time. Every other forward word (by, from, after) names who or what as often as when, so it counts only
#: among the last FORWARD_WORDS_BACK words
#: "by" is a deadline word only when the time follows it, as in "by 14:00" or "by about 14:00". In "by hand at
#: 14:00" or "by accident at 14:00" the time belongs to "at", so such a "by" is taken out before the forward
#: words are looked for. The sentence given to it ends where the time starts, so the end of the text is the time.
_BY_NOT_BEFORE_TIME = re.compile(r"\bby\b(?!\s*(?:(?:about|around|roughly|approximately)\s*)?$)", re.I)
SENTENCE_WIDE = ("if", "will", "tomorrow", "tonight")

#: a time of day. A colon may end it when a space or the end of the text follows, as in a log line that starts
#: "13:22: queued". A colon with anything else after it ("13:22:99", "1:23:4x", "fe80::1:23:45") means the
#: text is not a time.
TIME = re.compile(r"(?:(?<![\w:.\-/])|(?<=\dT))([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d(?:\.\d+)?)?(?!\w|:\S)")
#: a zone after a time, past any fractional seconds (`13:22:00.000Z`) and an opening bracket (`13:22 (+02)`)
_OFFSET = re.compile(r"(?:\.\d+)?\s?\(?(?:[+-](?:[01]\d|2[0-3])(?::?[0-5]\d)?\b|Z\b)")
_FENCED = re.compile(r"^\s{0,3}(```|~~~).*?^\s{0,3}\1[^\n]*$", re.S | re.M)
_INDENTED = re.compile(r"^(?: {4}|\t).*$", re.M)
_INLINE = re.compile(r"`[^`\n]*`")
_QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”|(?<![\w'])'[^'\n]*'(?![\w'])")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T|\s+)?(?:at\s+)?$")
_DAY_MONTH = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?(?:\s+(\d{4}))?,?\s+(?:at\s+)?$")
_MONTH_DAY = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?(?:\s+(\d{4}))?,?\s+(?:at\s+)?$")
_SENTENCE_END = re.compile(r"[.!?;]\s|\n")
#: a dotted time of day (`13.22`, `13.22.05`). It is judged only where `dotted_expected` says a time is
#: expected, since `1.04` and `3.14` are as often a decimal or a version
DOTTED = re.compile(r"(?<![\w.\-/])([01]?\d|2[0-3])\.([0-5]\d)(?:\.[0-5]\d)?(?!\w|[.,]\d)")
#: the words right before a dotted time that say a time follows
_DOTTED_WORD = re.compile(r"\b(?:at|update)\s+$", re.I)
#: the start of a line, a heading or a list item, with an opening bold or italic mark
_LINE_START = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+[.)][ \t]+)?(?:\*\*|__|\*|_)?$")
#: a unit after a dotted number, which makes it a measure and not a time
_UNIT = re.compile(r"\s?(?:%|(?:h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?|ms|x|[kmgt]i?b|points?|pts|"
                   r"days?|weeks?)\b)", re.I)
#: a correction names the wrong time on purpose ("written at 10:06, not 10:10")
_NOT = re.compile(r"\bnot\s+(?:at\s+)?$", re.I)


class Config:
    def __init__(self, data: dict | None = None):
        d = data if isinstance(data, dict) else {}
        self.tools = {str(k): [str(a) for a in (v or [])] for k, v in (d.get("tools") or {}).items()
                      if isinstance(v, list)}
        self.commands = [(str(c["program"]), [str(f) for f in c.get("flags") or []])
                         for c in d.get("commands") or [] if isinstance(c, dict) and c.get("program")]
        self.messages = bool(d.get("messages"))
        self.files = [os.path.abspath(os.path.expanduser(str(f))) for f in d.get("files") or []]
        self.zones = [str(z) for z in (d.get("zones") or DEFAULT_ZONES)]
        # "at" alone before a time says when something happened as often as when it will, so it is never forward
        self.forward = [w for w in (str(w).lower() for w in (d.get("forward_words") or DEFAULT_FORWARD))
                        if w.strip() != "at"]
        self.allow = []
        for a in d.get("allow") or []:
            try:
                self.allow.append(re.compile(str(a)))
            except re.error:
                continue
        self.min_minutes = float(d.get("min_minutes", MIN_MINUTES))
        self.max_minutes = float(d.get("max_minutes", MAX_MINUTES))
        words = "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in self.forward)
        self.forward_re = re.compile(rf"\b(?:{words})\b", re.I) if words else None
        wide = [str(w).lower() for w in (d["sentence_words"] if "sentence_words" in d else SENTENCE_WIDE)]
        wide_words = "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in wide if w.strip() and w.strip() != "at")
        self.wide_re = re.compile(rf"\b(?:{wide_words})\b", re.I) if wide_words else None
        self.forward_back = max([FORWARD_WORDS_BACK] + [len(w.split()) for w in self.forward])
        zones = "|".join(re.escape(z) for z in self.zones)
        self.zone_re = re.compile(rf"\s?\(?(?:{zones})\b") if zones else None

    @property
    def empty(self) -> bool:
        return not (self.tools or self.commands or self.messages or self.files)


def config_path(argv_path: str | None = None) -> str:
    return os.path.expanduser(argv_path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)


def load(path: str) -> Config:
    """The configuration in this file. A missing or unreadable file reads nothing."""
    try:
        with open(path) as f:
            return Config(json.load(f))
    except Exception:  # noqa: BLE001 - no configuration, nothing read
        return Config()


def _blank(m: re.Match) -> str:
    """The match as spaces, so the positions of what is left stay where they were."""
    return re.sub(r"[^\n]", " ", m.group(0))


def prose(text: str, cfg: Config) -> str:
    """The text with code, quotes and `allow` matches blanked out."""
    for rx in (_FENCED, _INDENTED, _INLINE, _QUOTED, *cfg.allow):
        text = rx.sub(_blank, text)
    return text


def _month(word: str) -> int | None:
    w = word[:3].lower()
    return _MONTHS.index(w) + 1 if w in _MONTHS else None


def dated(before: str, today: dt.date) -> bool | None:
    """Whether the words just before a time give it a date: True when that date is today, False when it
    is another day, None when there is no date."""
    m = _ISO_DATE.search(before)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) == today
        except ValueError:
            return False
    for rx, day_i, month_i in ((_DAY_MONTH, 1, 2), (_MONTH_DAY, 2, 1)):
        m = rx.search(before)
        if m and _month(m.group(month_i)):
            try:
                year = int(m.group(3)) if m.group(3) else today.year
                return dt.date(year, _month(m.group(month_i)), int(m.group(day_i))) == today
            except ValueError:
                return False
    return None


def dotted_expected(text: str, m: re.Match, today: dt.date) -> bool:
    """Whether the dotted number `m` in `text` stands where a time is expected: right after "at" or
    "update", right after a date, or as the first word of a line, a heading or a list item, and with
    no unit after it."""
    if _UNIT.match(text, m.end()):
        return False
    line = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
    if _DOTTED_WORD.search(line) or _LINE_START.match(line):
        return True
    return dated(text[max(0, m.start() - LOOKBACK):m.start()], today) is not None


def times(text: str, today: dt.date) -> list[re.Match]:
    """Each time of day in `text` in order: every colon time, and a dotted one where a time is expected."""
    found = list(TIME.finditer(text))
    found += [m for m in DOTTED.finditer(text) if dotted_expected(text, m, today)]
    return sorted(found, key=lambda m: m.start())


def ahead(text: str, now: dt.datetime, cfg: Config) -> list[tuple[str, float]]:
    """(the time as written, minutes ahead of `now`) for each bare time of day in `text` that is more
    than `min_minutes` and at most `max_minutes` ahead of the clock, with what is left alone (see the
    module's words) taken out first."""
    clean, out = prose(text, cfg), []
    for m in times(clean, now.date()):
        after = clean[m.end():m.end() + 12]
        if _OFFSET.match(after) or (cfg.zone_re and cfg.zone_re.match(after)):
            continue
        start = max(0, m.start() - LOOKBACK)
        before = clean[start:m.start()]
        ends = list(_SENTENCE_END.finditer(before))
        sentence = before[ends[-1].end():] if ends else before
        # "by" followed by anything but the time says how or by whom something was done, so it is taken out
        # before the forward words are looked for ("I ran it by hand at 13:25" is a past time)
        sentence = _BY_NOT_BEFORE_TIME.sub(" ", sentence)
        near = " ".join(sentence.split()[-cfg.forward_back:])
        if cfg.forward_re and cfg.forward_re.search(near):
            continue
        if cfg.wide_re and cfg.wide_re.search(sentence):
            continue
        if _NOT.search(before):
            continue
        on = dated(before, now.date())
        if on is False:
            continue
        written = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        minutes = (written - now).total_seconds() / 60
        if cfg.min_minutes < minutes <= cfg.max_minutes:
            out.append((m.group(0), minutes))
    return out


def _command_texts(command: str, cwd: str, cfg: Config) -> list[str]:
    """The text each configured program's flags carry in a Bash command."""
    texts = []
    for cmd, _here in commandline.steps(command, cwd):
        words = cmd.get("words") or []
        for i, w in enumerate(words):
            for program, flags in cfg.commands:
                if os.path.basename(w) != program:
                    continue
                rest = words[i + 1:]
                for j, a in enumerate(rest):
                    if a in flags and j + 1 < len(rest):
                        texts.append(rest[j + 1])
                    elif "=" in a and a.split("=", 1)[0] in flags:
                        texts.append(a.split("=", 1)[1])
    return texts


def watched(path: str, cfg: Config) -> bool:
    """Whether text written into this file is read (it matches one of `files`)."""
    full = os.path.abspath(os.path.expanduser(path))
    return any(fnmatch.fnmatchcase(full, pattern) for pattern in cfg.files)


def new_lines(path: str, text: str) -> str:
    """The lines of `text` that the file does not hold yet: what a write adds to it."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as f:
            held = set(f.read().splitlines())
    except OSError:
        held = set()
    return "\n".join(line for line in text.splitlines() if line not in held)


def texts(payload: dict, cfg: Config) -> list[str]:
    """The texts of this tool call that the configuration says a person reads."""
    return [text for text, _source in sourced_texts(payload, cfg)]


def sourced_texts(payload: dict, cfg: Config) -> list[tuple[str, str | None]]:
    """`texts` with each text's file: the path when the text is what a file the command names holds
    now (a body, notes, message or input file), else None."""
    tool = str(payload.get("tool_name", ""))
    ti = payload.get("tool_input") or {}
    if tool == "Bash":
        command, cwd = str(ti.get("command", "")), payload.get("cwd") or os.getcwd()
        found = [(t, None) for t in _command_texts(command, cwd, cfg)] if cfg.commands else []
        if cfg.messages:
            from .public_voice import published_from
            found += [(t, source) for _where, _target, t, source in published_from(command, cwd)]
        if cfg.files:
            found += [(new_lines(path, t), None) for path, t in commandline.writes(command, cwd)
                      if watched(path, cfg)]
        return found
    if tool in ("Write", "Edit", "MultiEdit"):
        path = str(ti.get("file_path", ""))
        if not cfg.files or not watched(path, cfg):
            return []
        if tool == "Write":
            written = [ti.get("content")]
        elif tool == "Edit":
            written = [ti.get("new_string")]
        else:
            written = [e.get("new_string") for e in ti.get("edits") or [] if isinstance(e, dict)]
        return [(new_lines(path, t), None) for t in written if isinstance(t, str)]
    from .public_voice import strings
    return [(v, None) for k in cfg.tools.get(tool, []) if k in ti for v in strings(ti.get(k))]


def check(payload: dict, cfg: Config, now: dt.datetime | None = None) -> str | None:
    """Why this tool call is refused, or None. A time read from a file the command names says so,
    because the check reads that file before the command runs."""
    if cfg.empty:
        return None
    now = now or dt.datetime.now()
    for text, source in sourced_texts(payload, cfg):
        hits = ahead(text, now, cfg)
        if hits:
            written, minutes = hits[0]
            more = f" (and {len(hits) - 1} more)" if len(hits) > 1 else ""
            why = (f"{written} is {minutes:.0f} minutes ahead of the clock, which reads {now:%H:%M}{more}. "
                   f"Read the clock and write the time it gives, or put a word such as after or until "
                   f"before a time still to come.")
            if source:
                command = str((payload.get("tool_input") or {}).get("command", ""))
                why += " " + commandline.read_note(source, command, payload.get("cwd") or os.getcwd()) + "."
            return why
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
