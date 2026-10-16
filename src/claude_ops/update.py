"""Bring an open Claude Code chat up to date: is it safe to touch, and how to restart it in place.

An open chat keeps the Claude Code version, settings and instructions it started with. Bringing it
up to date means typing into it or restarting it, and both are only safe when it is idle and
nobody is typing into it. These are the checks and the mechanics, learned on a machine with a
dozen chats open at once.

- Never while the chat is working. A restart in the middle of a turn throws the turn away, and a
  background command the chat started dies with its process. `idle()` decides from what a chat
  already produces: the transcript, the process's children (a command or background task is a
  shell under `claude`), and, when the caller has one, the time its last turn ended.
- Never over a half-written message. A chat at its prompt reads as idle while a person types into
  it, and a paste would join their draft and send both. `may_be_typing` and `empty_prompt` check.
- Never /exit. A pasted /exit can open Claude Code's own exit dialog and leave a choice on screen
  that belongs to the person. `restart_iterm`, `restart_tmux` and `restart_into_tmux` send SIGTERM;
  the conversation is on disk already.
- Never a new window. A tmux chat restarts inside its own pane, and a chat moves from an iTerm2 tab
  into tmux inside that same tab.
- The permission mode does not survive a resume. It is read from the footer before the restart
  and passed back with --permission-mode.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from . import chat as _chat
from . import inject as _inject

SHELLS = {"zsh", "bash", "sh", "fish"}
#: after its last turn ended, a chat must have been quiet this long before it is touched
QUIET_S = 30
#: a chat with no recorded end of turn must be quiet this long
QUIET_UNHOOKED_S = 600
#: someone may be typing when the terminal is in front and they touched the Mac this recently
TYPING_WINDOW_S = 120
#: a background agent whose file was written this recently is still working
AGENT_QUIET_S = 120
#: a workflow with no final state whose journal was written this recently is still running
WORKFLOW_QUIET_S = 1800
PROJECTS = Path.home() / ".claude" / "projects"


def shell_children(pid: int, procs: dict[int, dict]) -> list[int]:
    """The shells a claude process has started: a command or a background task it is running."""
    return [p for p, v in procs.items()
            if v["ppid"] == pid and Path(v["comm"]).name.lstrip("-") in SHELLS]


#: what may run after a wait loop for the shell to be only a wait: commands that read and print
READ_ONLY = frozenset({"tail", "cat", "head", "grep", "echo", "printf", "ls", "wc", "date", "sort",
                       "uniq", "cut", "sed", "awk", "jq"})
#: `gh` subcommands that only read: a report after a wait often asks GitHub for a pull request's state
GH_READS = frozenset({("pr", "view"), ("pr", "checks"), ("pr", "list"), ("pr", "status"),
                      ("run", "view"), ("run", "list"), ("issue", "view"), ("issue", "list")})
#: shell words that open or close a loop or a branch; what follows them on the line is a command
_KEYWORDS = frozenset({"if", "do", "done", "then", "else", "fi"})
_EVAL = re.compile(r"\beval '(?P<cmd>.*)' < /dev/null", re.S)
_LOOP = re.compile(r"^\s*(?:until|while)\s+.+?;\s*do\s+sleep\s+[\d.]+\s*;\s*done\b(?P<after>.*)$", re.S)
_PROXY = re.compile(r"^rtk\s+proxy\s+(?:bash|sh|zsh)\s+-c\s+(?P<inner>.*)$", re.S)


def is_waiter(command: str) -> bool:
    """Pure. Whether a background shell only waits: a loop whose body is `sleep N` (``until <test>;
    do sleep 60; done``), followed by nothing or only commands that read and print (READ_ONLY,
    also inside ``rtk proxy bash -c '...'``), with no redirection that writes. Claude Code wraps
    each command as ``eval '<command>' < /dev/null`` in a shell; the wrapper is looked through.

    Such a shell loses nothing when its chat restarts: the next instance is told which background
    commands did not finish and starts again the ones it still needs. A wait followed by a command
    that acts (``... done && deploy``) is not a waiter, and neither is any other command."""
    m = _EVAL.search(command or "")
    inner = (m.group("cmd") if m else (command or "")).replace("'\"'\"'", "'")
    loop = _LOOP.match(inner)
    if not loop:
        return False
    return _only_reads(loop.group("after"))


def _only_reads(text: str) -> bool:
    """Every command in `text` (split by shell rules, so a quoted `|` stays inside its argument) is
    one that reads and prints, `rtk proxy bash -c '<commands>'` is looked into, and nothing writes
    a file with `>`. A `for NAME in ...` loop, its `do`/`done`, and `if`'s `then`/`else`/`fi` are
    shell words around commands, and the commands inside them are checked. Every `$( ... )` is
    checked too, wherever it stands (an `echo "$(rm x)"` is not only an echo), and a backtick is
    refused."""
    if "`" in text:
        return False
    for inner in _substitutions(text):
        if inner is None or not _only_reads(inner):
            return False
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|<>")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return False                    # unbalanced quotes: not a shape we can vouch for
    commands, cur = [], []
    for t in tokens:
        if t and set(t) <= set(";&|"):
            commands.append(cur)
            cur = []
        else:
            cur.append(t)
    commands.append(cur)
    for cmd in commands:
        while cmd and cmd[0] in _KEYWORDS:
            cmd = cmd[1:]
        if not cmd:
            continue
        if cmd[0] == "for":
            if len(cmd) < 3 or cmd[2] != "in" or not re.fullmatch(r"[A-Za-z_]\w*", cmd[1]):
                return False            # not the plain `for NAME in WORDS` shape
            continue
        for i, t in enumerate(cmd):
            if t and set(t) <= set("<>&") and ">" in t and (cmd[i + 1:i + 2] != ["/dev/null"]):
                return False            # a redirection that writes a file (not /dev/null)
        if cmd[:2] == ["rtk", "proxy"] and len(cmd) >= 5 and cmd[3] == "-c":
            if not _only_reads(cmd[4]):
                return False
            continue
        if cmd[0] == "gh" and tuple(cmd[1:3]) in GH_READS:
            continue
        if cmd[0] not in READ_ONLY:
            return False
    return True


def _substitutions(text: str) -> list[str | None]:
    """The contents of each `$( ... )` in `text`, outermost first, or None for one left open."""
    out: list[str | None] = []
    i = 0
    while True:
        start = text.find("$(", i)
        if start < 0:
            return out
        depth, j = 1, start + 2
        while j < len(text) and depth:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        if depth:
            out.append(None)
            return out
        out.append(text[start + 2:j - 1])
        i = j


def waiting_shells(shells: list[int], procs: dict[int, dict], commands: Mapping[int, str]) -> list[int]:
    """The shells among `shells` that are only waiting right now: a waiter (`is_waiter`) whose
    children are all `sleep`, so it is between two looks and not running what follows the loop."""
    out = []
    for s in shells:
        kids = [v for v in procs.values() if v["ppid"] == s]
        if is_waiter(commands.get(s, "")) and kids and all(Path(k["comm"]).name == "sleep" for k in kids):
            out.append(s)
    return out


def command_lines(pids: list[int], *, run=subprocess.run) -> dict[int, str]:
    """{pid: its full command line} for the given processes, from one `ps`."""
    if not pids:
        return {}
    r = run(["/bin/ps", "-o", "pid=,command=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=10)
    out = {}
    for line in (getattr(r, "stdout", "") or "").splitlines():
        head, _, rest = line.strip().partition(" ")
        if head.isdigit():
            out[int(head)] = rest.strip()
    return out


def idle(pid: int, procs: dict[int, dict], transcript_at: dt.datetime | None,
         turn_ended_at: dt.datetime | None, now: dt.datetime,
         prompt_on_screen: bool | None = None, *, quiet_s: float = QUIET_S,
         quiet_unhooked_s: float = QUIET_UNHOOKED_S,
         commands: Mapping[int, str] | None = None) -> tuple[bool, str]:
    """(is the chat idle, why). Every "no" says what it is waiting for.

    `procs` is `config.processes()`, `transcript_at` is `last_activity(...)`, `turn_ended_at`
    is when a Stop hook last fired for it (None when the caller has no such hook), and
    `prompt_on_screen` whether Claude's empty prompt is on its screen (`empty_prompt`), None when
    the caller did not look. `commands` holds the full command line of the chat's shells
    (`command_lines`); with it, a shell that only waits (`waiting_shells`) does not count as a
    running command, because a restart loses nothing of it."""
    shells = shell_children(pid, procs)
    if commands is not None:
        shells = [s for s in shells if s not in waiting_shells(shells, procs, commands)]
    if shells:
        return False, "it is running a command"
    if transcript_at is None:
        return False, "its transcript was not found, so whether it is working is unknown"
    quiet = (now - transcript_at).total_seconds()
    if turn_ended_at is None:
        # no end of turn to go by: only a long silence, and the prompt when it is visible
        if quiet < quiet_unhooked_s:
            return False, f"it has no finished turn on record and wrote {int(quiet)}s ago"
        if prompt_on_screen is False:
            return False, "Claude's prompt is not on its screen"
        return True, f"quiet for {int(quiet_unhooked_s // 60)} minutes with no command running"
    if turn_ended_at < transcript_at - dt.timedelta(seconds=5):
        # ⚠️ A TURN THAT ENDS IN AN API ERROR FIRES NO STOP HOOK (portal-local-gguf, 2026-10-11).
        # Its writes are newer than the last recorded end of turn, so without this the chat read as
        # working for ever. The same rule as a chat with no end on record: a long silence, and
        # Claude's empty prompt on its screen. Unknown screen stays "working".
        if quiet >= quiet_unhooked_s and prompt_on_screen is True:
            return True, (f"its last turn ended without a recorded end (an error), it has been "
                          f"quiet {int(quiet // 60)} minutes and its prompt is empty")
        return False, "it has written since its last turn ended, so it is working"
    if quiet < quiet_s:
        return False, f"its turn ended {int(quiet)}s ago"
    return True, "its turn ended and nothing is running"


def may_be_typing(seconds_since_input: float | None, front_app: str | None, *,
                  terminal: str = "iTerm2", window_s: float = TYPING_WINDOW_S) -> bool:
    """Someone may be typing into a chat: the terminal is in front and the Mac was touched in the
    last two minutes. Unknown means they may be. A phone (Remote Control) keeps its draft on the
    phone, so this is about the Mac's own keyboard."""
    if seconds_since_input is None or front_app is None:
        return True
    return front_app == terminal and seconds_since_input < window_s


