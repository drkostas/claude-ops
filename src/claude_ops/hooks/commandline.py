"""Read a shell command line the way the shell will run it, and what a `git commit` on it records.

Two hooks share this: `no_session_links` and `public_voice` both need the simple commands on a line
(split on `&&`, `||`, `;`, `|`, `&` and newlines, with line continuations joined, here-document
bodies set aside and redirections dropped), the folder each one runs in (`cd`, `pushd`, `popd`,
`git -C`), and, for a `git commit`, every line and file name it would record: the staged changes,
the tracked changes for `-a`, what a `git add` on the same line would stage (untracked files read
from the working tree, since the add has not run yet when a hook runs), and the paths the commit
names. `writes` gives the text a line writes into files through here-documents and `echo` or
`printf` redirected to a file.

Everything here fails soft: a line that cannot be read gives nothing, and a git call that fails,
or takes longer than GIT_TIMEOUT_S, gives nothing.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess

#: a shell assignment that may stand before a command (`VAR=value git ...`)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: a redirection operator, as the shell splitter hands it over (its target word is dropped too)
REDIRECT = re.compile(r"^(\d*[<>]{1,3}&?|&>>?|<&|>&|>\|)$")
#: the characters whose runs separate simple commands (a newline is one of them)
SEPARATORS = set(";&|\n")
#: a backslash before a newline is a line continuation, which the shell removes (an even run of
#: backslashes before the newline is not one)
CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")
#: a here-document: its body is data, never shell words, so it is set aside before splitting
HEREDOC = re.compile(r"(<<-?[ \t]*\\?(['\"]?)(\w+)\2[^\n]*\n)(?:.*?\n)?[ \t]*\3[ \t]*(?=\n|$)", re.S)
#: a here-document in parts: the operator, the delimiter, the rest of the line it starts on, the body
HEREDOC_PARTS = re.compile(r"(<<-?)[ \t]*\\?(['\"]?)(\w+)\2([^\n]*\n)(.*?\n)?[ \t]*\3[ \t]*(?=\n|$)", re.S)
#: the word that stands for a set-aside here-document body while the line is split
HEREDOC_MARK = re.compile(r"__heredoc_(\d+)__")
#: redirections whose target is a file the command writes (a duplication such as `2>&1` is not)
WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
#: targets that are not files in a folder
NOT_FILES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "-"}
#: `git ... commit|tag|notes` seen in the text, for a line that could not be read as words
GIT_PUBLISHING = re.compile(r"\bgit\b[^\n;&|]*\b(commit|tag|notes)\b")
#: words that may stand before `git` on a command line without changing what it does
WRAPPERS = {"command", "exec", "nohup", "time", "builtin", "{", "}",
            "if", "elif", "then", "else", "while", "until", "do", "!"}

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


# --- the command line -----------------------------------------------------------------------------

def parse(command: str) -> list[dict]:
    """The command line as simple commands (split on &&, ||, ;, |, & and newlines), each as
    {"words": [...], "writes": [files its output is redirected into], "heredoc": body or None}.
    Line continuations are joined, here-document bodies are kept apart from the words, and
    redirections and subshell parentheses are not words. An unfinished quote gives []."""
    command = CONTINUATION.sub(r"\1", command)
    bodies: list[str] = []

    def set_aside(m: re.Match) -> str:
        bodies.append(m.group(5) or "")
        return f"<< __heredoc_{len(bodies) - 1}__{m.group(4)}"

    command = HEREDOC_PARTS.sub(set_aside, command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return []

    def fresh() -> dict:
        return {"words": [], "writes": [], "heredoc": None}

    out = [fresh()]
    pending: str | None = None                # the redirection whose target word comes next
    for t in tokens:
        if pending is not None:
            mark = HEREDOC_MARK.fullmatch(t)
            if pending == "<<" and mark:
                out[-1]["heredoc"] = bodies[int(mark.group(1))]
            elif pending in WRITE_REDIRECTS:
                out[-1]["writes"].append(t)
            pending = None
            continue
        if t and set(t) <= SEPARATORS:
            if out[-1]["words"]:
                out.append(fresh())
        elif REDIRECT.match(t):
            words = out[-1]["words"]
            fd = ""
            if words and words[-1].isdigit() and len(words[-1]) <= 2:
                fd = words.pop()              # the descriptor of `2>&1` or `1>file`
            pending = t if fd in ("", "1") or t == "<<" else "fd"
        elif t in ("(", ")"):
            continue
        else:
            out[-1]["words"].append(t)
    return [c for c in out if c["words"]]


def simple_commands(command: str) -> list[list[str]]:
    """The command line as simple commands (split on &&, ||, ;, |, & and newlines), each a list of
    words with its redirections and subshell parentheses dropped. An unfinished quote gives []."""
    return [c["words"] for c in parse(command)]


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


def steps(command: str, cwd: str):
    """Each simple command on the line (as parse gives it, its words unwrapped) with the folder it
    runs in. `cd`, `pushd` and `popd` are followed and not given out."""
    here, stack = cwd, []
    for cmd in parse(command):
        words = unwrap(cmd["words"])
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
            yield dict(cmd, words=words), here


def walk(command: str, cwd: str):
    """Each simple command on the line, as its unwrapped words, with the folder it runs in."""
    for cmd, here in steps(command, cwd):
        yield cmd["words"], here


def writes(command: str, cwd: str) -> list[tuple[str, str]]:
    """(file, text) for the text the line writes into files, as absolute paths: a here-document
    given to a command whose output goes to a file or to `tee`, and the words of `echo` or `printf`
    redirected to a file. Text a program makes itself is not known here and is not given out."""
    out = []
    for cmd, here in steps(command, cwd):
        words = cmd["words"]
        prog = os.path.basename(words[0])
        targets = list(cmd["writes"])
        if prog == "tee":
            targets += [w for w in words[1:] if not w.startswith("-")]
        if cmd["heredoc"] is not None:
            text = cmd["heredoc"]
        elif prog in ("echo", "printf"):
            text = " ".join(words[1:])
        else:
            continue
        for t in targets:
            if t not in NOT_FILES:
                out.append((os.path.normpath(os.path.join(here, expand(t))), text))
    return out


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


def commit_lines(command: str, cwd: str, repo: str | None = None) -> list[Row]:
    """Every line a `git commit` on this command line would record: the staged changes, the tracked
    changes for -a or after a `git add` on the same line, the untracked files such an add names,
    and the paths the commit itself names. Sorted by file and line, each line once. With `repo` (a
    work tree's top folder, as commit_repos gives it), only the commits into that repository."""
    return recorded(command, cwd, repo)[0]


def commit_repos(command: str, cwd: str) -> list[str]:
    """The top folders of the repositories the line commits into (a dry run is not a commit)."""
    tops = set()
    for inv in git_invocations(command, cwd):
        if inv["sub"] == "commit" and commit_plan(inv) is not None:
            top = Git(inv["dir"], inv["globals"]).toplevel()
            if top:
                tops.add(top)
    return sorted(tops)


def recorded(command: str, cwd: str, repo: str | None = None) -> tuple[list[Row], list[str]]:
    """What a `git commit` on this command line would record: its lines (as commit_lines) and the
    paths of the files, which a commit records even when they add no line. With `repo`, only the
    commits into that repository."""
    invs = git_invocations(command, cwd)
    commits = [(inv, commit_plan(inv)) for inv in invs if inv["sub"] == "commit"]
    adds = [(inv, add_plan(inv)) for inv in invs if inv["sub"] == "add"]
    rows: set[Row] = set()
    paths: set[str] = set()
    for inv, plan in commits:
        if plan is None:
            continue
        g = Git(inv["dir"], inv["globals"])
        if repo is not None and g.toplevel() != repo:
            continue
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
