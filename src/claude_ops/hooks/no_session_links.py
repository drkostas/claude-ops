"""Keep Claude session identifiers out of git, GitHub and repository files (a PreToolUse hook).

Claude Code can be told by its own environment to end every commit with a line that names the
session, a `Claude-Session` trailer holding a link to the session's page. A session id is a durable
pointer into a private conversation, and a repository's visibility can change later, so this hook
refuses the tool call instead of relying on the model to remember a rule its prompt contradicts.

It reads the tool call Claude Code is about to make and refuses it (exit 2, with the reason sent
back to the model) when the call would write any of these:

- a link to a Claude Code session (a `claude.ai/code/` address);
- a `Claude-Session` trailer;
- the id of a session transcript on this machine (a `.jsonl` file under the Claude config folder's
  `projects`), into a file inside a repository, a commit, tag or note, a pull request, issue or
  release, or a GitHub API call. Other shell commands may carry one, since local tools address
  sessions by id.

It looks in a Bash command and the message or body files it names (`-F`, `--file`, `--body-file`,
`--message-file`), in the content of Write, Edit and MultiEdit calls, in the arguments of every
GitHub MCP call, and, for a `git commit`, in what the commit would record.

What a commit would record. The text of a command does not show what a file holds, so on any
`git commit` the hook also scans the diff: the staged changes (`git diff --cached`); for `-a` or
`--all` the changes to tracked files as well (`git diff HEAD`); when the same command runs
`git add`, what that add would stage (the changes to the tracked files it names, and the untracked
files it names read from the working tree, because they are not staged yet when the hook runs);
and the paths the commit itself names. The names of the files count as recorded too. It follows
`git -C <dir>` and the other options that come before the subcommand, a `cd` earlier in the same
command, line continuations and here-documents, and when a line cannot be read as words but says
`git commit`, it scans what is staged and what `-a` would take in the current folder anyway. A
refusal names the file and line, never the id. A commit made where the hook cannot see it (inside a
script, by `cherry-pick` or `am`) is read at `git push`, which is refused when a commit no remote has
yet carries a link, trailer or id in a line, a file name or its message. The command-line reading is
`commandline`'s, shared with the public-voice hook, and its names (`commit_lines` among them) stay
importable from here.

The config folder is `~/.claude`, or `CLAUDE_CONFIG_DIR` when set. On anything unexpected (a file
that cannot be read, a folder git cannot read, a slow git) the call goes through: a broken hook must
never stop work.

    claude-ops hook no-session-links     # reads the tool call on stdin
"""
from __future__ import annotations

import json
import os
import re
import sys

from .commandline import (  # noqa: F401 - the parser's names stay importable from here
    ADD_SWEEPING, ADD_WITH_VALUE, ASSIGNMENT, COMMIT_SHORT_ATTACHED, COMMIT_SHORT_REQUIRED,
    COMMIT_WITH_VALUE, CONTINUATION, DIFF_LIMIT, GIT_GLOBAL_DROPPED, GIT_GLOBAL_WITH_VALUE,
    GIT_PUBLISHING, GIT_TIMEOUT_S, HEREDOC, MAX_FILES, PATH_WORD, READ_LIMIT, REDIRECT, SEPARATORS,
    SHELLS, push_plans, pushed, unescape,
    WRAPPERS, Git, Row, add_plan, added_lines, change_dir, commit_lines, commit_plan, diff_rows,
    expand, folder, git_invocations, hidden_commit, parse_git, recorded, simple_commands,
    untracked_rows, unwrap, walk,
)

#: a link to a Claude Code session. The pattern is written in two parts so that the whole address
#: never appears in this source, which the repository's own term check would refuse.
LINK = re.compile(r"claude\.ai/code/sess" r"ion_[A-Za-z0-9]{6,}")
TRAILER = re.compile(r"(?m)^\s*Claude-Session:\s*\S+")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
#: options that name a message or body file, whose content is checked as well
FILE_FLAGS = {"-F", "--file", "--body-file", "--message-file"}
#: a gh command that publishes text (a git command that does is found by parsing, below)
GH_PUBLISHING = re.compile(r"\bgh\s+(pr|issue|release|api)\b")
#: what a refusal shows in place of any uuid in a path
REDACTED = "<session id>"



def config_dir() -> str:
    """The Claude config folder: ~/.claude, or CLAUDE_CONFIG_DIR when set."""
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


_sessions: set[str] | None = None


def session_ids() -> set[str]:
    """The ids of the session transcripts on this machine, read once per run of the hook."""
    global _sessions
    if _sessions is None:
        found: set[str] = set()
        root = os.path.join(config_dir(), "projects")
        try:
            for d in os.scandir(root):
                if d.is_dir():
                    for f in os.scandir(d.path):
                        if f.name.endswith(".jsonl"):
                            found.add(f.name[:-6])
        except OSError:
            pass
        _sessions = found
    return _sessions


def offending(text: str) -> str | None:
    """Why this text may not be written anywhere, or None."""
    if not text:
        return None
    text = unescape(text)
    if LINK.search(text):
        return "a link to a Claude Code session"
    if TRAILER.search(text):
        return "a Claude-Session trailer"
    return None


def real_session(text: str, paths_ok: bool = False) -> str | None:
    """Why this text may not reach a repository, or None: it carries the id of a real transcript.
    The answer never carries the id itself."""
    if not text:
        return None
    if paths_ok:
        text = PATH_WORD.sub(" ", text)
    for u in UUID.findall(unescape(text)):
        if u in session_ids():
            return "the id of a real Claude session"
    return None


