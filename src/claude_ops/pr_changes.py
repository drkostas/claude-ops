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
  {path: [function, ...]}}. A branch is read as `origin/<branch>` in `git_dir` (so fetch first, or
  pass `fetch=True` and the missing branches are fetched once), a function appears when a changed
  line falls inside its definition, and a change outside any definition leaves the file with an
  empty list. A pull request git could not read (its branch is not fetched, `git_dir` is not a
  working tree) comes back with `files` {} and `unread`, the reason, and is never mistaken for one
  that changes nothing; when gh could not list the pull requests at all, the reason goes into the
  `unread` list the caller passes. The answer is cached in a JSON file for ten minutes, per
  repository and per repository folder (the worktrees of one clone share an entry, and the
  entries of other repositories stay), because the readers of it run often. A cached pull request
  that was unread is read again on the next call, and nothing is cached when gh failed.
- `names_in(text)` finds the file paths and the function names a text names: a path with a
  directory and a code extension, a word followed by `(`, a word in backticks, the last part of a
  dotted name, or a word with an underscore. Short and everyday words are left out.
- `overlaps(changes, text)` lists {number, title, path, function} where an open pull request
  changes a function the text names, or a code file it names by path.
- `warnings(text, repo=..., git_dir=...)` are those as lines, such as "open pull request #12
  (A title) already changes spawn_helpers in src/pkg/dispatch.py", followed by `unread_lines`: one
  line per pull request that could not be read ("open pull request #14 (A title) could not be read:
  origin/fix/x is not in /path (git fetch origin fix/x)"), or one line for all of them when more
  than three are unread. Without those lines a reader that could not read anything would answer
  the same as one that found no overlap, and that silence is the failure this module exists to
  remove.

    claude-ops pr-changes --repo owner/name [--git-dir DIR] [--fetch] [--text "..."] [--json]

The command exits 1 when `--text` has overlaps, 2 when nothing (or not everything) could be read,
and 0 otherwise. With `--json` the unread lines go to standard error, so the output stays JSON.
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
    None for the repository of the working folder. [] when gh failed (`list_prs` says why)."""
    return list_prs(repo, run=run)[0]


def list_prs(repo: str | None, *, run=subprocess.run) -> tuple[list[dict], str | None]:
    """(`open_prs`, None), or ([], why) when gh could not list them: it is not there, it failed
    (no login, no network, no such repository) or answered something that is not a listing."""
    argv = ["gh", "pr", "list", "--state", "open", "--limit", "200",
            "--json", "number,headRefName,baseRefName,title"]
    if repo:
        argv += ["--repo", repo]
    try:
        p = run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"gh could not run: {e}"
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return [], f"gh exited {p.returncode}" + (f": {err[0]}" if err else "")
    try:
        rows = json.loads(p.stdout or "[]")
    except ValueError:
        return [], "gh answered something that is not a listing"
    if not isinstance(rows, list):
        return [], "gh answered something that is not a listing"
    return [{"number": int(r["number"]), "branch": r.get("headRefName") or "",
             "base": r.get("baseRefName") or "", "title": r.get("title") or ""}
            for r in rows if isinstance(r, dict) and "number" in r], None


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
    return _git(git_dir, *args, run=run)[0]


def _git(git_dir: str, *args: str, run=subprocess.run) -> tuple[str | None, str]:
    """(git's output, "") in `git_dir`, or (None, why) when it failed: git's first line of error, or
    what stopped it from running."""
    try:
        p = run(["git", "-C", git_dir, *args], capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"git could not run: {e}"
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return None, err[0] if err else f"git exited {p.returncode}"
    return p.stdout, ""


def folder_of(git_dir: str, *, run=subprocess.run) -> tuple[str | None, str | None, str]:
    """(the working tree's top folder, the repository's common .git folder, "") for `git_dir`, or
    (None, None, why) when it is not inside a git working tree: a folder that is no repository, or
    a .git folder itself, where a diff of two refs prints nothing and exits 0."""
    top, why = _git(git_dir, "rev-parse", "--show-toplevel", run=run)
    if top is None:
        return None, None, f"{os.path.abspath(git_dir)} is not a git working tree ({why})"
    top = top.strip()
    common, _ = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir", run=run)
    return top, (common or "").strip() or top, ""


def refs_in(git_dir: str, *, run=subprocess.run) -> set[str]:
    """The short names of every ref in `git_dir` ("origin/fix/x", "main"), so a listing checks its
    branches without one git call each."""
    out = git(git_dir, "for-each-ref", "--format=%(refname:short)", run=run) or ""
    return {row.strip() for row in out.splitlines() if row.strip()}


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


#: the words every unread line carries, for a reader that tells those lines from overlaps
UNREAD = "could not be read"


def read_files(git_dir: str, base: str, branch: str, *, run=subprocess.run, ref_prefix: str = "origin/",
               refs: set[str] | None = None) -> tuple[dict[str, list[str]], str | None]:
    """({path: [function, ...]}, None) of what `branch` changes over `base`, read in `git_dir`, or
    ({}, why) when git could not read it: the branch's ref is not there (not fetched yet, or
    deleted), the base's is not, or the diff failed. The why names the ref, the folder and, for a
    remote prefix, the fetch that brings it. `refs` (from `refs_in`) spares a git call per ref."""
    head, root = f"{ref_prefix}{branch}", f"{ref_prefix}{base}"
    for ref, name in ((head, branch), (root, base)):
        if refs is not None and ref in refs:
            continue
        if git(git_dir, "rev-parse", "--verify", "--quiet", ref, run=run) is None:
            remote = ref_prefix.rstrip("/")
            how = f" (git fetch {remote} {name})" if remote and ref_prefix.endswith("/") else ""
            return {}, f"{ref} is not in {git_dir}{how}"
    diff, why = _git(git_dir, "diff", "--no-color", "-U0", "--no-ext-diff", f"{root}...{head}", run=run)
    if diff is None:
        return {}, f"git diff {root}...{head} in {git_dir} failed ({why})"
    out: dict[str, list[str]] = {}
    for path, ranges in diff_ranges(diff).items():
        if not path.lower().endswith(CODE):
            out[path] = []
            continue
        text = git(git_dir, "show", f"{head}:{path}", run=run)
        out[path] = functions_in(definitions(path, text), ranges) if text is not None else []
    return out, None


def pr_files(git_dir: str, base: str, branch: str, *, run=subprocess.run, ref_prefix: str = "origin/") -> dict[str, list[str]]:
    """{path: [function, ...]} of what `branch` changes over `base`: `read_files` without its why,
    so {} both for a branch that changes nothing and for one git could not read."""
    return read_files(git_dir, base, branch, run=run, ref_prefix=ref_prefix)[0]


def changes(repo: str | None, git_dir: str = ".", *, run=subprocess.run, prs: list[dict] | None = None,
            cache: Path | None = CACHE, max_age: float = CACHE_S, now=time.time,
            ref_prefix: str = "origin/", fetch: bool = False, unread: list[str] | None = None) -> list[dict]:
    """The open pull requests with what each changes: [{number, branch, base, title, files}], and
    `unread` (the reason) on one git could not read, whose `files` is {}. From the cache while it is
    younger than `max_age` seconds, for the same repository read in the same repository folder; a
    cached pull request that was unread is read again. `fetch` fetches the branches that are not in
    `git_dir` once, before reading. `prs` stands in for gh (a listing already in hand, or a test's).
    When gh could not list the pull requests, the answer is [] and the reason is appended to
    `unread` (a list the caller passes), and nothing is cached."""
    top, common, why = folder_of(git_dir, run=run)
    if top is None:
        rows, failed = list_prs(repo, run=run) if prs is None else (prs, None)
        if failed and unread is not None:
            unread.append(f"the open pull requests {UNREAD}: {failed}")
        return [{**pr, "files": {}, "unread": why} for pr in rows if pr.get("branch")]
    key = f"{repo or common}@{common}"
    rows: list[dict] | None = None
    at = None
    if cache is not None and prs is None:
        entry = _cached(cache).get(key)
        if entry and now() - float(entry.get("at", 0)) < max_age and isinstance(entry.get("changes"), list):
            rows, at = entry["changes"], float(entry["at"])
            if not any(r.get("unread") for r in rows):
                return rows
    fresh = rows is None
    if fresh:
        listed, failed = list_prs(repo, run=run) if prs is None else (prs, None)
        if failed:
            if unread is not None:
                unread.append(f"the open pull requests {UNREAD}: {failed}")
            return []
        rows = [{**pr, "files": {}} for pr in listed if pr.get("branch")]
    todo = [r for r in rows if fresh or r.get("unread")]
    refs = refs_in(top, run=run)
    remote = ref_prefix.rstrip("/") if ref_prefix.endswith("/") else ""
    if fetch and remote:
        missing = [n for r in todo for n in (r["branch"], r.get("base") or "main") if f"{ref_prefix}{n}" not in refs]
        if missing:
            git(top, "fetch", "--quiet", remote, *dict.fromkeys(missing), run=run)
            refs = refs_in(top, run=run)
    for r in todo:
        files, failed = read_files(top, r.get("base") or "main", r["branch"], run=run, ref_prefix=ref_prefix, refs=refs)
        r["files"] = files
        if failed:
            r["unread"] = failed
        else:
            r.pop("unread", None)
    if cache is not None and prs is None:
        entries = {k: v for k, v in _cached(cache).items()
                   if k != key and isinstance(v, dict) and now() - float(v.get("at", 0) or 0) < max_age}
        entries[key] = {"at": now() if fresh else at, "changes": rows}
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"entries": entries}))
        except OSError:
            pass
    return rows


def _cached(cache: Path) -> dict:
    """The cache file's entries, {"<repo>@<common .git>": {at, changes}}; {} when there is no file
    or it is not one of ours (an older one held a single listing)."""
    try:
        kept = json.loads(cache.read_text())
    except (OSError, ValueError):
        return {}
    entries = kept.get("entries") if isinstance(kept, dict) else None
    return entries if isinstance(entries, dict) else {}


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


def unread_lines(listing: list[dict], problems=()) -> list[str]:
    """The `problems` (what `changes` appended to its `unread` list), then one line per pull request
    git could not read, or one line for all of them when more than three are unread, naming the
    first few and the first reason; [] when everything was read."""
    out = list(problems)
    rows = [r for r in listing if r.get("unread")]
    if len(rows) > 3:
        nums = ", ".join(f"#{r['number']}" for r in rows[:5]) + (", ..." if len(rows) > 5 else "")
        out.append(f"{len(rows)} of {len(listing)} open pull requests {UNREAD} ({nums}): {rows[0]['unread']}")
    else:
        for r in rows:
            title = f" ({r['title']})" if r.get("title") else ""
            out.append(f"open pull request #{r['number']}{title} {UNREAD}: {r['unread']}")
    return out


def warnings(text: str, *, repo: str | None = None, git_dir: str = ".", exclude=(), listing: list[dict] | None = None,
             with_unread: bool = True, **kw) -> list[str]:
    """One line per overlap: "open pull request #12 (A title) already changes spawn_helpers in
    src/pkg/dispatch.py", then `unread_lines` for what could not be read (`with_unread=False` leaves
    those out). `listing` stands in for `changes` (a test's, or one already in hand)."""
    problems: list[str] = []
    rows = changes(repo, git_dir, unread=problems, **kw) if listing is None else listing
    out = []
    for o in overlaps(rows, text, exclude=exclude):
        what = f"{o['function']} in {o['path']}" if o["function"] else o["path"]
        title = f" ({o['title']})" if o["title"] else ""
        out.append(f"open pull request #{o['number']}{title} already changes {what}")
    if with_unread:
        out += unread_lines(rows, problems)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops pr-changes", description=__doc__.split("\n")[0])
    ap.add_argument("--repo", help="owner/name; the working folder's repository when absent")
    ap.add_argument("--git-dir", default=".", help="a checkout whose origin/<branch> refs are fetched")
    ap.add_argument("--fetch", action="store_true", help="fetch the branches the checkout lacks first")
    ap.add_argument("--text", help="a brief, a work item or a thing to claim: print what open pull requests already change in it")
    ap.add_argument("--exclude", action="append", default=[], metavar="PR", help="a number or branch to leave out (repeatable)")
    ap.add_argument("--prs-file", metavar="FILE", help="a JSON listing of pull requests in place of gh")
    ap.add_argument("--no-cache", action="store_true", help="read the pull requests again")
    ap.add_argument("--ref-prefix", default="origin/", help="where the branches are read (default origin/)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    prs = json.loads(Path(a.prs_file).read_text()) if a.prs_file else None
    problems: list[str] = []
    rows = changes(a.repo, a.git_dir, prs=prs, cache=None if a.no_cache or prs is not None else CACHE,
                   ref_prefix=a.ref_prefix, fetch=a.fetch, unread=problems)
    unread = unread_lines(rows, problems)
    if a.text:
        found = overlaps(rows, a.text, exclude=a.exclude)
        if a.json:
            print(json.dumps(found, indent=1))
            for line in unread:
                print(line, file=sys.stderr)
        else:
            for line in warnings(a.text, exclude=a.exclude, listing=rows):
                print(line)
        return 1 if found else (2 if unread else 0)
    if a.json:
        print(json.dumps(rows, indent=1))
        for line in problems:
            print(line, file=sys.stderr)
    else:
        for line in problems:
            print(line)
        for pr in rows:
            print(f"#{pr['number']} {pr['branch']}: {pr['title']}")
            if pr.get("unread"):
                print(f"   {UNREAD}: {pr['unread']}")
            for path, fns in pr["files"].items():
                print(f"   {path}" + (f": {', '.join(fns)}" if fns else ""))
    return 2 if unread else 0


if __name__ == "__main__":
    sys.exit(main())
