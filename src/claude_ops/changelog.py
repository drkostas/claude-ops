"""What changed between two commits of a repository, as the pull requests merged in between.

A deploy moves a checkout from one commit to another, and the person who runs it wants to know what
arrived: the titles of the pull requests, not a list of hashes. `merged` walks the first-parent
history from the old commit to the new one (the commits the main branch itself took, not the
commits inside each pull request) and reads each one the way the host wrote it:

- a merge commit, "Merge pull request #12 from owner/branch", whose body's first line is the pull
  request's title
- a squash merge, "Title of the pull request (#12)"
- anything else (a commit pushed straight to the branch, a merge of another branch) by its subject,
  with no number

Each change also carries its commit time. For the host's merge commit that is when the pull request
was merged, so a deploy can say how long each change waited between its merge and the deploy.

    claude-ops changelog v1.2.0 HEAD                 # one line per change, oldest first
    claude-ops changelog 4f1c2e0 origin/main --repo ~/src/app --json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

#: GitHub's merge commit subject
MERGE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
#: a squash merge's subject ends with the pull request's number
SQUASH = re.compile(r"^(.*\S)\s+\(#(\d+)\)$")
#: between fields and between records of the git log format below
FIELD, RECORD = "\x1f", "\x1e"
#: the commit time git prints for %cI (strict ISO 8601, with its offset)
ISO_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|[+-]\d\d:\d\d)$")


@dataclass
class Change:
    sha: str
    title: str
    #: the pull request's number, None for a commit that did not come through one
    number: int | None = None
    #: the branch a merge commit names, when it names one
    branch: str | None = None
    #: the commit's committer time, ISO 8601 with its offset (for the host's merge commit, when the
    #: pull request was merged), None when the log did not carry it
    committed: str | None = None


def parse(log: str) -> list[Change]:
    """Changes from `git log --format=%H%x1f%s%x1f%b%x1f%cI%x1e` output, in the order given. The
    commit time may be left out (`%H%x1f%s%x1f%b%x1e`), and then each change's `committed` is None."""
    out: list[Change] = []
    for rec in log.split(RECORD):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split(FIELD)
        committed = parts[-1].strip() if len(parts) >= 4 and ISO_TIME.match(parts[-1].strip()) else None
        if committed:
            parts = parts[:2] + [FIELD.join(parts[2:-1])]
        sha, subject, body = (parts + ["", ""])[:3]
        subject = subject.strip()
        m = MERGE.match(subject)
        if m:
            first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
            out.append(Change(sha.strip(), first or m.group(2), int(m.group(1)), m.group(2), committed))
            continue
        m = SQUASH.match(subject)
        if m:
            out.append(Change(sha.strip(), m.group(1), int(m.group(2)), committed=committed))
            continue
        out.append(Change(sha.strip(), subject, committed=committed))
    return out


def merged(old: str, new: str, repo: str = ".", env: dict | None = None) -> list[Change]:
    """The changes the first-parent history took after `old` up to `new`, oldest first.

    Raises ValueError when git cannot read the range (an unknown commit, not a repository)."""
    p = subprocess.run(
        ["git", "-C", repo, "log", "--first-parent", f"--format=%H{FIELD}%s{FIELD}%b{FIELD}%cI{RECORD}",
         f"{old}..{new}"],
        capture_output=True, text=True, env=env, check=False)
    if p.returncode != 0:
        raise ValueError(p.stderr.strip() or f"git log {old}..{new} exit {p.returncode}")
    return list(reversed(parse(p.stdout)))


def web_url(remote: str) -> str | None:
    """The https address of a GitHub repository from its remote URL (https or ssh), else None."""
    m = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$",
                 remote.strip())
    return f"https://github.com/{m.group(1)}" if m else None


def line(c: Change) -> str:
    """One change as a person reads it: "#12 Title", or the title alone."""
    return f"#{c.number} {c.title}" if c.number is not None else c.title


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops changelog", description=(
        "The pull requests merged between two commits, oldest first."))
    ap.add_argument("old", help="the commit the change starts after")
    ap.add_argument("new", help="the commit it ends at")
    ap.add_argument("--repo", default=".", help="the repository (default: here)")
    ap.add_argument("--json", action="store_true", help="a JSON list of changes")
    a = ap.parse_args(argv)
    try:
        changes = merged(a.old, a.new, a.repo)
    except ValueError as e:
        print(f"claude-ops changelog: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps([asdict(c) for c in changes]))
    else:
        for c in changes:
            print(line(c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
