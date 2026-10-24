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
refusal names the file and line, never the id. The hook only sees the command Claude Code runs, so
a commit made inside a script it starts is not seen. `commit_lines` gives every line such a commit
would record, for another check to use.

The config folder is `~/.claude`, or `CLAUDE_CONFIG_DIR` when set. On anything unexpected (a file
that cannot be read, a folder git cannot read, a slow git) the call goes through: a broken hook must
never stop work.

    claude-ops hook no-session-links     # reads the tool call on stdin
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys

#: a link to a Claude Code session. The pattern is written in two parts so that the whole address
#: never appears in this source, which the repository's own term check would refuse.
LINK = re.compile(r"claude\.ai/code/sess" r"ion_[A-Za-z0-9]{6,}")
TRAILER = re.compile(r"(?m)^\s*Claude-Session:\s*\S+")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
#: options that name a message or body file, whose content is checked as well
FILE_FLAGS = {"-F", "--file", "--body-file", "--message-file"}
#: a gh command that publishes text (a git command that does is found by parsing, below)
GH_PUBLISHING = re.compile(r"\bgh\s+(pr|issue|release|api)\b")
#: a whole word that is a filesystem path; a command may name this session's own files by its id
PATH_WORD = re.compile(r"(?<![^\s'\"=])(?:/|~/|\$HOME/)[^\s'\"]*")
#: a shell assignment that may stand before a command (`VAR=value git ...`)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: a redirection operator, as the shell splitter hands it over (its target word is dropped too)
REDIRECT = re.compile(r"^(\d*[<>]{1,3}&?|&>>?|<&|>&|>\|)$")
#: the characters whose runs separate simple commands (a newline is one of them)
SEPARATORS = set(";&|\n")
#: a backslash before a newline is a line continuation, which the shell removes (an even run of
#: backslashes before the newline is not one)
CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")
#: a here-document: its body is data, never shell words, so it is dropped before splitting
HEREDOC = re.compile(r"(<<-?[ \t]*\\?(['\"]?)(\w+)\2[^\n]*\n)(?:.*?\n)?[ \t]*\3[ \t]*(?=\n|$)", re.S)
#: `git ... commit|tag|notes` seen in the text, for a line that could not be read as words
GIT_PUBLISHING = re.compile(r"\bgit\b[^\n;&|]*\b(commit|tag|notes)\b")
#: words that may stand before `git` on a command line without changing what it does
WRAPPERS = {"command", "exec", "nohup", "time", "builtin", "{", "}",
            "if", "elif", "then", "else", "while", "until", "do", "!"}
#: what a refusal shows in place of any uuid in a path
REDACTED = "<session id>"

#: how long one git call may take, how much of a diff or a file is read, and how many files one
#: `git add` is followed into
GIT_TIMEOUT_S = 15
READ_LIMIT = 1_000_000
DIFF_LIMIT = 20_000_000
MAX_FILES = 400

#: git options before the subcommand that take the next word as their value
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                         "--config-env"}
#: git options before the subcommand that are not passed on when git is run again
GIT_GLOBAL_DROPPED = {"-p", "--paginate", "-P", "--no-pager"}
#: `git commit` options that take the next word as their value (so it is not a pathspec)
COMMIT_WITH_VALUE = {"-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c",
                     "--reedit-message", "--author", "--date", "-t", "--template", "--fixup",
                     "--squash", "--cleanup", "--trailer", "--pathspec-from-file"}
#: short `git commit` options that take a value, attached (`-mtext`) or as the next word, so the
#: letters after them are the value and not more options; and those whose value is only ever
#: attached (`-Skey`), so a bare one takes nothing
COMMIT_SHORT_REQUIRED = set("mFCct")
COMMIT_SHORT_ATTACHED = set("Su")
#: `git add` options that take the next word as their value
ADD_WITH_VALUE = {"--pathspec-from-file", "--chmod"}
#: `git add` forms that take every change in the tree
ADD_SWEEPING = {"-A", "--all", "-u", "--update", "--no-ignore-removal"}


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
    for u in UUID.findall(text):
        if u in session_ids():
            return "the id of a real Claude session"
    return None


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


