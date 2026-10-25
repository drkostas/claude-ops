"""restack — rebuild a stack of one-commit branches on a new base, settling add-only conflicts.

A stack of pull requests, each built on the one before and each one commit, falls behind when the
base moves or a lower branch changes. Restacking by hand means a rebase per branch and the same
add-only conflicts each time (an entry at the end of a list, a line in a changelog). `restack`
rebases each worktree's top commit onto the branch before it, the first onto the base, and settles
conflicts with `union` (both sides only added lines, or a JSON object, or a file named as ordered,
journal or set). It stops at the first conflict union refuses and says where; that rebase is left
stopped for a person to finish.

    claude-ops restack <base> <worktree>... [--upstream COMMIT] [--ordered FILE] [--journal FILE] [--set FILE]

Each worktree must have its branch checked out, and the branches are listed bottom first.
Nothing is pushed.

`--upstream` is for a bottom branch of several commits: the commit it sits on now, so every commit
after it moves onto the base. That is the case after the pull request below it merged with its
commits rewritten (a squash, or a merge queue that rebases): the old commits are not on the base, so
a merge or a plain rebase brings them back and conflicts with their rewritten copies. Moving only
what came after the old head leaves them behind.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from claude_ops import union


def _git(wt: Path, *args: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(wt), "-c", "merge.conflictStyle=diff3", *args],
                          capture_output=True, text=True, env=env)


def _rebasing(wt: Path, env) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        path = _git(wt, "rev-parse", "--git-path", name, env=env).stdout.strip()
        if path and (wt / path if not os.path.isabs(path) else Path(path)).exists():
            return True
    return False


def restack(base: str, worktrees: list[Path], *, upstream: str | None = None, ordered: tuple = (),
            journals: tuple = (), sets: tuple = (), env=None, say=print) -> int:
    """0 when every branch is rebuilt, 3 when a conflict needs a person (that rebase stays
    stopped), 1 for a worktree that is not on a branch or a rebase git could not start.
    `upstream` is the commit the bottom branch sits on; without it each branch moves its top commit."""
    run_env = dict(os.environ if env is None else env, GIT_EDITOR="true")
    for wt in worktrees:
        br = _git(wt, "branch", "--show-current", env=run_env).stdout.strip()
        if not br:
            say(f"STOP at {wt}: no branch checked out")
            return 1
        since = upstream if (upstream and wt == worktrees[0]) else "HEAD~1"
        r = _git(wt, "rebase", "-q", "--onto", base, since, env=run_env)
        # each commit that stops is settled and continued, so a branch of several commits moves whole
        while r.returncode != 0:
            conflicted = _git(wt, "diff", "--name-only", "--diff-filter=U", env=run_env).stdout.split()
            if not conflicted:
                if not _rebasing(wt, run_env):
                    say(f"STOP at {br}: the rebase did not start ({(r.stderr or r.stdout).strip()[:200]})")
                    return 1
                say(f"STOP at {br}: rebase --continue failed ({(r.stderr or r.stdout).strip()[:200]})")
                return 3
            if union.settle_tree(wt, ordered=ordered, journals=journals, sets=sets, env=run_env) != 0:
                say(f"STOP at {br}: {' '.join(conflicted)} needs a person (the rebase is left stopped)")
                return 3
            r = _git(wt, "rebase", "--continue", env=run_env)
        top = _git(wt, "log", "--oneline", "-1", env=run_env).stdout.strip()
        say(f"{br} {top[:70]}")
        base = br
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops restack", description=__doc__.split("\n")[0])
    ap.add_argument("base", help="the branch or commit the bottom branch goes onto")
    ap.add_argument("worktrees", nargs="+", type=Path, help="one worktree per branch, bottom first")
    ap.add_argument("--upstream", metavar="COMMIT",
                    help="the commit the bottom branch sits on now; every commit after it moves")
    ap.add_argument("--ordered", action="append", default=[], metavar="FILE")
    ap.add_argument("--journal", action="append", default=[], metavar="FILE")
    ap.add_argument("--set", action="append", default=[], metavar="FILE", dest="sets")
    a = ap.parse_args(argv)
    return restack(a.base, a.worktrees, upstream=a.upstream, ordered=tuple(a.ordered), journals=tuple(a.journal),
                   sets=tuple(a.sets), say=lambda s: print(s, flush=True))


if __name__ == "__main__":
    sys.exit(main())