#: the footer Claude Code shows for each permission mode, and the --permission-mode that brings it back
MODES = (("bypass permissions on", "bypassPermissions"), ("accept edits on", "acceptEdits"),
         ("plan mode on", "plan"), ("auto mode on", "auto"), ("don't ask on", "dontAsk"))


def permission_mode(screen: str | None) -> str | None:
    """The permission mode the chat is in, from its footer; None when it shows none (the default).
    A resume does not keep a mode switched inside the session, so read it before restarting."""
    low = (screen or "").lower()
    for footer, mode in MODES:
        if footer in low:
            return mode
    return None


#: the flags a restart carries over, and whether each takes a value. Nothing else is kept.
#: Not the rest of the command line: `ps` prints arguments without their quoting, so a chat
#: started with a first prompt reads back as loose words, and the first of them would reach the
#: resumed chat as a new message.
KEEP_FLAGS = {"--allow-dangerously-skip-permissions": False, "--dangerously-skip-permissions": False,
              "-n": True, "--name": True, "--remote-control": True, "--model": True,
              "--add-dir": True, "--effort": True, "--settings": True}


def remote_control_of(path: Path | None) -> bool:
    """Whether the conversation had Remote Control on: its newest `bridge_status` record says
    "/remote-control is active".

    ⛔ A RESTART DROPPED REMOTE CONTROL THAT WAS SWITCHED ON INSIDE THE CHAT (2026-10-11). A chat
    started without --remote-control and given /remote-control later came back from a restart
    without it, because the resume only repeats the start flags, and it dropped out of the session list in the Claude app.
    Claude Code writes the state into the transcript, so the restart reads it there."""
    if path is None:
        return False
    try:
        # newest first: a long transcript is not read whole for its last record of this kind
        for line in _lines_from_end(Path(path)):
            if '"bridge_status"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("type") == "system" and d.get("subtype") == "bridge_status":
                return "is active" in str(d.get("content") or "")
    except OSError:
        return False
    return False