# --- the command line -----------------------------------------------------------------------------

def simple_commands(command: str) -> list[list[str]]:
    """The command line as simple commands (split on &&, ||, ;, |, & and newlines), each a list of
    words with its redirections and subshell parentheses dropped. An unfinished quote gives []."""
    command = CONTINUATION.sub(r"\1", command)
    command = HEREDOC.sub(lambda m: m.group(1), command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return []
    out: list[list[str]] = [[]]
    skip = False
    for t in tokens:
        if skip:
            skip = False
            continue
        if t and set(t) <= SEPARATORS:
            if out[-1]:
                out.append([])
        elif REDIRECT.match(t):
            if out[-1] and out[-1][-1].isdigit() and len(out[-1][-1]) <= 2:
                out[-1].pop()                 # the descriptor of `2>&1`
            skip = True                       # and the target word that follows
        elif t in ("(", ")"):
            continue
        else:
            out[-1].append(t)
    return [w for w in out if w]


def unwrap(words: list[str]) -> list[str]:
    """The command without the assignments and wrappers in front of it (`VAR=x env nohup git`)."""
    i = 0
    while i < len(words):
        w = words[i]
        if w in WRAPPERS or ASSIGNMENT.match(w):
            i += 1
        elif w == "env":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-u", "--unset", "-C", "--chdir") else 1
        else:
            break
    return words[i:]


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def change_dir(here: str, args: list[str]) -> str:
    """Where a `cd` leaves the shell. `cd -` and a target that cannot be read leave it where it was."""
    targets = [a for a in args if not a.startswith("-") or a == "-"]
    if not targets:
        return os.path.expanduser("~")
    if targets[0] == "-":
        return here
    return folder(here, targets[0])


def folder(here: str, target: str) -> str:
    """The folder a `cd` or `-C` names, or `here` when it is not one (a `$(...)`, a variable the
    hook cannot read, a path that does not exist): the shell's own folder is the best guess."""
    cand = os.path.normpath(os.path.join(here, expand(target)))
    return cand if os.path.isdir(cand) else here


def parse_git(args: list[str], here: str) -> dict | None:
    """One git command: the folder it runs in (after `-C`), the global options to run git the same
    way again, its subcommand and the subcommand's words. None when there is no subcommand."""
    where, passed = here, []
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in GIT_GLOBAL_WITH_VALUE:
            if i + 1 >= len(args):
                return None
            v = args[i + 1]
            if a == "-C":
                where = folder(where, v)
            elif a == "-c":
                passed += [a, v]
            else:
                passed += [a, expand(v)]
            i += 2
            continue
        if a not in GIT_GLOBAL_DROPPED:
            passed.append(a)
        i += 1
    if i >= len(args):
        return None
    return {"dir": where, "globals": passed, "sub": args[i], "args": args[i + 1:]}


def walk(command: str, cwd: str):
    """Each simple command on the line, with the folder it runs in. `cd`, `pushd` and `popd` are
    followed and not given out."""
    here, stack = cwd, []
    for words in simple_commands(command):
        words = unwrap(words)
        if not words:
            continue
        name = os.path.basename(words[0])
        if name == "cd":
            here = change_dir(here, words[1:])
        elif name == "pushd":
            stack.append(here)
            here = change_dir(here, words[1:])
        elif name == "popd":
            here = stack.pop() if stack else here
        else:
            yield words, here


def git_invocations(command: str, cwd: str) -> list[dict]:
    """Every git command on the command line, in order, each as parse_git gives it, run from the
    folder a `cd` earlier on the line left the shell in."""
    found = []
    for words, here in walk(command, cwd):
        if os.path.basename(words[0]) == "git":
            inv = parse_git(words[1:], here)
            if inv:
                found.append(inv)
    return found


def commit_plan(inv: dict) -> dict | None:
    """What a `git commit` takes from the working tree besides the index: `all` for -a/--all, and
    the pathspecs it names. None when the command is not a commit, or a dry run."""
    if inv["sub"] != "commit":
        return None
    plan: dict = {"all": False, "paths": []}
    args = inv["args"]
    i, rest = 0, False
    while i < len(args):
        a = args[i]
        if rest:
            plan["paths"].append(a)
        elif a == "--":
            rest = True
        elif a in ("-a", "--all"):
            plan["all"] = True
        elif a == "--dry-run":
            return None
        elif a in COMMIT_WITH_VALUE:
            i += 1
        elif a.startswith("--") or a == "-":
            pass
        elif a.startswith("-"):
            for letter in a[1:]:                       # bundled short options: -am "text"
                if letter in COMMIT_SHORT_REQUIRED:
                    if letter == a[-1]:
                        i += 1                         # the value is the next word
                    break                              # or attached: the rest of the word
                if letter in COMMIT_SHORT_ATTACHED:
                    break                              # a key, when there is one, is attached
                if letter == "a":
                    plan["all"] = True
        else:
            plan["paths"].append(a)
        i += 1
    return plan


def add_plan(inv: dict) -> dict | None:
    """What a `git add` stages: `sweeping` for -A/-u/--all, else its pathspecs. None when the
    command is not an add, or a dry run."""
    if inv["sub"] != "add":
        return None
    plan: dict = {"sweeping": False, "paths": []}
    args = inv["args"]
    i, rest = 0, False
    while i < len(args):
        a = args[i]
        if rest:
            plan["paths"].append(a)
        elif a == "--":
            rest = True
        elif a in ("-n", "--dry-run"):
            return None
        elif a in ADD_SWEEPING:
            plan["sweeping"] = True
        elif a in ADD_WITH_VALUE:
            i += 1
        elif a.startswith("--") or a == "-":
            pass
        elif a.startswith("-"):
            if "n" in a[1:]:
                return None
            if "A" in a[1:] or "u" in a[1:]:
                plan["sweeping"] = True
        else:
            plan["paths"].append(a)
        i += 1
    return plan


# --- what a commit would record ------------------------------------------------------------------

Row = tuple[str, int, str]          # (path, line, text)
Finding = tuple[str, str, int]      # (why, path, line)


class Git:
    """Run git in one folder, with the global options the command line gave it."""

    def __init__(self, cwd: str, globals_: list[str]):
        self.cwd = cwd
        self.globals = list(globals_)
        self._top: str | None = None
        self._base: str | None = None

    def run(self, *args: str) -> bytes | None:
        """git's output, or None when it failed, took too long or could not start."""
        exe = shutil.which("git") or "/usr/bin/git"
        try:
            p = subprocess.run([exe, "--no-pager", *self.globals, "-c", "core.quotePath=false", *args],
                               cwd=self.cwd, capture_output=True, timeout=GIT_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return p.stdout[:DIFF_LIMIT] if p.returncode == 0 else None

    def toplevel(self) -> str | None:
        if self._top is None:
            out = self.run("rev-parse", "--show-toplevel")
            self._top = out.decode(errors="replace").strip() if out else ""
        return self._top or None

    def base(self) -> str:
        """HEAD, or the empty tree when the repository has no commit yet, so that a first commit's
        content is diffed like any other."""
        if self._base is None:
            if self.run("rev-parse", "--verify", "--quiet", "HEAD") is not None:
                self._base = "HEAD"
            else:
                out = self.run("hash-object", "-t", "tree", os.devnull)
                self._base = out.decode(errors="replace").strip() if out else "HEAD"
        return self._base

    def names(self, *args: str) -> list[str]:
        """The paths a diff touches."""
        out = self.run("diff", "--name-only", "-z", *args)
        return [p for p in out.decode(errors="replace").split("\0") if p] if out else []


def added_lines(diff: bytes) -> list[Row]:
    """(path, line, text) for every line a unified diff without context adds. A `+++` row names
    the file only between `diff --git` and the first hunk: inside a hunk, a row that looks like
    one is a changed line of the file and is given out like any other."""
    out = []
    path, line, in_header = None, 0, False
    for row in diff.decode(errors="replace").splitlines():
        if row.startswith("diff --git "):
            in_header, path = True, None
        elif in_header and row.startswith("+++ "):
            path = row[6:] if row.startswith("+++ b/") else None
        elif row.startswith("@@"):
            in_header = False
            m = re.search(r"\+(\d+)", row)
            line = int(m.group(1)) if m else 0
        elif row.startswith("+") and path and not in_header:
            out.append((path, line, row[1:]))
            line += 1
    return out


def diff_rows(g: Git, *args: str) -> list[Row]:
    """The lines a diff adds."""
    out = g.run("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "-U0",
                "--src-prefix=a/", "--dst-prefix=b/", *args)
    return added_lines(out) if out else []


def untracked_rows(g: Git, specs: list[str]) -> list[Row]:
    """The lines of the untracked files a `git add` of these pathspecs would stage, read from the
    working tree."""
    top = g.toplevel()
    listed = g.run("ls-files", "--others", "--exclude-standard", "--full-name", "-z", "--", *specs)
    if not top or not listed:
        return []
    out: list[Row] = []
    for rel in listed.decode(errors="replace").split("\0")[:MAX_FILES]:
        if not rel:
            continue
        try:
            with open(os.path.join(top, rel), "rb") as fh:
                text = fh.read(READ_LIMIT).decode(errors="replace")
        except OSError:
            continue
        out += [(rel, n, row) for n, row in enumerate(text.splitlines(), 1)]
    return out


def commit_lines(command: str, cwd: str) -> list[Row]:
    """Every line a `git commit` on this command line would record: the staged changes, the tracked
    changes for -a or after a `git add` on the same line, the untracked files such an add names,
    and the paths the commit itself names. Sorted by file and line, each line once."""
    return recorded(command, cwd)[0]


def recorded(command: str, cwd: str) -> tuple[list[Row], list[str]]:
    """What a `git commit` on this command line would record: its lines (as commit_lines) and the
    paths of the files, which a commit records even when they add no line."""
    invs = git_invocations(command, cwd)
    commits = [(inv, commit_plan(inv)) for inv in invs if inv["sub"] == "commit"]
    adds = [(inv, add_plan(inv)) for inv in invs if inv["sub"] == "add"]
    rows: set[Row] = set()
    paths: set[str] = set()
    for inv, plan in commits:
        if plan is None:
            continue
        g = Git(inv["dir"], inv["globals"])
        rows.update(diff_rows(g, "--cached"))
        paths.update(g.names("--cached"))
        if plan["all"]:
            rows.update(diff_rows(g, g.base()))
        if plan["paths"]:
            rows.update(diff_rows(g, g.base(), "--", *plan["paths"]))
        for ainv, aplan in adds:
            if aplan is None:
                continue
            ga = Git(ainv["dir"], ainv["globals"])
            same = ga.toplevel() == g.toplevel() if g.toplevel() and ga.toplevel() else True
            specs = [":/"] if aplan["sweeping"] else aplan["paths"]
            if not same or not specs:
                continue
            rows.update(diff_rows(ga, ga.base(), "--", *specs))
            rows.update(untracked_rows(ga, specs))
    paths.update(p for p, _, _ in rows)
    return sorted(rows), sorted(paths)


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


#: programs that run a command given to them as a string
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "eval", "xargs", "ssh"}


def hidden_commit(command: str) -> bool:
    """Does the line run a `git commit` its words do not show: it could not be read as words, git
    stands behind a wrapper program (timeout, sudo, nice, ...), or a shell runs it from a string."""
    cmds = simple_commands(command)
    if not cmds:
        m = GIT_PUBLISHING.search(command)
        return bool(m and m.group(1) == "commit")
    for words in cmds:
        words = unwrap(words)
        if not words:
            continue
        prog = os.path.basename(words[0])
        rest = words[1:]
        if prog != "git" and any(os.path.basename(w) == "git" for w in rest) and "commit" in rest:
            return True
        if prog in SHELLS and any(GIT_PUBLISHING.search(w) and "commit" in w for w in rest):
            return True
    return False


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
        return None
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
