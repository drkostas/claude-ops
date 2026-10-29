"""pr_changes — what the open pull requests of a repository change, by file and function.

When several sessions build in one repository, two of them can build the same thing: each reads
the code as it is on the main branch, and neither sees the other's open pull request until the
second one conflicts at the merge. This reads every open pull request (`gh pr list`) and, for each,
the files and the functions its branch changes against its base branch (a `git diff`, then the
file's own definitions: Python by its syntax tree, SQL by its CREATE statements, shell by its
function openers, TypeScript and JavaScript by their top-level functions), and matches that against
a text (a brief, a work item, a thing about to be claimed), so a session about to build is told
which open pull request already changes the same function, by number.

- `changes(repo, git_dir)` lists the open pull requests as {number, branch, base, title, files:
  {path: [function, ...]}}. A branch is read as `origin/<branch>` in `git_dir` (so fetch first), a
  function appears when a changed line falls inside its definition, and a change outside any
  definition leaves the file with an empty list. The answer is cached in a JSON file for ten
  minutes, because the readers of it run often.
- `names_in(text)` finds the file paths and the function names a text names: a path with a
  directory and a code extension, a word followed by `(`, a word in backticks, the last part of a
  dotted name, or a word with an underscore. Short and everyday words are left out.
- `overlaps(changes, text)` lists {number, title, path, function} where an open pull request
  changes a function the text names, or a code file it names by path.
- `warnings(text, repo=..., git_dir=...)` are those as lines, such as "open pull request #12
  (A title) already changes spawn_helpers in src/pkg/dispatch.py".

    claude-ops pr-changes --repo owner/name [--git-dir DIR] [--text "..."] [--json]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

#: how long a listing is kept before the open pull requests are read again
CACHE_S = 600
#: the cache file, unless one is given
CACHE = Path(os.environ.get("CLAUDE_OPS_PR_CHANGES_CACHE") or "/tmp/claude-ops-pr-changes.json")
#: how long gh and git may take, each
TIMEOUT_S = 60
#: the extensions of files whose functions are read, and whose paths count when a text names them
CODE = (".py", ".sql", ".sh", ".bash", ".ts", ".tsx", ".js", ".mjs")
#: words a text uses that are not the name of something a pull request changes
STOP = frozenset("main check print ground setup start stop close write read open test tests self none "
                 "true false return import from def class with else elif while break pass raise yield "
                 "list dict tuple range enumerate sorted print str int float bool bytes super type "
                 "args kwargs path file line text name value conn cursor execute fetchall fetchone "
                 "commit rollback connect run call exists append extend insert update delete select "
                 "where order limit join left right inner group having count sum min max avg".split())
#: the shortest word that can be a function name in a text
MIN_NAME = 4

Range = tuple[int, int]


# --- what a pull request changes --------------------------------------------------------------

def open_prs(repo: str | None, *, run=subprocess.run) -> list[dict]:
    """The open pull requests, [{number, branch, base, title}], from gh. `repo` is owner/name, or
    None for the repository of the working folder."""
    argv = ["gh", "pr", "list", "--state", "open", "--limit", "200",
            "--json", "number,headRefName,baseRefName,title"]
    if repo:
        argv += ["--repo", repo]
    try:
        p = run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    try:
        rows = json.loads(p.stdout or "[]")
    except ValueError:
        return []
    return [{"number": int(r["number"]), "branch": r.get("headRefName") or "",
             "base": r.get("baseRefName") or "", "title": r.get("title") or ""}
            for r in rows if isinstance(r, dict) and "number" in r]


def diff_ranges(diff: str) -> dict[str, list[Range]]:
    """{path: [(first line, last line), ...]} of the lines a unified diff changes in each file, on
    the new side. A hunk that only deletes is the one line where the deletion sits, so the
    definition around it still counts as changed."""
    out: dict[str, list[Range]] = {}
    path = None
    for row in diff.splitlines():
        if row.startswith("+++ "):
            path = row[6:] if row.startswith("+++ b/") else None
            if path is not None:
                out.setdefault(path, [])
        elif row.startswith("@@") and path is not None:
            m = re.search(r"\+(\d+)(?:,(\d+))?", row)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                out[path].append((start, start + max(count, 1) - 1))
    return out


def git(git_dir: str, *args: str, run=subprocess.run) -> str | None:
    """git's output in `git_dir`, or None when it failed."""
    try:
        p = run(["git", "-C", git_dir, *args], capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def definitions(path: str, text: str) -> list[tuple[str, int, int]]:
    """[(name, first line, last line)] of the definitions in a file: Python functions, classes and
    methods (a method by its own name), SQL CREATE statements (the object's name without its
    schema), shell functions, and TypeScript or JavaScript top-level functions and consts."""
    ext = os.path.splitext(path)[1].lower()
    lines = text.splitlines()
    if ext == ".py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return []
        out = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append((node.name, node.lineno, node.end_lineno or node.lineno))
                if isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            out.append((sub.name, sub.lineno, sub.end_lineno or sub.lineno))
        return out
    if ext == ".sql":
        starts = [(m.group(1).split(".")[-1].strip('"'), n)
                  for n, row in enumerate(lines, 1)
                  for m in [re.match(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?"
                                     r"(?:FUNCTION|VIEW|TABLE|TRIGGER|INDEX|TYPE|PROCEDURE)\s+"
                                     r"(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", row, re.I)] if m]
        return _blocks(starts, lines, re.compile(r"^(CREATE|INSERT|GRANT|ALTER|DROP|UPDATE|DELETE|COMMENT"
                                                  r"|COMMIT|BEGIN|SELECT|WITH|REVOKE|DO)\b", re.I))
    if ext in (".sh", ".bash"):
        starts = [(m.group(1), n) for n, row in enumerate(lines, 1)
                  for m in [re.match(r"(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{", row)] if m]
        out = []
        for name, start in starts:
            end = next((n for n in range(start + 1, len(lines) + 1) if lines[n - 1].startswith("}")), len(lines))
            out.append((name, start, end))
        return out
    if ext in (".ts", ".tsx", ".js", ".mjs"):
        head = re.compile(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s+([A-Za-z_]\w*)"
                          r"|(?:const|let|var)\s+([A-Za-z_]\w*)\s*=|class\s+([A-Za-z_]\w*))")
        starts = []
        for n, row in enumerate(lines, 1):
            m = head.match(row)
            if m:
                starts.append((next(g for g in m.groups() if g), n))
        return _blocks(starts, lines, re.compile(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?"
                                                  r"(?:function\s+|(?:const|let|var)\s+\w+\s*=|class\s+)"))
    return []


def _blocks(starts: list[tuple[str, int]], lines: list[str], opener: re.Pattern) -> list[tuple[str, int, int]]:
    """Each start runs to the line before the next top-level opener, or to the end of the file."""
    out = []
    for name, start in starts:
        end = len(lines)
        for n in range(start + 1, len(lines) + 1):
            if opener.match(lines[n - 1]):
                end = n - 1
                break
        out.append((name, start, end))
    return out


def functions_in(defs: list[tuple[str, int, int]], ranges: list[Range]) -> list[str]:
    """The names whose definition holds a changed line, in the file's order, each once."""
    out: list[str] = []
    for name, start, end in defs:
        if any(a <= end and b >= start for a, b in ranges) and name not in out:
            out.append(name)
    return out


def pr_files(git_dir: str, base: str, branch: str, *, run=subprocess.run, ref_prefix: str = "origin/") -> dict[str, list[str]]:
    """{path: [function, ...]} of what `branch` changes over `base`, read in `git_dir`. {} when git
    cannot read the branch (not fetched yet, or deleted)."""
    head, root = f"{ref_prefix}{branch}", f"{ref_prefix}{base}"
    diff = git(git_dir, "diff", "--no-color", "-U0", "--no-ext-diff", f"{root}...{head}", run=run)
    if diff is None:
        return {}
    out: dict[str, list[str]] = {}
    for path, ranges in diff_ranges(diff).items():
        if not path.lower().endswith(CODE):
            out[path] = []
            continue
        text = git(git_dir, "show", f"{head}:{path}", run=run)
        out[path] = functions_in(definitions(path, text), ranges) if text is not None else []
    return out


def changes(repo: str | None, git_dir: str = ".", *, run=subprocess.run, prs: list[dict] | None = None,
            cache: Path | None = CACHE, max_age: float = CACHE_S, now=time.time,
            ref_prefix: str = "origin/") -> list[dict]:
    """The open pull requests with what each changes: [{number, branch, base, title, files}]. From
    the cache while it is younger than `max_age` seconds and for the same repository; `prs` stands
    in for gh (a listing already in hand, or a test's)."""
    key = repo or os.path.abspath(git_dir)
    if cache is not None and prs is None:
        try:
            kept = json.loads(cache.read_text())
            if kept.get("repo") == key and now() - float(kept.get("at", 0)) < max_age:
                return kept["changes"]
        except (OSError, ValueError, TypeError, KeyError):
            pass
    rows = open_prs(repo, run=run) if prs is None else prs
    out = [{**pr, "files": pr_files(git_dir, pr.get("base") or "main", pr["branch"], run=run, ref_prefix=ref_prefix)}
           for pr in rows if pr.get("branch")]
    if cache is not None and prs is None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"repo": key, "at": now(), "changes": out}))
        except OSError:
            pass
    return out