def _lines_from_end(path: Path, block: int = 1 << 20):
    """The file's lines, newest first, read backwards in blocks so a long transcript is not read
    whole to find its last few records."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end, rest = f.tell(), b""
        while end > 0:
            start = max(0, end - block)
            f.seek(start)
            chunk = f.read(end - start) + rest
            lines = chunk.split(b"\n")
            rest = lines.pop(0) if start > 0 else b""
            for line in reversed(lines):
                if line.strip():
                    yield line.decode("utf-8", "replace")
            end = start


def model_of(path: Path | None) -> tuple[str | None, str | None]:
    """The model and the effort level the conversation's newest turn ran on, each None when the
    transcript does not say.

    A restart repeats the flags the chat was started with, so a model or effort chosen inside the
    chat (`/model`, `/effort`) was lost and the chat came back on the default. Claude Code writes
    both on every assistant record, so the restart reads the newest one. The model is
    `requestedModel`, the exact value the chat asked for (a context suffix such as "[1m]"
    included), or `message.model` from versions that do not write it. Turns of a subagent
    (`isSidechain`) and synthetic messages are skipped. A model chosen after the chat's last answer
    is not in the transcript yet, so it is not seen."""
    if path is None:
        return None, None
    try:
        for line in _lines_from_end(Path(path)):
            if '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant" or d.get("isSidechain"):
                continue
            model = d.get("requestedModel") or (d.get("message") or {}).get("model")
            if not model or model.startswith("<"):
                continue
            return model, d.get("effort") or None
    except OSError:
        pass
    return None, None


def title_of(path: Path | None) -> str | None:
    """The chat's own title (`/rename` or `-n`): its newest `custom-title` record, read from the end,
    so a long transcript is not read whole (one of 1.8 GB took two seconds)."""
    if path is None:
        return None
    try:
        for line in _lines_from_end(Path(path)):
            if '"custom-title"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("customTitle"):
                return d["customTitle"]
    except OSError:
        return None
    return None


def background_agent(chat_id: str, now: float | None = None, projects: Path = PROJECTS,
                     quiet_s: float = AGENT_QUIET_S,
                     workflow_quiet_s: float = WORKFLOW_QUIET_S) -> str | None:
    """The name of a background agent or workflow this chat started that is still working, or None.

    ⛔ A BACKGROUND AGENT IS NOT A SHELL. It runs inside the chat's own Claude process and writes to
    <transcript folder>/<chat id>/subagents/, not to the chat's transcript, so a chat whose turn had
    ended read as idle while its agent worked, and a restart would have ended the agent with the
    process. A subagent file written in the last `quiet_s` counts as working, at any depth.

    ⛔ AND A WORKFLOW'S AGENTS ARE ONE FOLDER DEEPER. They write to
    subagents/workflows/<run>/agent-*.jsonl beside the run's journal.jsonl, and only the top folder
    was read, so a chat running a thirty-agent workflow read as idle and a restart ended the whole
    run. A workflow also waits between phases with no agent writing, so a run counts as running
    while its journal was written in the last `workflow_quiet_s` and the run has no final state
    file (<chat id>/workflows/<run>.json, written when it completes, fails or is killed)."""
    t = transcript_of(chat_id, projects)
    if t is None:
        return None
    now = time.time() if now is None else now
    base = t.parent / chat_id
    sub = base / "subagents"
    for f in sorted(sub.rglob("*.jsonl")):
        if f.name == "journal.jsonl":          # a workflow's own log, read below
            continue
        try:
            if now - f.stat().st_mtime < quiet_s:
                return str(f.relative_to(sub).with_suffix(""))
        except OSError:
            continue
    for journal in sorted((sub / "workflows").glob("*/journal.jsonl")):
        run = journal.parent.name
        if (base / "workflows" / f"{run}.json").exists():
            continue
        try:
            if now - journal.stat().st_mtime < workflow_quiet_s:
                return f"workflow {run}"
        except OSError:
            continue
    return None


def resume_line(args: str | None, chat_id: str, mode: str | None = None,
                remote_control: str | bool | None = None, model: str | None = None,
                effort: str | None = None, name: str | None = None) -> str:
    """The command that reopens this conversation with the flags it was started with.

    `args` is the process's command line (`ps -o args=`), and `mode` the permission mode it was
    in (from `permission_mode`). `remote_control` keeps Remote Control on for a chat that had it
    without the flag (`remote_control_of`): a name, or True for an unnamed one. `model` and
    `effort` are what the chat last ran on (`model_of`), and they win over the start flags, so a
    choice made inside the chat survives the restart. A chat started with `--settings` keeps the
    model its settings file names. `name` is the chat's name now: a chat renamed after it started
    still carries its old `-n` in its command line, and a restart must not undo the rename."""
    argv = (args or "").split()
    out = ["claude"]
    if argv and Path(argv[0]).name == "claude":
        i = 1
        while i < len(argv):
            a = argv[i]
            if a in KEEP_FLAGS:
                if KEEP_FLAGS[a]:
                    if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                        out += [a, argv[i + 1]]
                        i += 2
                        continue
                elif a not in out:
                    out.append(a)
            i += 1
    def put(flag: str, value: str) -> None:
        if flag in out:
            out[out.index(flag) + 1] = value
        else:
            out.extend([flag, value])

    if name:
        if "--name" in out:
            out[out.index("--name") + 1] = name
        else:
            put("-n", name)
        if "--remote-control" in out and out.index("--remote-control") + 1 < len(out) \
                and not out[out.index("--remote-control") + 1].startswith("-"):
            out[out.index("--remote-control") + 1] = name
    if model and "--settings" not in out:
        put("--model", model)
    if effort and "--settings" not in out:
        put("--effort", effort)
    if remote_control and "--remote-control" not in out:
        name = remote_control if isinstance(remote_control, str) else next(
            (out[k + 1] for k, a in enumerate(out[:-1]) if a in ("-n", "--name")), None)
        out += ["--remote-control"] + ([name] if name else [])
    tail = ["--permission-mode", mode] if mode else []
    return shlex.join(out + tail + ["--resume", chat_id])


#: words on screen that mean Claude is asking something, so a paste and Enter would answer it
DIALOG = ("enter to confirm", "esc to cancel", "esc to discard", "do you want", "enter to review",
          "trust this folder", "press enter", "(y/n)")


ASKING = "Claude is asking something on its screen"
TYPED = "something is typed in its input line"

#: a colour or attribute code (SGR) and an OSC 8 hyperlink, as a styled tmux capture writes them
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_OSC8 = re.compile(r"\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)")


def _dim_after(params: str, dim: bool) -> bool:
    """Whether text is dim after one SGR code. 2 turns dim on, 22 and a reset turn it off, and the
    arguments of an extended colour (38;5;n, 38;2;r;g;b) are skipped, so a 2 inside one is a colour."""
    codes = re.split(r"[;:]", params) if params else ["0"]
    i = 0
    while i < len(codes):
        c = codes[i] or "0"
        if c in ("38", "48", "58"):
            i += 3 if codes[i + 1:i + 2] == ["5"] else 5 if codes[i + 1:i + 2] == ["2"] else 1
            continue
        if c == "0":
            dim = False
        elif c == "2":
            dim = True
        elif c == "22":
            dim = False
        i += 1
    return dim


def plain_screen(styled: str) -> str:
    """A styled capture as plain text, without the next prompt Claude Code suggests.

    After a turn Claude Code may show a suggested next prompt in its empty input line, drawn dim,
    which Tab accepts. Nobody typed it, and a check for a half-written message must not see it, or
    a chat with a suggestion on screen reads as one somebody is typing into. Dim text is dropped on
    the input line only (the line that starts with ❯); everywhere else the text stays, so a dim
    hint such as "Esc to cancel" still names a dialog. Attributes carry from line to line, as the
    capture writes them only where they change."""
    out, dim = [], False
    for line in _OSC8.sub("", styled).split("\n"):
        cells, pos = [], 0                     # (character, drawn dim)
        for m in _SGR.finditer(line):
            cells += [(ch, dim) for ch in line[pos:m.start()]]
            dim, pos = _dim_after(m.group(1), dim), m.end()
        cells += [(ch, dim) for ch in line[pos:]]
        text = "".join(ch for ch, _ in cells)
        if text.lstrip().startswith("❯"):
            mark = text.index("❯")             # the glyph and what precedes it always stay
            text = "".join(ch for i, (ch, d) in enumerate(cells) if i <= mark or not d)
        out.append(text)
    return "\n".join(out)


def empty_prompt(screen: str | None) -> tuple[bool, str]:
    """Is Claude showing its empty input line, with nothing asked on screen?

    A paste and Enter answer whatever is on screen: a permission question, an exit dialog or the
    trust prompt takes the Enter as a choice, and a half-typed message would be sent with the
    paste joined to it."""
    if not screen:
        return False, "its screen could not be read"
    lines = [ln.rstrip() for ln in screen.splitlines() if ln.strip()]
    tail = "\n".join(lines[-15:]).lower()
    if any(d in tail for d in DIALOG):
        return False, ASKING
    prompts = [ln.strip() for ln in lines[-8:] if ln.strip().startswith("❯")]
    if not prompts:
        return False, "Claude's input line is not on its screen"
    if prompts[-1] != "❯":
        return False, TYPED
    return True, "its input line is empty"


def ready(pid: int, procs: dict[int, dict], chat_id: str, screen: str | None,
          turn_ended_at: dt.datetime | None, now: dt.datetime, *,
          projects: Path = PROJECTS, commands: Mapping[int, str] | None = None) -> tuple[bool, str]:
    """(may this chat be restarted or typed into now, why). Everything a chat produces is read:
    what is on its screen, its transcript and process (`idle`), and its background agents.

    A question on its screen is named first. While Claude waits for an answer the turn has not
    ended, and `idle` alone said "it is working" for a chat that had waited hours for the person.
    Restarting it would lose the question, so it waits either way,
    but the reason has to say who it is waiting for."""
    prompt = empty_prompt(screen)
    if not prompt[0] and prompt[1] == ASKING:
        return False, "Claude is asking a question on its screen and waits for an answer"
    if commands is None:
        commands = command_lines(shell_children(pid, procs))
    ok, why = idle(pid, procs, last_activity(transcript_of(chat_id, projects)), turn_ended_at,
                   now, prompt_on_screen=prompt[0], commands=commands)
    if not ok:
        return ok, why
    agent = background_agent(chat_id, now.timestamp(), projects)
    if agent:
        return False, f"a background agent it started is still working ({agent})"
    return prompt


# The screen decides whether a chat is idle, so it must be the screen of the session whose id
# matched: `(get ...)` holds ids, where the bare collections hold positions (see inject.py).
ITERM_SCREEN = """on run argv
  tell application "iTerm2"
    repeat with w in (get windows)
      -- a window iTerm2 keeps after it closed has tabs that cannot be read: skip it, never fail
      try
        repeat with t in (get tabs of w)
          repeat with s in (get sessions of t)
            if (id of s as text) is (item 1 of argv) then return contents of s
          end repeat
        end repeat
      end try
    end repeat
  end tell
  return ""
