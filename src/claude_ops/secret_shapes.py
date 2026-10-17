"""secret_shapes — refuse a push whose new commits add a line of a shape a secret scanner flagged before.

A secret scanner that checks pull requests (GitGuardian, for example) reads every commit in them, and
lines that hold no secret can still look like one to it: a key naming a passphrase with a variable for
its value, a selector whose value is "pass", a variable name in a list. Each such finding costs a
rewritten commit and another wait for the check. Once a shape has been flagged, this finds it before
the push instead.

The shapes are data, a JSON file the project keeps:

    {"shapes": [{"name": "...", "pattern": "<regex>", "detector": "...", "date": "YYYY-MM-DD",
                 "pr": 123, "fix": "what cleared it"}]}

`pattern` is a Python regular expression matched against each added line; keep each one the
narrowest that catches its flagged lines and none of the lines already merged. `pr` is optional.

It reads the added lines of EVERY commit, not the branch's sum, because the scanner does: a line one
commit adds and the next removes still raises the finding.

    claude-ops secret-shapes <worktree> [<base>] --shapes shapes.json   # the commits in <base>..HEAD
    claude-ops secret-shapes --pre-push --shapes shapes.json            # from a git pre-push hook

Exit 0 when nothing matches, 1 with each match (commit, file, line, shape) and its fix, 2 when the
shapes cannot be read.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ZERO = "0" * 40
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def load(path: Path) -> list[dict]:
    """The shapes, each with its pattern compiled as `rx`."""
    shapes = json.loads(path.read_text(encoding="utf-8"))["shapes"]
    return [{**s, "rx": re.compile(s["pattern"])} for s in shapes]


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """Pure. (file, line in the new file, text) of every line a unified diff adds."""
    out, path, n = [], None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None     # +++ /dev/null: a deletion
            continue
        m = HUNK.match(line)
        if m:
            n = int(m.group(1))
            continue
        if path is None or line.startswith(("--- ", "diff --git", "index ")):
            continue
        if line.startswith("+"):
            out.append((path, n, line[1:]))
            n += 1
        elif line.startswith(" "):
            n += 1
    return out


def matches(lines: list[tuple[str, int, str]], shapes: list[dict]) -> list[dict]:
    """Pure. Each added line a shape matches, once per line (the first shape that matches)."""
    found = []
    for path, n, text in lines:
        for s in shapes:
            if s["rx"].search(text):
                found.append({"file": path, "line": n, "text": text.strip(), "shape": s})
                break
    return found


def _git(wt: str, *args: str, env=None) -> str | None:
    r = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True, env=env)
    return r.stdout if r.returncode == 0 else None


def scan(wt: str, commits: list[str], shapes: list[dict], env=None) -> list[dict]:
    """Every match in the lines each commit adds. A commit that cannot be read is said, never passed."""
    found = []
    for sha in commits:
        diff = _git(wt, "show", "--format=", "-U0", "--no-color", "--no-ext-diff", sha, env=env)
        if diff is None:
            found.append({"commit": sha, "unreadable": True})
            continue
        found += [{**m, "commit": sha} for m in matches(added_lines(diff), shapes)]
    return found


def report(found: list[dict], where: str = "the shapes file") -> str:
    out = ["REFUSED: these added lines have the shape of a secret a scanner flagged before, and it",
           f"  reads every commit of a pull request ({where}):"]
    for f in found:
        if f.get("unreadable"):
            out.append(f"  {f['commit'][:8]} could not be read, so it was not checked")
            continue
        s = f["shape"]
        out.append(f"  {f['commit'][:8]} {f['file']}:{f['line']}  {s['name']} "
                   f"({s['detector']}, #{s['pr']} {s['date']})" if s.get("pr") else
                   f"  {f['commit'][:8]} {f['file']}:{f['line']}  {s['name']} ({s['detector']}, {s['date']})")
        out.append(f"      {f['text'][:150]}")
        out.append(f"      fix: {s['fix']}")
    out.append("  Rewrite the commit that added it (a later commit that removes it does not help).")
    return "\n".join(out)


def branch_commits(wt: str, base: str = "origin/HEAD", env=None) -> list[str] | None:
    out = _git(wt, "rev-list", "--reverse", f"{base}..HEAD", env=env)
    return None if out is None else out.split()


def pushed_commits(wt: str, stdin: str, env=None) -> list[str]:
    """The commits a push brings that no remote branch has yet, from the lines git hands a pre-push
    hook (<local ref> <local sha> <remote ref> <remote sha>). A deleted ref brings none."""
    commits: list[str] = []
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:
            continue
        for sha in (_git(wt, "rev-list", "--reverse", parts[1], "--not", "--remotes", env=env) or "").split():
            if sha not in commits:
                commits.append(sha)
    return commits


def main(argv: list[str] | None = None, stdin=sys.stdin) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops secret-shapes", description=__doc__.split("\n")[0])
    ap.add_argument("worktree", nargs="?", help="the worktree whose branch commits are read")
    ap.add_argument("base", nargs="?", default="origin/HEAD", help="the commits in <base>..HEAD")
    ap.add_argument("--shapes", required=True, type=Path, help="the JSON file of shapes")
    ap.add_argument("--pre-push", action="store_true", help="read the commits git hands a pre-push hook")
    a = ap.parse_args(argv)
    try:
        shapes = load(a.shapes)
    except (OSError, ValueError, KeyError, re.error) as e:
        print(f"REFUSED: cannot read the shapes in {a.shapes}: {e}", file=sys.stderr)
        return 2
    if a.pre_push:
        where = (_git(".", "rev-parse", "--show-toplevel") or ".").strip()
        commits = pushed_commits(where, stdin.read())
    else:
        if not a.worktree:
            ap.print_usage(sys.stderr)
            return 2
        where = a.worktree
        commits = branch_commits(where, a.base)
        if commits is None:
            print(f"REFUSED: cannot list the commits of {where} over {a.base}", file=sys.stderr)
            return 1
    found = scan(where, commits, shapes)
    if found:
        print(report(found, str(a.shapes)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