# --- what a text names ----------------------------------------------------------------------------

PATH_IN_TEXT = re.compile(r"(?<![\w/])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:py|sql|sh|bash|ts|tsx|js|mjs))\b")
DOTTED_HEAD = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\.[A-Za-z_]")
CALLED = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
BACKTICKED = re.compile(r"`([A-Za-z_]\w*)`")
DOTTED = re.compile(r"\b[A-Za-z_]\w*\.([A-Za-z_]\w*)\b")
UNDERSCORED = re.compile(r"\b([A-Za-z]\w*_\w+)\b")


def names_in(text: str) -> dict[str, set[str]]:
    """{"files": paths, "functions": names} a text names (see the module's description)."""
    files = {m.group(1) for m in PATH_IN_TEXT.finditer(text or "")}
    functions: set[str] = set()
    for pat in (CALLED, BACKTICKED, DOTTED, DOTTED_HEAD, UNDERSCORED):
        for m in pat.finditer(text or ""):
            name = m.group(1)
            if len(name) >= MIN_NAME and name.lower() not in STOP and not name.isdigit():
                functions.add(name)
    return {"files": files, "functions": functions}


def overlaps(listing: list[dict], text: str, *, exclude=()) -> list[dict]:
    """[{number, title, path, function}] where an open pull request changes a function the text
    names, or a code file it names by path (function ""). `exclude` are numbers or branches to
    leave out, such as the session's own pull request."""
    named = names_in(text)
    out: list[dict] = []
    skip = {str(e) for e in exclude}
    for pr in listing:
        if str(pr.get("number")) in skip or (pr.get("branch") or "") in skip:
            continue
        for path, fns in (pr.get("files") or {}).items():
            if path in named["files"] or any(path.endswith("/" + f) or ("/" not in f and path.rsplit("/", 1)[-1] == f)
                                             for f in named["files"]):
                out.append({"number": pr["number"], "title": pr.get("title", ""), "path": path, "function": ""})
            for fn in fns:
                if fn in named["functions"]:
                    out.append({"number": pr["number"], "title": pr.get("title", ""), "path": path, "function": fn})
    seen: set[tuple] = set()
    kept = []
    for o in sorted(out, key=lambda o: (o["number"], o["path"], o["function"])):
        k = (o["number"], o["path"], o["function"])
        if k not in seen:
            seen.add(k)
            kept.append(o)
    return kept