end run"""


def screen_of(kind: str, handle: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """The text on a chat's screen: a tmux session by name, or an iTerm2 session by id."""
    if kind == "tmux":
        return plain_screen(_chat.pane_text(handle, styled=True, env=env)) or None
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", ITERM_SCREEN, handle], capture_output=True,
                           text=True, timeout=20, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def last_start(path: Path | None) -> str | None:
    """When the chat's newest instance started: the timestamp of the last SessionStart hook record
    Claude Code wrote into the transcript, or None. Read at the start of a new instance, before its
    own record is written, it is when the instance that just ended started."""
    if path is None:
        return None
    found = None
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b"SessionStart" not in raw or b'"attachment"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                a = rec.get("attachment") or {}
                if str(a.get("hookEvent") or a.get("hookName") or "").startswith("SessionStart"):
                    found = rec.get("timestamp") or found
    except OSError:
        return None
    return found


def unfinished_background(path: Path | None, since: str | None = None) -> list[dict]:
    """Background shell commands a chat started that never finished: a `run_in_background` tool
    call with no task notification after it. Each is {"tool_use_id", "command", "description",
    "at"}, oldest first, or [] when the transcript cannot be read.

    When a chat is stopped (a restart, a crash), its background commands stop with it, and the
    resumed chat is told only that "a background shell command didn't finish", without the
    command. `since` (an ISO time, as the transcript writes it) keeps only commands started from
    then on: pass when the instance that just ended started, or older stops come back each time.
    Read this when a conversation is resumed, before Claude Code writes those notices,
    and hand the commands back to it so it can start again what it still needs. Lines are read
    with a cheap substring test first, so a transcript of hundreds of megabytes takes seconds."""
    if path is None:
        return []
    started: dict[str, dict] = {}
    seen: set[str] = set()
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b"run_in_background" in raw and b'"tool_use"' in raw:
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    for c in (rec.get("message") or {}).get("content") or []:
                        inp = c.get("input") or {} if isinstance(c, dict) else {}
                        if c.get("type") == "tool_use" and inp.get("run_in_background") and c.get("id"):
                            started[c["id"]] = {"tool_use_id": c["id"], "command": str(inp.get("command", "")),
                                                "description": str(inp.get("description", "")),
                                                "at": rec.get("timestamp")}
                elif b"<task-notification>" in raw:
                    seen.update(m.decode() for m in re.findall(rb"<tool-use-id>([^<]+)</tool-use-id>", raw))
    except OSError:
        return []
    return [t for k, t in started.items()
            if k not in seen and (since is None or (t["at"] or "") >= since)]


