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
  zero, so a file that defines a name twice on purpose still settles. The same holds for a key a
  dict display (`{...}`) repeats, set files included: two branches that each changed or added the
  same entry would both be kept, and Python keeps only the last.
- anything else, a block that changes a line both sides had, exits 1 so the caller aborts the
  rebase and a person resolves it.

It also reads a branch before any rebase, for the one doubling a union cannot be stopped from making:

    claude-ops union <worktree> --replay-risk FILE [--base REF]    # exit 1 when a rebase can double a line

A union keeps both sides of a block, so when a later commit of a branch rewrites or removes a line one
of its own earlier commits added, the replay keeps both versions and the branch arrives with that line
twice. A merge of the two heads is clean, because a merge compares end states and replays nothing, so
no check made on the heads can see this. Squashing the file's commits answers it, since one commit
cannot hold two versions of a line.

It needs three-way markers (`git -c merge.conflictStyle=diff3 rebase`): without the base section a
block cannot be told apart from one that changed existing lines, so such a block is refused.

    claude-ops union <worktree> [--ordered FILE] [--journal FILE] [--set FILE]   # in a stopped rebase; stages what it settles
    claude-ops union <conflicted file> [...]                                     # that one file only

A folder below the worktree's top counts as the worktree. A file that holds no conflict, or a path
no git worktree holds, is refused with exit 2 and one line that says why.
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
#: One commit per record in a `git log` read, so a subject that holds a newline cannot split a row.
RECORD = "\x1e"


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


