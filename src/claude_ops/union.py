"""union — settle a rebase conflict where both sides only added lines, by keeping both.

When several branches each add an entry at the same place (a case in a test, a line at the end of a
list), a rebase conflicts although neither changed what the other wrote. This settles exactly those
blocks and refuses everything else, so a script that ships branches one after another can carry on
without a person, and a real conflict still stops it:

- a block whose common base is EMPTY (both sides only inserted lines) keeps both sides, the upstream
  side first. Run the tests after the rebase: a union that does not work fails there.
- in a file named with `--ordered` (a list where order matters and each entry appears once) the
  same, without repeats, the upstream entries first. A comment line (`#`) or a blank line is not an
  entry, so two entries that carry the same note both keep it.
- in a file named with `--set` (a ledger whose entry lines are an unordered set), the result keeps
  every line either side added and drops every line either side removed, wherever they are, so
  an entry added beside one another branch removed still merges. A Python file must still parse.
- in a file named with `--journal` (an append-only log), a block where the commit only appended after
  its base keeps the upstream side and adds the new lines. That is the shape left when a branch is
  moved off another branch it was stacked on: the base holds the lower branch's entry, which the
  upstream does not have and this branch must not bring. A block where the upstream only added lines
  before or after a base line the commit changed keeps the upstream lines and the commit's version of
  that line. Only journals: in code, dropping the base could drop lines silently.
- a JSON file is merged by its keys rather than its lines, from git's three stages of it: a key one
  side added or changed and the other left alone is taken, so two branches that each add an entry to
  the same object both land. Both sides changing one value differently, a list both sides changed,
  or a file not in the layout json.dumps writes (a merge would rewrite lines nobody changed) is
  refused.
- in a Python file, a union that defines a top-level name twice (def, class, or NAME =) where
  neither side did is refused: the later copy would silently win. Each side is the measure, not
  zero, so a file that defines a name twice on purpose still settles.
- anything else, a block that changes a line both sides had, exits 1 so the caller aborts the
  rebase and a person resolves it.

It needs three-way markers (`git -c merge.conflictStyle=diff3 rebase`): without the base section a
block cannot be told apart from one that changed existing lines, so such a block is refused.

    claude-ops union <worktree> [--ordered FILE] [--journal FILE] [--set FILE]   # in a stopped rebase; stages what it settles
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

#: A marker counts only at the start of a line: a document that quotes `||||||| ` mid-sentence must
#: not be read as a base marker.
BLOCK = re.compile(r"^<<<<<<< [^\n]*\n(.*?)(?:^\|\|\|\|\|\|\|[^\n]*\n(.*?))?^=======\n(.*?)^>>>>>>> [^\n]*\n",
                   re.S | re.M)
#: a line a conflict leaves behind. `=======` alone is left out: markdown underlines a heading with
#: it. The base marker counts too: a hand resolution that deleted only the other three leaves it.
MARKER_LINE = re.compile(r"^(<<<<<<< |\|\|\|\|\|\|\| |>>>>>>> )", re.M)


class Refused(Exception):
    pass


def settle(path: str, text: str, *, ordered: tuple = (), journals: tuple = ()) -> str:
    """The file's text with every conflict block settled, or Refused. `ordered` names files that
    list each entry once (a list both sides add to keeps the upstream entries first, without repeats);
    `journals` names append-only files (see the module's description)."""
    def block(m: re.Match) -> str:
        ours, base, theirs = m.group(1), m.group(2), m.group(3)
        if base is None:
            raise Refused("no base section: rebase with merge.conflictStyle=diff3")
        if base.strip():
            if path in journals and theirs.startswith(base):
                parts = [x for x in (ours.strip("\n"), theirs[len(base):].strip("\n")) if x]
                return "\n\n".join(parts) + "\n" if parts else ""
            # the upstream side only added lines around a base line the commit changed (a branch that
            # edited its own entry's title after the upstream added entries next to it): the
            # upstream side with the base replaced by the commit's. Exactly one of "ends with" and
            # "starts with": both, or neither, is not that shape, and is refused.
            if path in journals and ours != base and ours.endswith(base) != ours.startswith(base):
                if ours.endswith(base):
                    return ours[:len(ours) - len(base)] + theirs
                return theirs + ours[len(base):]
            raise Refused("both sides changed lines that were already there")
        if path in ordered:               # an entry both sides list is listed once
            have = ours.splitlines()
            # a comment or a blank line is not an entry: two entries can carry the same note
            return "".join(f"{ln}\n" for ln in have + [t for t in theirs.splitlines()
                                                      if t not in have or not t.strip()
                                                      or t.lstrip().startswith("#")])
        return ours + theirs
    out = BLOCK.sub(block, text)
    if MARKER_LINE.search(out):
        raise Refused("conflict markers left")
    return out


def top_names(text: str) -> Counter | None:
    """Pure. How often each top-level name is defined in a Python file (def, async def, class, and
    a plain or annotated assignment to a name), or None when it does not parse."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    seen: Counter = Counter()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen[node.name] += 1
        elif isinstance(node, ast.Assign):
            seen.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            seen[node.target.id] += 1
    return seen


def defined_twice(result: str, ours: str | None, theirs: str | None) -> list[str]:
    """Pure. The top-level names the union defines more than once although neither side did. A
    result that does not parse is refused as a whole."""
    got = top_names(result)
    if got is None:
        raise Refused("the kept result is not valid Python")
    sides = [top_names(s) or Counter() for s in (ours, theirs) if s is not None]
    return sorted(n for n, k in got.items() if k > 1 and all(s[n] < 2 for s in sides))


_GONE = object()          # a key one side does not have


def merge_json(base, ours, theirs):
    """Pure. Three versions of one JSON value merged: what only one side changed is taken, the
    upstream side's keys keep their order and the commit's new keys follow. Refused when both sides
    changed one value differently, or a list both sides changed."""
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        for k in list(ours) + [k for k in theirs if k not in ours]:
            v = merge_json(b.get(k, _GONE), ours.get(k, _GONE), theirs.get(k, _GONE))
            if v is not _GONE:
                out[k] = v
        return out
    raise Refused("both sides changed one value differently")


def settle_json(base: str | None, ours: str, theirs: str) -> str:
    """Pure. A JSON file's text from git's three stages of it (base None when both sides added the
    file), merged by keys and written back in the layout it had."""
    try:
        b = json.loads(base) if base is not None else {}
        o, t = json.loads(ours), json.loads(theirs)
    except ValueError:
        raise Refused("not JSON on every side")
    tail = "\n" if ours.endswith("\n") else ""
    if json.dumps(o, indent=2, ensure_ascii=False) + tail != ours:
        raise Refused("not in the layout json.dumps writes, so a merge would rewrite lines nobody changed")
    return json.dumps(merge_json(b, o, t), indent=2, ensure_ascii=False) + tail


def settle_set(base: str | None, ours: str, theirs: str) -> str:
    """A three-way merge of a file whose lines are a set (`--set`): `ours` with every line `theirs`
    removed from `base` taken out, and every line `theirs` added put in after the line that comes
    before it in `theirs`, after the lines `ours` added there (upstream first, as everywhere in union).
    Lines both sides added appear once. Refused when there is no base."""
    if base is None:
        raise Refused("no common version to compare with")
    b, o, t = (x.splitlines(keepends=True) for x in (base, ours, theirs))
    removed = Counter(b) - Counter(t)
    added_by_us = Counter(o) - Counter(b)
    added = Counter(t) - Counter(b) - added_by_us
    out = list(o)
    ours_new = set(added_by_us)
    for line, n in removed.items():
        for _ in range(n):
            if line in out:
                out.remove(line)
    for i, line in enumerate(t):
        if added[line] <= 0:
            continue
        added[line] -= 1
        before = next((t[j] for j in range(i - 1, -1, -1) if t[j] in out), None)
        at = out.index(before) + 1 if before is not None else 0
        while at < len(out) and out[at] in ours_new:      # upstream's own new lines stay first
            at += 1
        out.insert(at, line)
    return "".join(out)


def _stage(wt: Path, n: int, f: str, env=None) -> str | None:
    """Git's stage n of a conflicted file (1 the base, 2 ours, 3 theirs), or None when it has none."""
    r = subprocess.run(["git", "-C", str(wt), "show", f":{n}:{f}"], capture_output=True, text=True,
                       env=env)
    return r.stdout if r.returncode == 0 else None


def settle_tree(wt: Path, *, ordered: tuple = (), journals: tuple = (), sets: tuple = (),
                env=None) -> int:
    """Settle every conflicted file in a stopped rebase in `wt` and stage it. 0 when every one
    settled, 1 (nothing written) when any is refused. `env` is passed to every git command."""
    files = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
                           capture_output=True, text=True, check=True, env=env).stdout.split()
    settled = {}
    for f in files:
        try:
            if f in sets:
                ours, theirs = _stage(wt, 2, f, env), _stage(wt, 3, f, env)
                if ours is None or theirs is None:
                    raise Refused("one side deleted it")
                settled[f] = settle_set(_stage(wt, 1, f, env), ours, theirs)
                if f.endswith(".py"):
                    try:
                        ast.parse(settled[f])
                    except SyntaxError as e:
                        raise Refused(f"the merged set does not parse ({e.msg}, line {e.lineno})") from None
            elif f.endswith(".json"):
                ours, theirs = _stage(wt, 2, f, env), _stage(wt, 3, f, env)
                if ours is None or theirs is None:
                    raise Refused("one side deleted it")
                settled[f] = settle_json(_stage(wt, 1, f, env), ours, theirs)
            else:
                settled[f] = settle(f, (wt / f).read_text(), ordered=ordered, journals=journals)
                if f.endswith(".py"):
                    twice = defined_twice(settled[f], _stage(wt, 2, f, env), _stage(wt, 3, f, env))
                    if twice:
                        raise Refused(f"keeping both sides defines {', '.join(twice)} twice")
        except Refused as e:
            print(f"{f}: {e}", file=sys.stderr)
            return 1
    for f, text in settled.items():          # nothing is written unless every file settles
        (wt / f).write_text(text)
        if MARKER_LINE.search((wt / f).read_text()):    # read back: what is staged is what is on disk
            print(f"{f}: conflict markers left after writing it", file=sys.stderr)
            return 1
        subprocess.run(["git", "-C", str(wt), "add", "--", f], check=True, env=env)
        print(f"kept both sides in {f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops union", description=__doc__.split("\n")[0])
    ap.add_argument("worktree", nargs="?", default=".", type=Path)
    ap.add_argument("--ordered", action="append", default=[], metavar="FILE",
                    help="a file that lists each entry once (repeatable)")
    ap.add_argument("--journal", action="append", default=[], metavar="FILE",
                    help="an append-only file (repeatable)")
    ap.add_argument("--set", action="append", default=[], metavar="FILE", dest="sets",
                    help="a ledger whose entry lines are an unordered set (repeatable)")
    a = ap.parse_args(argv)
    return settle_tree(a.worktree, ordered=tuple(a.ordered), journals=tuple(a.journal), sets=tuple(a.sets))


if __name__ == "__main__":
    raise SystemExit(main())