def files_seen(path: Path | None, since: str | None = None) -> dict[Path, str]:
    """When the conversation last had each file's whole content: it wrote the file (Write, Edit,
    MultiEdit) or read all of it (a Read that started at the first line and reached the last). It is
    {resolved path: the time of that tool's result, as the transcript writes it}, keeping results
    from `since` on, or {} when the transcript cannot be read.

    Claude Code refuses to change a file the conversation has not read, or one that changed since it
    was read, so after a successful write the conversation's copy is the file. A failed call leaves
    only an error text, and an edit the person changed before accepting it (`userModified`) leaves
    a file the conversation has not seen, so neither counts.

    Use it before telling a chat that a file it loads has changed: a chat that changed the file
    itself already knows. Compare each file's modification time with its time here, and a file
    modified later than that was changed by someone else."""
    if path is None:
        return {}
    seen: dict[Path, str] = {}
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b'"toolUseResult"' not in raw or b'"filePath"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                at = rec.get("timestamp")
                if not isinstance(at, str) or (since is not None and at < since):
                    continue
                name = _whole_file(rec.get("toolUseResult"))
                if name:
                    try:
                        seen[Path(name).resolve()] = at
                    except (OSError, RuntimeError):
                        continue
    except OSError:
        return {}
    return seen


def tools_held(path: Path | None) -> dict[str, dict]:
    """The deferred tool definitions the conversation holds now: {tool name: {"description": its
    text, "args": the names of its arguments}} for each tool it loaded since its last compaction, as
    its newest load recorded it. {} when the transcript cannot be read or holds none.

    Claude Code loads a deferred tool (an MCP tool found through its tool search) once and writes
    the definition it got into the transcript. A resumed conversation keeps using those recorded
    definitions until it is compacted, so restarting a chat does not give it an argument the server
    gained since: the model never sees it, and the call goes out without it. A compaction clears the
    loaded tools, and the next load reads the server again.

    Compare these with the server's live tool list before telling a resumed chat that its tools
    are current, and name the arguments it cannot pass yet."""
    if path is None:
        return {}
    held: dict[str, dict] = {}
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b'"deferred_tools_record"' not in raw and b'"compact_boundary"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
                    held = {}
                    continue
                a = rec.get("attachment")
                if rec.get("type") != "attachment" or not isinstance(a, dict) \
                        or a.get("type") != "deferred_tools_record":
                    continue
                for e in a.get("entries") or []:
                    if not isinstance(e, dict) or not e.get("name"):
                        continue
                    schema = e.get("input_schema") if isinstance(e.get("input_schema"), dict) else {}
                    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
                    held[e["name"]] = {"description": str(e.get("description") or ""), "args": set(props)}
    except OSError:
        return {}
    return held