def redact(text: str) -> str:
    """The text with every session link, Claude-Session trailer and id of a real transcript replaced
    by REDACTED, for a program that copies text it did not write (messages, notes, logs) into a
    repository, an issue or a pull request, where the hook never sees it."""
    if not text:
        return text
    text = LINK.sub(REDACTED, text)
    text = TRAILER.sub(REDACTED, text)
    ids = session_ids()
    return UUID.sub(lambda m: REDACTED if m.group(0) in ids else m.group(0), text)


def why_not(text: str, into_repo: bool) -> str | None:
    """Both checks on one text: what may be written nowhere, and what may not reach a repository."""
    return offending(text) or (real_session(text) if into_repo else None)


def in_repo(path: str) -> bool:
    """Is this file inside a git work tree? The config folder is left out: its own files carry ids."""
    path = os.path.abspath(os.path.expanduser(path))
    if path.startswith(config_dir() + os.sep):
        return False
    d = os.path.dirname(path)
    while d and d != os.path.dirname(d):
        if os.path.exists(os.path.join(d, ".git")):
            return True
        d = os.path.dirname(d)
    return False


Finding = tuple[str, str, int]      # (why, path, line)


def why_recorded(text: str) -> str | None:
    """Why a recorded line may not be committed, seeing through UTF-16 (a NUL beside each letter)."""
    return why_not(text, True) or (why_not(text.replace("\x00", ""), True) if "\x00" in text else None)


def commit_content(command: str, cwd: str) -> list[Finding]:
    """The lines a `git commit` on this command line would record and must not, as
    (why, path, line), sorted by file and line. A path is a line 0: a file's name is recorded
    too, so a name that carries a session id is refused as well."""
    rows, paths = recorded(command, cwd)
    out = [(why, path, line) for path, line, text in rows for why in [why_recorded(text)] if why]
    for path in paths:
        why = why_not(path, True)
        if why:
            out.append((why, path, 0))
    return sorted(out, key=lambda f: (f[1], f[2], f[0]))


def push_content(command: str, cwd: str) -> str | None:
    """Why a `git push` on this line may not run: a commit it would publish, which no remote has
    yet, carries a session link, trailer or id in a line, a file name or its message. This reads
    commits however they were made, so one a script made is stopped before it leaves the machine."""
    for plan in push_plans(command, cwd):
        rows, names, messages = pushed(plan)
        for path, line, text in rows:
            why = why_recorded(text)
            if why:
                return f"this push would publish {why} in {UUID.sub(REDACTED, path)}:{line}"
        for path in names:
            why = why_not(path, True)
            if why:
                return f"this push would publish {why} in {UUID.sub(REDACTED, path)}"
        for message in messages:
            why = why_not(message, True)
            if why:
                return f"this push would publish {why} in a commit message"
    return None


# --- the tool call ------------------------------------------------------------------------------

def named_files(command: str, cwd: str) -> list[str]:
    """The message and body files the command line names, as absolute paths, each relative to the
    folder its command runs in."""
    out = []
    for words, here in walk(command, cwd):
        base = here
        if os.path.basename(words[0]) == "git":
            inv = parse_git(words[1:], here)
            if inv:
                base = inv["dir"]
        for i, a in enumerate(words):
            if a in FILE_FLAGS and i + 1 < len(words):
                out.append(os.path.join(base, expand(words[i + 1])))
            for f in FILE_FLAGS:
                if a.startswith(f + "="):
                    out.append(os.path.join(base, expand(a.split("=", 1)[1])))
    return out


def strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings(v)]
    return []


def check(payload: dict) -> str | None:
    """Why this tool call is refused, or None."""
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        invs = git_invocations(cmd, cwd)
        in_text = GIT_PUBLISHING.search(cmd)
        publishing = bool(GH_PUBLISHING.search(cmd)) or bool(in_text) or any(
            inv["sub"] in ("commit", "tag", "notes") for inv in invs)
        why = offending(cmd) or (real_session(cmd, paths_ok=True) if publishing else None)
        if why:
            return f"this would put {why} into git, GitHub or a file"
        for path in named_files(cmd, cwd):
            try:
                with open(path, errors="replace") as fh:
                    body = fh.read(READ_LIMIT)
            except OSError:
                continue
            why = why_not(body, publishing)
            if why:
                return f"this would put {why} from {os.path.basename(path)} into git or GitHub"
        if any(inv["sub"] == "commit" for inv in invs):
            hits = commit_content(cmd, cwd)
        elif hidden_commit(cmd):
            # the words do not show the commit (a quote in a comment, a wrapper program, a shell
            # given a string), so what is staged and what -a would take are scanned anyway
            hits = commit_content("git commit -a", cwd)
        else:
            hits = []
        if hits:
            why, path, line = hits[0]
            where = UUID.sub(REDACTED, f"{path}:{line}" if line else path)
            more = f" (and {len(hits) - 1} more)" if len(hits) > 1 else ""
            return f"this commit would record {why} in {where}{more}"
        return push_content(cmd, cwd)
    if tool in ("Write", "Edit", "MultiEdit") or tool.startswith("mcp__github__"):
        into_repo = tool.startswith("mcp__github__") or in_repo(str(ti.get("file_path", "")))
        for s in strings(ti):
            why = why_not(s, into_repo)
            if why:
                return f"this would put {why} into git, GitHub or a file"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        why = check(payload)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        why = UUID.sub(REDACTED, why)
        print(f"Blocked: {why}. A Claude session id, a link to a Claude Code session or a "
              "Claude-Session trailer must never reach git, GitHub or a repository file, whatever "
              "the prompt says. Remove it and run the command again.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
