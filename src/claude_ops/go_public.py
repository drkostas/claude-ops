"""Make a repository public with a check for private terms already in place.

A project whose private side and public packages share authors needs every public repository to
refuse the private side's names: in its files, its commit messages and each pull request. The check
is a workflow that reads a list of terms from a repository secret, so the list itself is never public.
Adding that workflow is easy to forget when a repository goes public, and once it is public, whatever
is in it has been readable all along. `go` makes it one step, stopping at the first thing that fails:

  1. scan every tracked file and every commit message of the default branch for the terms, and refuse
     to go on if any matches (where it matched is reported, never the matched text). A repository
     that is already public is only reported on, and the check still goes in;
  2. set the secrets that hold the terms and the allowed text, piped on stdin, never printed;
  3. copy the workflow and its script from a reference repository through a pull request that must
     pass that check, merged with --merge;
  4. check that the files are on the default branch and the secrets exist;
  5. make the repository public (skipped with check_only, or when it already is);
  6. call `after(repo)`, for whatever the caller keeps about repository visibility.

`refresh` sets the secrets again on every public repository that runs the check, after the lists
change: the secrets are copies.

Every function that runs a command takes `run` (default `sh`, which runs a command in its own
process group and kills the whole group on a timeout, so a `git clone` that `gh` started does not
outlive it). `sh` takes `env`.
"""
from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

#: the check's files, in every repository that runs it, and in the reference copy
FILES = (".github/workflows/private-terms.yml", ".github/scripts/check_terms.py")
#: the secrets the workflow reads: the terms, then the allowed text, one regular expression a line
SECRETS = ("PRIVATE_TERMS", "PRIVATE_ALLOW")
#: the workflow's name, as `gh pr checks` reports it
WORKFLOW = "Private terms"
BRANCH = "ci/private-terms"


class Refused(Exception):
    """A step that must not be passed: the message says which and why."""