def _whole_file(result) -> str | None:
    """The path a tool result shows the whole content of, or None (a range, an image, an error)."""
    if not isinstance(result, dict) or result.get("userModified"):
        return None
    f = result.get("file")
    if isinstance(f, dict):
        n, total = f.get("numLines"), f.get("totalLines")
        whole = (result.get("type") == "text" and f.get("startLine", 1) == 1
                 and isinstance(n, int) and isinstance(total, int) and n >= total)
        return f.get("filePath") if whole else None
    if "structuredPatch" in result or "edits" in result or result.get("type") in ("create", "update"):
        return result.get("filePath")
    return None


#: error kinds of an "API Error:" stop that waiting and asking again will not cure
NOT_TRANSIENT = {"invalid_request", "authentication_failed", "billing_error"}


def _text_of(d: dict) -> str:
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def stopped_on_api_error(path: Path | None, scan: int = 2000) -> dict | None:
    """The transient API error a conversation stopped on, or None.

    When the connection to the API drops mid-turn ("API Error: Connection lost mid-response"),
    Claude Code ends the turn with an assistant record marked `isApiErrorMessage`, and the session
    then waits at its prompt as if it had finished. This answers {"uuid", "at", "error", "text"} when
    that record is the conversation's newest message (nothing typed or queued after it) and its error
    is transient: the text starts with "API Error:" and the kind is not one of NOT_TRANSIENT. A usage
    limit ("You've hit your session limit"), a prompt that is too long, billing or an expired login
    are not, since asking again cannot help. At most `scan` lines are read from the end."""
    if path is None:
        return None
    try:
        for n, line in enumerate(_lines_from_end(Path(path))):
            if n >= scan:
                return None
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") not in ACTIVITY or d.get("isSidechain"):
                continue
            if d.get("type") != "assistant" or not d.get("isApiErrorMessage"):
                return None
            text = _text_of(d).strip()
            if not text.startswith("API Error:") or d.get("error") in NOT_TRANSIENT:
                return None
            return {"uuid": d.get("uuid"), "at": d.get("timestamp"), "error": d.get("error"),
                    "text": text}
    except OSError:
        return None
    return None


