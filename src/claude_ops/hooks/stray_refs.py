"""Refuse a `git fetch` that would leave behind a ref nothing ever removes (a PreToolUse hook).

A fetch takes refspecs, and a refspec may name its own destination, so `git fetch origin
+refs/pull/7/head:refs/remotes/origin/pr-7` stores a ref for a branch the remote never had, and
`git fetch origin "+refs/pull/*/head:refs/remotes/mine/*"` stores one for every open pull request.
Nobody meets either again, because neither is a branch any command lists and the line that made
them was typed once.

⭐ WHAT A PRUNE REACHES IS THE WHOLE QUESTION, AND IT IS TWO ANSWERS, measured on real git rather
than reasoned about (the test beside this file keeps the measurement). A prune removes what a
remote's CONFIGURED refspec covers, which by default is `+refs/heads/*:refs/remotes/<name>/*`. So a
destination INSIDE that namespace, `refs/remotes/origin/pr-7`, is removed by `git fetch --prune
origin`, and until somebody runs one it reads exactly like a branch the remote still has, which is
the worse half in practice: a count of the remote's merged branches counts it, and the ancestry walk
behind that count pays for it. A destination anywhere ELSE, another `refs/remotes/` namespace or a
name invented at the top (`refs/mine/7`), is removed by nothing at all, neither `git fetch --prune`
nor `git remote prune`, in any repository, for ever. In the repository this was measured in, 4,761
refs had gathered that way, 2,245 of them from one wildcard fetch, and the merged listing of them
took 2.09 s against 0.084 s for the remote's own refs alone.

⛔ AND THE DISCRIMINATOR IS NOT WHERE THE REF LANDS, IT IS WHETHER A CONFIGURED REFSPEC COVERS IT,
source as well as destination. `git fetch origin +refs/heads/master:refs/remotes/origin/master` is a
documented idiom, the remote's own default refspec spelled out for one branch, and the ref it writes
is refreshed by every later plain fetch and protected by a prune. `+refs/pull/7/head:refs/remotes/origin/pr-7`
lands in the same namespace and is covered by nothing, so that ref is never refreshed and only looks
at home. Both are prunable, so prunability cannot tell them apart. This hook reads the repository's
own `remote.<name>.fetch` patterns and refuses only a refspec none of them maps, which is why the
answer it gives is about the repository in front of it rather than about the shape of a ref.

So this hook refuses a fetch whose refspec no configured refspec covers and whose destination is
under `refs/remotes/`, which only a remote's own refspec should write, or under any namespace with no
tool that lists it, and the refusal says which of the two it would be. A destination under `refs/heads/`, `refs/tags/`,
`refs/notes/` or `refs/replace/` goes through, because each of those has a command that shows it and
a person who can remove it, and so does a plain local branch name. A fetch with no destination goes
through, which is almost every fetch. The refusal names both ways through, since a refusal with no
way through is one people route around: fetch the source alone with `--no-write-fetch-head` and read
the shas from `git ls-remote`, or, to keep such refs, add them to the remote's own refspec, which a
prune then keeps clean.

    claude-ops hook stray-refs        # reads the tool call on stdin
    python3 -P -m claude_ops.hooks.stray_refs

It takes no configuration. On anything unexpected the call goes through, because a broken hook must
never stop work.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from . import commandline

#: the ref namespaces a person can list and remove with a command of their own (`git branch`,
#: `git tag`, `git notes`, `git replace`), so a ref put there is one somebody meets again
OWNED = ("heads", "tags", "notes", "replace")


def destination(spec: str) -> str | None:
    """The ref this refspec would write, or None when it names none.

    Only a destination that starts at `refs/` is read as one, which is what keeps a remote given as
    an address out of it: `git@host:owner/repo.git` and `https://host/repo.git` both hold a colon,
    and neither names a ref. A plain name (`master:master`) is a local branch, which `git branch`
    lists and a person can delete, so it is not read as one either."""
    _src, sep, dst = spec.rpartition(":")
    if not sep or not dst.startswith("refs/"):
        return None
    return dst


def owned(ref: str) -> bool:
    """This destination lies in a namespace with a tool that lists it."""
    parts = ref.split("/")
    return len(parts) > 2 and parts[1] in OWNED


def tracking_namespace(ref: str) -> str | None:
    """The name whose tracking namespace this destination lies in (`refs/remotes/<name>/...`), or
    None. A prune of a REMOTE by that name removes such a ref, and when no remote has that name
    nothing does, which is why the refusal asks the repository which remotes it has."""
    parts = ref.split("/")
    return parts[2] if len(parts) > 3 and parts[1] == "remotes" else None


def configured_remotes(folder: str) -> set[str] | None:
    """The remotes this repository has, or None when that cannot be read (no repository, no git).
    Unknown is never read as none, because "no remote owns it" is the harsher half of the refusal."""
    try:
        done = subprocess.run(["git", "-C", folder or ".", "remote"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


def configured_refspecs(folder: str) -> tuple[str, ...] | None:
    """Every `remote.<name>.fetch` pattern this repository configures, or None when they cannot be
    read (no repository, no git). A clone and `git remote add` both write one, so an empty answer is
    read as unknown too rather than as a repository that configures nothing."""
    try:
        done = subprocess.run(["git", "-C", folder or ".", "config", "--get-regexp",
                               r"^remote\..*\.fetch$"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    out = tuple(line.split(None, 1)[1].strip() for line in done.stdout.splitlines()
                if len(line.split(None, 1)) == 2)
    return out or None


def covered(spec: str, patterns: tuple[str, ...]) -> bool:
    """This refspec is one a configured refspec already maps, source and destination both, so a
    plain fetch refreshes the ref it writes and a prune protects it.

    A pattern holds at most one `*` on each side, and the piece the source's `*` stands for must be
    the piece the destination's `*` stands for, which is what makes
    `+refs/heads/master:refs/remotes/origin/master` covered by `+refs/heads/*:refs/remotes/origin/*`
    and `+refs/pull/7/head:refs/remotes/origin/pr-7` covered by neither."""
    src, _, dst = spec.lstrip("+").partition(":")
    for pattern in patterns:
        p_src, _, p_dst = pattern.lstrip("+").partition(":")
        if not p_dst:
            continue
        if "*" not in p_src or "*" not in p_dst:
            if (src, dst) == (p_src, p_dst):
                return True
            continue
        head, _, tail = p_src.partition("*")
        if not (src.startswith(head) and src.endswith(tail) and len(src) >= len(head) + len(tail)):
            continue
        middle = src[len(head):len(src) - len(tail)] if tail else src[len(head):]
        if dst == p_dst.replace("*", middle, 1):
            return True
    return False


def strays(command: str, cwd: str = ".") -> list[tuple[str, str, str]]:
    """(the refspec, the ref it writes, the folder the fetch runs in) for every fetch on this
    command line that would leave a ref behind, in the order they are written."""
    found: list[tuple[str, str, str]] = []
    for inv in commandline.git_invocations(command, cwd):
        if inv["sub"] != "fetch":
            continue
        patterns = None
        for spec in inv["args"]:
            if spec.startswith("-"):
                continue
            dst = destination(spec)
            if dst is None or owned(dst):
                continue
            if patterns is None:
                patterns = configured_refspecs(inv["dir"])
            # ⛔ unknown is not a stray: a repository whose patterns cannot be read may be
            # configuring this very refspec, and a hook that cannot tell must not refuse
            if patterns is not None and not covered(spec, patterns):
                found.append((spec, dst, inv["dir"]))
    return found


def fate(ref: str, remotes: set[str] | None) -> str:
    """What would ever remove this ref, which is the sentence the refusal turns on."""
    name = tracking_namespace(ref)
    if name is None:
        return ("nothing will ever remove it, because a prune only covers what a remote's "
                "configured refspec writes and no refspec writes outside the tracking namespaces")
    if remotes is None:
        return (f"only `git fetch --prune {name}` would remove it, and nothing would if {name} is "
                f"no remote of this repository, which could not be read here")
    if name in remotes:
        return (f"it goes inside {name}'s own namespace, where it reads as a branch {name} still "
                f"has to everything that lists them, and only `git fetch --prune {name}` removes "
                f"it, which nothing runs by itself")
    return (f"{name} is no remote of this repository, so no prune covers that namespace and "
            f"nothing will ever remove it")


def why(found: list[tuple[str, str, str]], remotes: set[str] | None | object = None) -> str:
    """The refusal, which names what the fetch would leave, what would remove it, and both ways
    through. `remotes` is read from the fetch's own folder unless it is given."""
    spec, dst, folder = found[0]
    more = f" (and {len(found) - 1} more on this line)" if len(found) > 1 else ""
    known = configured_remotes(folder) if remotes is None else remotes
    return (f"this fetch's refspec `{spec}` stores {dst}{more}, and {fate(dst, known)}. It is also "
            f"unlisted by `git branch` and unmet by anyone, since the line that made it is typed "
            f"once. For the objects, or for a sha, fetch the source alone with `git fetch "
            f"--no-write-fetch-head <remote> <ref>...`, which writes no ref and no FETCH_HEAD, and "
            f"read each sha from `git ls-remote <remote> <pattern>`; both take many refs in one "
            f"call, which is the answer where a wildcard would have been. To KEEP such refs "
            f"instead, add them to the remote's refspec (`git config --add remote.<name>.fetch "
            f"'+refs/pull/*/head:refs/remotes/<name>/pr/*'`), and a prune keeps that namespace "
            f"clean from then on.")


def check(payload: dict, argv: list[str] | None = None) -> str | None:
    """Why this tool call is refused, or None."""
    if payload.get("tool_name") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not command:
        return None
    here = str(payload.get("cwd") or os.getcwd())
    found = strays(command, here)
    return why(found) if found else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        payload = json.load(sys.stdin)
        refused = check(payload, args)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if refused:
        print(f"Blocked: {refused}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