def warnings(text: str, *, repo: str | None = None, git_dir: str = ".", exclude=(), listing: list[dict] | None = None,
             **kw) -> list[str]:
    """One line per overlap: "open pull request #12 (A title) already changes spawn_helpers in
    src/pkg/dispatch.py". `listing` stands in for `changes` (a test's, or one already in hand)."""
    rows = changes(repo, git_dir, **kw) if listing is None else listing
    out = []
    for o in overlaps(rows, text, exclude=exclude):
        what = f"{o['function']} in {o['path']}" if o["function"] else o["path"]
        title = f" ({o['title']})" if o["title"] else ""
        out.append(f"open pull request #{o['number']}{title} already changes {what}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops pr-changes", description=__doc__.split("\n")[0])
    ap.add_argument("--repo", help="owner/name; the working folder's repository when absent")
    ap.add_argument("--git-dir", default=".", help="a checkout whose origin/<branch> refs are fetched")
    ap.add_argument("--text", help="a brief, a work item or a thing to claim: print what open pull requests already change in it")
    ap.add_argument("--exclude", action="append", default=[], metavar="PR", help="a number or branch to leave out (repeatable)")
    ap.add_argument("--prs-file", metavar="FILE", help="a JSON listing of pull requests in place of gh")
    ap.add_argument("--no-cache", action="store_true", help="read the pull requests again")
    ap.add_argument("--ref-prefix", default="origin/", help="where the branches are read (default origin/)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    prs = json.loads(Path(a.prs_file).read_text()) if a.prs_file else None
    rows = changes(a.repo, a.git_dir, prs=prs, cache=None if a.no_cache or prs is not None else CACHE,
                   ref_prefix=a.ref_prefix)
    if a.text:
        found = overlaps(rows, a.text, exclude=a.exclude)
        if a.json:
            print(json.dumps(found, indent=1))
        else:
            for line in warnings(a.text, exclude=a.exclude, listing=rows):
                print(line)
        return 1 if found else 0
    if a.json:
        print(json.dumps(rows, indent=1))
    else:
        for pr in rows:
            print(f"#{pr['number']} {pr['branch']}: {pr['title']}")
            for path, fns in pr["files"].items():
                print(f"   {path}" + (f": {', '.join(fns)}" if fns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