def keys_repeated(text: str) -> Counter | None:
    """Pure. For each constant key that some dict display (`{...}`) in a Python file holds more than once,
    how many extra times, summed over the file; None when it does not parse."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    seen: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = Counter(repr(k.value) for k in node.keys if isinstance(k, ast.Constant))
            seen.update({k: n - 1 for k, n in keys.items() if n > 1})
    return seen


def repeated_keys(result: str, ours: str | None, theirs: str | None) -> list[str]:
    """Pure. The keys a dict display in the union repeats more often than either side does: each
    side's entry for one key kept, of which Python keeps only the last. A result that does not parse
    is refused as a whole."""
    got = keys_repeated(result)
    if got is None:
        raise Refused("the kept result is not valid Python")
    sides = [keys_repeated(s) or Counter() for s in (ours, theirs) if s is not None]
    return sorted(k for k, n in got.items() if all(n > s[k] for s in sides))


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


def conflicted(wt: Path, env=None) -> list[str]:
    """The files git lists as conflicted in `wt`, relative to its top folder."""
    return subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
                          capture_output=True, text=True, check=True, env=env).stdout.split()


#: ⭐ WHAT A UNION KEEPS THAT NO MERGE SHOWS. A merge compares the two end states, so it cannot see a
#: line a branch rewrote between two of its own commits. A rebase replays those commits one after
#: another, and in a file that unions, the earlier commit's version of the line and the later one's
#: are both kept, so the branch arrives with the line twice. `git merge-tree` of the same two heads is
#: clean, and the branch's own end-to-end diff can read as a pure addition, so a check on either is
#: blind to this whole class.


def commits_touching(wt: Path, base: str, path: str, head: str = "HEAD", env=None) -> list[dict]:
    """The commits of `base..head` that change `path`, oldest first. Each row holds its `sha`, its
    `subject`, and the `added` and `removed` lines of that file, without the leading + or -. Merge
    commits are left out, because a rebase replays the commits under them, and for the same reason
    the read asks for the full history: git would otherwise leave out a commit whose change a merge
    did not keep, which a rebase replays all the same.
    ⛔ A GIT RUN THAT FAILED IS NOT AN EMPTY HISTORY. A range naming a revision the repository does
    not hold, such as a remote-tracking branch in a clone that has none, left git with nothing on
    standard output, and this read answered that no commit changes the file, which a caller takes
    as "nothing to worry about". A refused read now raises Refused with git's own first line, so a
    question that cannot be answered is never answered reassuringly."""
    r = subprocess.run(["git", "-C", str(wt), "log", "--reverse", "--no-merges", "--full-history",
                        "-p", "--unified=0", f"--format={RECORD}%H%x1f%s", f"{base}..{head}",
                        "--", path],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        said = (r.stderr or r.stdout).strip().splitlines()
        raise Refused(f"git could not read {base}..{head} in {wt}. "
                      f"{said[0] if said else f'git exited {r.returncode}'}")
    rows: list[dict] = []
    for chunk in r.stdout.split(RECORD):
        head_line, _, body = chunk.partition("\n")
        sha, _, subject = head_line.partition("\x1f")
        if not sha.strip():
            continue
        added, removed = [], []
        for line in body.splitlines():
            if line[:3] in ("+++", "---"):
                continue
            if line.startswith("+"):
                added.append(line[1:])
            elif line.startswith("-"):
                removed.append(line[1:])
        rows.append({"sha": sha.strip(), "subject": subject, "added": added, "removed": removed})
    return rows


def replay_doubles(wt: Path, base: str, path: str, head: str = "HEAD", env=None) -> list[dict]:
    """Every line of `path` that a rebase of `base..head` can leave twice, as rows of `line`,
    `added_by`, `removed_by` and `subject`. A line is in it when one commit of the range removes
    what an earlier commit of the same range added, which is also the shape of a rewritten line.
    A branch whose commits only add lines to the file cannot double one of them, and a line that
    came with the base is a different question, which a three-way read of the two heads answers.
    Blank lines are left out."""
    added_by: dict[str, str] = {}
    out: list[dict] = []
    for c in commits_touching(wt, base, path, head, env):
        for line in c["removed"]:
            if line.strip() and line in added_by and added_by[line] != c["sha"]:
                out.append({"line": line, "added_by": added_by[line], "removed_by": c["sha"],
                            "subject": c["subject"]})
        for line in c["added"]:
            added_by.setdefault(line, c["sha"])
    return out


def report_replay_risk(wt: Path, base: str, path: str, head: str = "HEAD", env=None) -> int:
    """Print what `base..head` does to `path` and whether a rebase can double a line of it. 1 when
    it can, 0 when it cannot, and 2 when the range cannot be read at all.
    ⭐ 0 MEANS SAFE AND MUST ONLY MEAN SAFE, which is why a read git refuses is its own answer. A
    caller that gates on this treats 2 as "decide some other way" and 0 as "go ahead", so telling
    it 0 for a range that was never read is the one answer that cannot be recovered from."""
    where = f"{base}..{head}"
    try:
        rows = commits_touching(wt, base, path, head, env)
    except Refused as e:
        print(f"claude-ops union: {e}", file=sys.stderr)
        return 2
    if not rows:
        print(f"{path}: no commit of {where} changes it")
        return 0
    print(f"{path}: {len(rows)} commit(s) of {where} change it")
    for r in rows:
        print(f"  {r['sha'][:9]} +{len(r['added'])} -{len(r['removed'])} {r['subject']}")
    try:
        doubles = replay_doubles(wt, base, path, head, env)
    except Refused as e:                        # the second read of the same range, for the same reason
        print(f"claude-ops union: {e}", file=sys.stderr)
        return 2
    for d in doubles:
        print(f"a rebase can leave a line of {path} twice. {d['removed_by'][:9]} removes a line "
              f"{d['added_by'][:9]} added, and a union keeps both versions")
        print(f"  the line: {d['line'][:120]}")
    if doubles:
        print(f"squash this branch's commits on {path} into one, so its final text arrives once. "
              f"A later commit that only removes the extra line is undone by the same union and "
              f"comes out of the rebase empty")
        return 1
    if len(rows) > 1:
        print("each of them only adds lines, so none of these can double one. Squashing them all "
              "the same is the cheaper rule, because one commit cannot hold two versions of a line")
    return 0


def top_folder(path: Path, env=None) -> Path | None:
    """The top folder of the git worktree that holds `path` (a folder, or a file's folder), or None
    when no worktree holds it."""
    folder = path if path.is_dir() else path.parent
    if not folder.is_dir():
        return None
    r = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"], capture_output=True,
                       text=True, env=env)
    top = r.stdout.strip()
    return Path(top).resolve() if r.returncode == 0 and top else None


def settle_tree(wt: Path, *, ordered: tuple = (), journals: tuple = (), sets: tuple = (),
                env=None, only: str | None = None) -> int:
    """Settle every conflicted file in a stopped rebase in `wt` and stage it. 0 when every one
    settled, 1 when any is refused. Each file is settled on its own: one that settles is written and
    staged, one that is refused is left exactly as git left it (markers and all, not staged) and named
    with its reason, so whoever finishes the rebase settles only those. `env` is passed to every git
    command. `only`, a path relative to the top folder, settles that one file and leaves the others."""
    files = [f for f in conflicted(wt, env) if only is None or f == only]
    settled: dict[str, str] = {}
    refused: list[str] = []
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
                    keys = repeated_keys(settled[f], ours, theirs)
                    if keys:
                        raise Refused(f"the merged set holds the key {', '.join(keys)} twice in one dict")
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
                    keys = repeated_keys(settled[f], _stage(wt, 2, f, env), _stage(wt, 3, f, env))
                    if keys:
                        raise Refused(f"keeping both sides holds the key {', '.join(keys)} twice in one dict")
        except Refused as e:
            settled.pop(f, None)
            refused.append(f)
            print(f"{f}: refused, left as git left it: {e}", file=sys.stderr)
    for f, text in settled.items():
        (wt / f).write_text(text)
        if MARKER_LINE.search((wt / f).read_text()):    # read back: what is staged is what is on disk
            print(f"{f}: refused, conflict markers left after writing it", file=sys.stderr)
            refused.append(f)
            continue
        subprocess.run(["git", "-C", str(wt), "add", "--", f], check=True, env=env)
        print(f"kept both sides in {f}")
    if refused:
        print(f"settle by hand: {' '.join(refused)} (each still holds its conflict markers)", file=sys.stderr)
    return 1 if refused else 0


USAGE = ("claude-ops union [<worktree> | <conflicted file>] [--ordered FILE] [--journal FILE] [--set FILE]\n"
         "       claude-ops union [<worktree>] --replay-risk FILE [--base REF] [--head REF]")


def main(argv: list[str] | None = None) -> int:
    """Settle a stopped rebase. The argument is a folder of the worktree (every conflicted file is
    settled) or one conflicted file (only that one is). Anything else is refused with exit 2 and a
    line that says why, never a traceback."""
    ap = argparse.ArgumentParser(prog="claude-ops union", description=__doc__.split("\n")[0], usage=USAGE)
    ap.add_argument("worktree", nargs="?", default=".", type=Path,
                    help="a folder of the worktree, or one conflicted file in it (default: .)")
    ap.add_argument("--ordered", action="append", default=[], metavar="FILE",
                    help="a file that lists each entry once (repeatable)")
    ap.add_argument("--journal", action="append", default=[], metavar="FILE",
                    help="an append-only file (repeatable)")
    ap.add_argument("--set", action="append", default=[], metavar="FILE", dest="sets",
                    help="a ledger whose entry lines are an unordered set (repeatable)")
    ap.add_argument("--replay-risk", metavar="FILE",
                    help="read the branch instead of settling anything, and exit 1 when a rebase can "
                         "leave a line of FILE twice")
    ap.add_argument("--base", default="origin/main", metavar="REF",
                    help="the branch's base, for --replay-risk (default origin/main)")
    ap.add_argument("--head", default="HEAD", metavar="REF",
                    help="the branch's head, for --replay-risk (default HEAD), so a branch can be "
                         "read without checking it out")
    a = ap.parse_args(argv)
    path: Path = a.worktree
    top = top_folder(path) if path.exists() else None
    if top is None:
        print(f"claude-ops union: {path} is not in a git worktree. Usage: {USAGE}", file=sys.stderr)
        return 2
    if a.replay_risk:
        return report_replay_risk(top, a.base, a.replay_risk, a.head)
    only = None
    if not path.is_dir():
        only = path.resolve().relative_to(top).as_posix()
        if only not in conflicted(top):
            print(f"claude-ops union: {path} holds no conflict git knows of. Give the worktree to settle every "
                  f"conflicted file, or one file that `git diff --name-only --diff-filter=U` lists.", file=sys.stderr)
            return 2
    return settle_tree(top, ordered=tuple(a.ordered), journals=tuple(a.journal), sets=tuple(a.sets), only=only)


if __name__ == "__main__":
    raise SystemExit(main())