def transcript_of(chat_id: str, projects: Path = PROJECTS) -> Path | None:
    """The transcript of a conversation, in whichever project folder holds it (newest if several)."""
    hits = list(projects.glob(f"*/{chat_id}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


#: transcript records that mean the conversation moved: a message, or one typed and queued
ACTIVITY = {"user", "assistant", "queue-operation"}


def _local_command(d: dict) -> bool:
    """A record of a slash command run inside Claude Code, or of its output."""
    if d.get("type") != "user":
        return False
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return isinstance(c, str) and c.lstrip().startswith(("<command-name>", "<local-command-"))


def last_activity(path: Path | None, first: int = 262144, cap: int = 64 << 20) -> dt.datetime | None:
    """When the conversation last moved: the newest message record in the transcript.

    Not the file's modification time: Claude Code appends bookkeeping after a turn has ended, so
    an idle chat's file keeps being touched. And not only the last few hundred kilobytes: a large
    transcript can end in megabytes of bookkeeping, so the tail read grows until a message is
    found or `cap` is reached."""
    if path is None:
        return None
    size = first
    try:
        total = path.stat().st_size
    except OSError:
        return None
    while True:
        try:
            with open(path, "rb") as f:
                f.seek(max(0, total - size))
                lines = f.read(min(size, total)).splitlines()
        except OSError:
            return None
        if size < total:
            lines = lines[1:]          # the first line of a window may be cut
        for raw in reversed(lines):
            if b'"timestamp"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            # a compaction writes a user record ("This session is being continued ...") with no turn
            # around it, so counting it made an idle chat look busy for ever
            if d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
                continue
            # a local slash command (/reload-plugins, /model ...) is recorded as user records and
            # starts no turn, so nothing ends it: keep_current's own refresh made every chat it
            # refreshed look busy from then on. A command that does start a turn is still seen,
            # through the assistant records that follow it.
            if d.get("isMeta") or _local_command(d):
                continue
            if d.get("type") in ACTIVITY and d.get("timestamp"):
                try:
                    return dt.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
                except ValueError:
                    continue
        if size >= total or size >= cap:
            return None
        size *= 4


def seconds_since_input(*, env: Mapping[str, str] | None = None) -> float | None:
    """The Mac's own count since the last keyboard, mouse or trackpad event (IOHIDSystem)."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"], capture_output=True,
                             text=True, timeout=10, env=env).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    vals = [int(m) for m in re.findall(r'"HIDIdleTime" = (\d+)', out)]
    return min(vals) / 1e9 if vals else None


def front_app(*, bundle_id: bool = False, env: Mapping[str, str] | None = None) -> str | None:
    """The name of the application in front, or its bundle id (e.g. "com.googlecode.iterm2").

    `lsappinfo` asks LaunchServices, so it needs no Automation grant and always gives a fresh
    answer. ⚠️ AppKit's frontmostApplication does not: inside one process with no run loop it keeps
    its first answer (measured 2026-10-11, it still said Brave after Finder had come forward). On a
    locked screen the app in front is com.apple.loginwindow."""
    field, key = ("bundleid", "CFBundleIdentifier") if bundle_id else ("name", "LSDisplayName")
    try:
        asn = subprocess.run(["/usr/bin/lsappinfo", "front"], capture_output=True, text=True,
                             timeout=5, env=env).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(["/usr/bin/lsappinfo", "info", "-only", field, asn],
                             capture_output=True, text=True, timeout=5, env=env).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(rf'"{key}"="([^"]*)"', out)
    return m.group(1) if m else None


def process_args(pid: int, *, env: Mapping[str, str] | None = None) -> str:
    """A process's command line, as ps prints it (without quoting)."""
    try:
        return subprocess.run(["/bin/ps", "-o", "args=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5, env=env).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _wait(pred: Callable[[], bool], seconds: float, step: float = 2.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def restart_iterm(pid: int, session_id: str, cwd: str, chat_id: str, *,
                  env: Mapping[str, str] | None = None, stop_s: float = 30,
                  back_s: float = 90) -> tuple[bool, str]:
    """End the chat in an iTerm2 session and resume the same conversation there.

    The permission mode is read from the footer and the start flags from the process, the process
    gets SIGTERM, and once the session is back at a shell the resume line is typed into it. The
    answer is (back at Claude's prompt, what happened)."""
    mode = permission_mode(screen_of("iterm", session_id, env=env))
    args = process_args(pid, env=env)
    transcript = transcript_of(chat_id)
    rc = remote_control_of(transcript) or None
    model, effort = model_of(transcript)
    name = title_of(transcript)   # a rename after the start is kept, not undone by the old -n
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, TypeError, ValueError) as e:
        return False, f"could not end the Claude process: {e}"

    def session():
        return _inject.iterm_sessions(env=env).get(session_id)

    def at_shell():
        s = session()
        return s is not None and not _inject.looks_like_claude("iterm", s)

    if not _wait(at_shell, stop_s):
        return False, f"Claude did not stop within {int(stop_s)} seconds of being asked to"
    cmd = resume_line(args, chat_id, mode, remote_control=rc, model=model, effort=effort, name=name)
    ok, detail = _inject.send_iterm(session_id, f"cd {shlex.quote(cwd)} && {cmd}", True, env=env)
    if not ok:
        return False, f"could not type the resume command ({detail})"

    def back():
        s = session()
        return s is not None and _inject.looks_like_claude("iterm", s)

    return _wait(back, back_s), f"iTerm session {session_id} resumed"


def restart_tmux(pid: int, name: str, chat_id: str, *, command: str | None = None,
                 env: Mapping[str, str] | None = None, stop_s: float = 30,
                 back_s: float = 90) -> tuple[bool, str]:
    """End the chat in tmux session `name` and resume the same conversation INSIDE ITS OWN PANE.

    The pane's shell outlives Claude (`chat.new` and `restart_into_tmux` both start a shell and type
    claude into it), so the session, the terminal showing it and its desktop all stay as they are.
    ⛔ This used to end the session and start a new one, which opened a new window for a chat that
    had been shown in a tab the person chose (2026-10-11). `command` is the resume line when the
    caller knows better (a chat on a local model); by default it is built from the process."""
    if command is None:
        mode = permission_mode(screen_of("tmux", name, env=env))
        transcript = transcript_of(chat_id)
        rc = remote_control_of(transcript) or None
        model, effort = model_of(transcript)
        # the title the chat has now: a rename after the start is kept, not undone by the old -n
        command = resume_line(process_args(pid, env=env), chat_id, mode, remote_control=rc,
                              model=model, effort=effort, name=title_of(transcript))
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, TypeError, ValueError) as e:
        return False, f"could not end the Claude process in {name}: {e}"

    def pane():
        return _inject.tmux_sessions(env=env).get(name)

    if not _wait(lambda: pane() is not None and not _inject.looks_like_claude("tmux", pane()), stop_s):
        return False, f"Claude did not stop in {name} within {int(stop_s)} seconds"
    ok, how = _inject.send_tmux(name, command, True, env=env)
    if not ok:
        return False, f"could not type the resume line into {name} ({how})"
    if not _wait(lambda: pane() is not None and _inject.looks_like_claude("tmux", pane()), back_s):
        return False, f"{name} did not come back to Claude within {int(back_s)} seconds"
    return True, f"tmux {name} resumed in its own pane"


def tmux_name(title: str | None, chat_id: str) -> str:
    """The tmux session for a chat: claude-<its title>, or claude-<the start of its id>. Only
    letters, digits, - and _, because tmux reads . and : in a target."""
    return "claude-" + re.sub(r"[^A-Za-z0-9_-]", "-", title or chat_id[:8] or "chat")


def restart_into_tmux(pid: int, session_id: str, cwd: str, chat_id: str, *,
                      title: str | None = None, cc: bool = False,
                      env: Mapping[str, str] | None = None, stop_s: float = 30,
                      back_s: float = 90) -> tuple[bool, dict]:
    """Move a chat that runs in an iTerm2 tab into a tmux session shown in THAT SAME TAB.

    It is a restart with a different last line: the mode and flags are read, Claude gets SIGTERM,
    and once the tab is back at its shell one line (`chat.attach_line`) starts the session, resumes
    the conversation in it and attaches the tab. No window is made or moved. The caller decides
    whether the chat may be touched (`ready`, `may_be_typing`). Refused when the running tmux
    server cannot script iTerm2 (`chat.server_context`), because the chat would lose iTerm2's
    permissions. Remote Control comes back named after the chat, since a chat in tmux is reached
    by name. The answer is (moved, detail): "tmux", "tab" and "shown", or "step" and "why"."""
    context = _chat.server_context(env=env)
    if context not in ("none", "iterm"):
        return False, {"step": "check", "why": f"the tmux server cannot script iTerm2 ({context}), "
                                               "so the chat would lose iTerm2's permissions"}
    title = title or title_of(transcript_of(chat_id))
    name = tmux_name(title, chat_id)
    if name in _inject.tmux_sessions(env=env):
        return False, {"step": "check", "why": f"a tmux session called {name} already exists"}
    mode = permission_mode(screen_of("iterm", session_id, env=env))
    model, effort = model_of(transcript_of(chat_id))
    command = resume_line(process_args(pid, env=env), chat_id, mode, remote_control=title or True,
                          model=model, effort=effort)
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, TypeError, ValueError) as e:
        return False, {"step": "stop", "why": f"could not end the Claude process: {e}"}

    def at_shell():
        s = _inject.iterm_sessions(env=env).get(session_id)
        return s is not None and not _inject.looks_like_claude("iterm", s)

    if not _wait(at_shell, stop_s):
        return False, {"step": "stop", "why": f"Claude did not stop within {int(stop_s)} seconds"}
    sent, how = _inject.send_iterm(session_id, _chat.attach_line(cwd, name, command, cc), True, env=env)
    if not sent:
        return False, {"step": "type", "why": f"could not type into the tab ({how})"}

    def back():
        s = _inject.tmux_sessions(env=env).get(name)
        return s is not None and _inject.looks_like_claude("tmux", s)

    if not _wait(back, back_s):
        return False, {"step": "resume", "tmux": name,
                       "why": f"the chat did not come back in tmux within {int(back_s)} seconds"}
    return True, {"tmux": name, "tab": session_id, "shown": "iTerm tmux mode" if cc else "plain tmux"}