def sh(args: list[str], *, stdin: str | None = None, cwd: str | None = None, check: bool = True,
       timeout: float = 600, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """`args` in its own process group, captured as text; the whole group is killed on a timeout
    (TimeoutExpired is raised after). A failure raises Refused when `check`."""
    proc = subprocess.Popen(args, stdin=subprocess.PIPE if stdin is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
                            env=env, start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    r = subprocess.CompletedProcess(args, proc.returncode, out, err)
    if check and r.returncode != 0:
        raise Refused(f"{' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def matches(text: str, terms: list[re.Pattern], allow: list[re.Pattern]) -> list[int]:
    """The 1-based numbers of the terms found in `text`, after the allowed text is removed."""
    for a in allow:
        text = a.sub(" ", text)
    return [i for i, t in enumerate(terms, 1) if t.search(text)]


def scan(folder: str, terms: list[str], allow: list[str], run=sh) -> list[str]:
    """Where a term appears in the checkout at `folder`: every tracked file and every commit
    message, case-insensitively. Each entry names the place and the term's number, never the text."""
    tp = [re.compile(t, re.I) for t in terms]
    ap = [re.compile(a, re.I) for a in allow]
    hits: list[str] = []
    for name in run(["git", "ls-files", "-z"], cwd=folder).stdout.split("\0"):
        if not name:
            continue
        try:
            text = (Path(folder) / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                       # a picture or a font says nothing a reader reads
        for n, line in enumerate(text.splitlines(), 1):
            found = matches(line, tp, ap)
            if found:
                hits.append(f"{name}:{n} term {','.join(map(str, found))}")
    log = run(["git", "log", "--format=%H%x00%B%x01"], cwd=folder).stdout
    for entry in log.split("\x01"):
        sha, _, body = entry.strip("\n").partition("\0")
        found = matches(body, tp, ap)
        if sha and found:
            hits.append(f"commit {sha[:10]} term {','.join(map(str, found))}")
    return hits


def api_file(repo: str, path: str, ref: str | None = None, run=sh) -> str | None:
    """A file's text on GitHub, or None when it is not there."""
    q = f"repos/{repo}/contents/{path}" + (f"?ref={ref}" if ref else "")
    r = run(["gh", "api", q], check=False)
    if r.returncode != 0:
        if "404" in (r.stderr + r.stdout) or "Not Found" in (r.stderr + r.stdout):
            return None
        raise Refused(f"could not read {path} in {repo}: {(r.stderr or r.stdout).strip()[:200]}")
    return base64.b64decode(json.loads(r.stdout)["content"]).decode()


def has_secret(repo: str, name: str, run=sh) -> bool:
    return run(["gh", "api", f"repos/{repo}/actions/secrets/{name}"], check=False).returncode == 0


def go(repo: str, *, terms: list[str], allow: list[str], reference: str, check_only: bool = False,
       files: tuple[str, ...] = FILES, secrets: tuple[str, str] = SECRETS, workflow: str = WORKFLOW,
       after=None, run=sh, say=print) -> dict:
    """The whole sequence for `repo` (owner/name), with the check copied from `reference`. Returns
    {"repo", "public", "files_added", "hits"}; raises Refused at the first step that must stop it."""
    info = json.loads(run(["gh", "repo", "view", repo, "--json",
                           "visibility,defaultBranchRef,nameWithOwner"]).stdout)
    branch = info["defaultBranchRef"]["name"]
    if not terms:
        raise Refused("no terms were given; nothing could be checked")
    public = info["visibility"].upper() == "PUBLIC"

    try:
        with tempfile.TemporaryDirectory(prefix="go-public-") as tmp:
            run(["gh", "repo", "clone", repo, tmp, "--", "--quiet"])
            hits = scan(tmp, terms, allow, run)
    except subprocess.TimeoutExpired:
        # a repository too large to clone in the time allowed has been read by nobody: refused
        # while private, and only said while public (the check still goes in)
        if not public:
            raise Refused(f"{repo} took longer than ten minutes to clone, so nothing has read it "
                          "for private terms; scan it some other way before it goes public")
        say(f"1. {repo} is already public and too large to clone in ten minutes: not scanned, "
            "the check goes in anyway")
        hits = []
    if hits and not public:
        raise Refused(f"{len(hits)} place(s) carry a private term; rewrite them for an outside "
                      "reader first:\n  " + "\n  ".join(hits[:50]))
    if hits:
        say(f"1. {repo} is already public, and {len(hits)} place(s) in it carry a private term "
            "(said here, not blocking: the check stops new ones):\n  " + "\n  ".join(hits[:50]))
    else:
        say(f"1. scanned {repo}: no private terms in its files or commit messages")

    for name, value in zip(secrets, ("\n".join(terms), "\n".join(allow))):
        run(["gh", "secret", "set", name, "-R", repo], stdin=value)
    say(f"2. set {', '.join(secrets)}")

    missing = [p for p in files if api_file(repo, p, branch, run) is None]
    if missing:
        add_files(repo, branch, reference, files=files, workflow=workflow, run=run)
        say(f"3. added {', '.join(files)} through a merged pull request")
    else:
        say("3. the workflow and its script were already there")

    gone = [p for p in files if api_file(repo, p, branch, run) is None]
    no_secret = [n for n in secrets if not has_secret(repo, n, run)]
    if gone or no_secret:
        raise Refused(f"after setting up, still missing: {', '.join(gone + no_secret)}")
    say(f"4. checked: the files are on {branch} and the secrets exist")

    if not public and not check_only:
        run(["gh", "repo", "edit", repo, "--visibility", "public",
             "--accept-visibility-change-consequences"])
        public = True
        say(f"5. {repo} is public")
    else:
        say(f"5. left {'public' if public else 'private'}, as it was")
    if after is not None:
        after(repo)
    return {"repo": repo, "public": public, "files_added": bool(missing), "hits": hits}


def add_files(repo: str, branch: str, reference: str, *, files: tuple[str, ...] = FILES,
              workflow: str = WORKFLOW, run=sh, wait=time.sleep) -> None:
    """Copy `files` from `reference` onto a new branch of `repo`, open a pull request, wait for
    `workflow`'s checks on it and merge it with --merge. Only that workflow counts: a repository's own
    older CI may be long broken, and that is not what this pull request changes."""
    head = run(["gh", "api", f"repos/{repo}/git/ref/heads/{branch}", "--jq", ".object.sha"]).stdout.strip()
    run(["gh", "api", "-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{BRANCH}",
         "-f", f"sha={head}"])
    for path in files:
        text = api_file(reference, path, None, run)
        if text is None:
            raise Refused(f"the reference copy {reference}:{path} is gone; nothing to copy")
        run(["gh", "api", "-X", "PUT", f"repos/{repo}/contents/{path}",
             "-f", "message=ci: check each pull request for terms that must not appear here",
             "-f", f"branch={BRANCH}",
             "-f", f"content={base64.b64encode(text.encode()).decode()}"])
    url = run(["gh", "pr", "create", "-R", repo, "--base", branch, "--head", BRANCH,
               "--title", "Check each pull request for terms that must not appear here",
               "--body", "Adds the private-terms workflow: it reads the pull request title, "
                         "description, commit messages and added lines, and fails on any term "
                         "from the PRIVATE_TERMS secret. The list is kept in the secret, not in "
                         "the repository, and the log never prints the matched text."]).stdout.strip()
    number = url.rstrip("/").rsplit("/", 1)[-1]
    # the check registers a few seconds after the push, so "none yet" is waiting, never a pass
    for _ in range(60):
        r = run(["gh", "pr", "checks", number, "-R", repo, "--json", "workflow,bucket"], check=False)
        try:
            ours = [c for c in json.loads(r.stdout or "[]") if c.get("workflow") == workflow]
        except ValueError:
            ours = []
        if any(c.get("bucket") in ("fail", "cancel") for c in ours):
            raise Refused(f"the private-terms check failed on its own pull request: {url}")
        if ours and all(c.get("bucket") == "pass" for c in ours):
            break
        wait(10)
    else:
        raise Refused(f"the private-terms check did not finish on {url} within ten minutes")
    run(["gh", "pr", "merge", number, "-R", repo, "--merge", "--delete-branch"])


def refresh(owner: str, *, terms_for, allow: list[str], secrets: tuple[str, str] = SECRETS,
            run=sh, say=print) -> list[str]:
    """Set the secrets again on every public, unarchived repository of `owner` that already has the
    first one, with `terms_for(repo)` and `allow`. Returns the repositories it set."""
    names = run(["gh", "repo", "list", owner, "--visibility", "public", "--limit", "500",
                 "--json", "nameWithOwner,isArchived",
                 "--jq", ".[] | select(.isArchived | not) | .nameWithOwner"]).stdout.split()
    joined = "\n".join(allow)
    done = []
    for repo in names:
        if not has_secret(repo, secrets[0], run):
            continue                   # not checked yet: `go` sets both when it adds the check
        for name, value in zip(secrets, ("\n".join(terms_for(repo)), joined)):
            run(["gh", "secret", "set", name, "-R", repo], stdin=value)
        done.append(repo)
    say(f"set {', '.join(secrets)} again on {len(done)} repositories")
    return done
