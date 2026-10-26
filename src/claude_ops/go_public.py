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
     pass that check, merged with --merge. `before_checks(repo, public)` is called first, for
     whatever the check needs in order to run at all (a self-hosted runner, for a private repository
     whose hosted Actions minutes have run out);
  4. check that the files are on the default branch and the secrets exist, and that no self-hosted
     runner is registered to the repository: once it is public, a pull request from a fork can run
     its jobs on one, so the machine behind it would run a stranger's code. `before_public(repo,
     runners)` may remove them first; a runner still there, or a list GitHub would not give, stops
     it (an already public repository, or check_only, is only reported on);
  5. make the repository public (skipped with check_only, or when it already is);
  6. call `after(repo)`, for whatever the caller keeps about repository visibility.

Two steps are off unless asked for, so a caller that knows the sequence above sees no change:
`github=True` makes step 1 also read what people wrote on GitHub itself (`scan_github`), and
`private_reporting=True` turns on GitHub's private vulnerability reporting after step 5 when the
repository's SECURITY.md sends reports there (`points_to_reporting`).

`survey` is the scan alone: the files, the commit messages and the GitHub text, read and reported,
with nothing set, added or changed. Use it to see what a repository would carry into the open.

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


#: where GitHub looks for a security policy
SECURITY_PATHS = ("SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md")
#: a security policy that sends reports to GitHub's private reporting says one of these
REPORTING = re.compile(r"security advisor|private vulnerability report|/security/advisories", re.I)
#: what a person has to do about an edit revision, since the API cannot delete one
EDIT_STEP = ("an edit revision cannot be deleted through the API: delete it from the edit history "
             "on that page, signed in as someone who may")


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


def self_hosted_runners(repo: str, run=sh) -> list[str] | None:
    """The names of the self-hosted runners registered to `repo`, or None when GitHub did not list
    them (listing them needs admin rights on the repository)."""
    r = run(["gh", "api", f"repos/{repo}/actions/runners", "--paginate", "--jq", ".runners[].name"],
            check=False)
    if r.returncode != 0:
        return None
    return [n.strip() for n in r.stdout.splitlines() if n.strip()]


def go(repo: str, *, terms: list[str], allow: list[str], reference: str, check_only: bool = False,
       files: tuple[str, ...] = FILES, secrets: tuple[str, str] = SECRETS, workflow: str = WORKFLOW,
       before_checks=None, before_public=None, after=None, github: bool = False,
       private_reporting: bool = False, run=sh, say=print) -> dict:
    """The whole sequence for `repo` (owner/name), with the check copied from `reference`. Returns
    {"repo", "public", "files_added", "hits", "runners", "reporting"}; raises Refused at the first
    step that must stop it. `github` adds the text on GitHub to the scan; `private_reporting` turns
    on private vulnerability reporting once public, when SECURITY.md points to it."""
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
    if github:
        hits += scan_github(repo, terms, allow, run)
    if hits and not public:
        raise Refused(f"{len(hits)} place(s) carry a private term; rewrite them for an outside "
                      "reader first:\n  " + "\n  ".join(hits[:50]))
    if hits:
        say(f"1. {repo} is already public, and {len(hits)} place(s) in it carry a private term "
            "(said here, not blocking: the check stops new ones):\n  " + "\n  ".join(hits[:50]))
    else:
        say(f"1. scanned {repo}: no private terms in its files or commit messages"
            + (", or in its issues, pull requests, comments, reviews and releases" if github else ""))

    for name, value in zip(secrets, ("\n".join(terms), "\n".join(allow))):
        run(["gh", "secret", "set", name, "-R", repo], stdin=value)
    say(f"2. set {', '.join(secrets)}")

    missing = [p for p in files if api_file(repo, p, branch, run) is None]
    if missing and before_checks is not None:
        before_checks(repo, public)
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
    going = not public and not check_only
    runners = self_hosted_runners(repo, run)
    if runners and going and before_public is not None:
        before_public(repo, runners)
        runners = self_hosted_runners(repo, run)
    if runners is None:
        if going:
            raise Refused(f"GitHub did not list the self-hosted runners of {repo} (that needs admin "
                          "rights), so nothing shows that none would run a fork's pull request")
        say("4. could not list its self-hosted runners")
    elif runners:
        found = f"{len(runners)} self-hosted runner(s) registered ({', '.join(runners)})"
        if going:
            raise Refused(f"{found}: on a public repository a pull request from a fork can run its "
                          "jobs on them; remove them first")
        say(f"4. {found}: " + ("a pull request from a fork can run its jobs on them now" if public
                               else "they must go before it turns public"))
    else:
        say("4. no self-hosted runner is registered")

    if not public and not check_only:
        run(["gh", "repo", "edit", repo, "--visibility", "public",
             "--accept-visibility-change-consequences"])
        public = True
        say(f"5. {repo} is public")
    else:
        say(f"5. left {'public' if public else 'private'}, as it was")
    reporting = None
    if private_reporting and public and not check_only:
        reporting = turn_on_reporting(repo, branch, run)
        say("5. private vulnerability reporting is on" if reporting
            else "5. private vulnerability reporting left as it was: SECURITY.md does not point to it")
    if after is not None:
        after(repo)
    return {"repo": repo, "public": public, "files_added": bool(missing), "hits": hits, "runners": runners,
            "reporting": reporting}


def survey(repo: str, *, terms: list[str], allow: list[str], run=sh, say=print) -> dict:
    """Step 1 alone, and wider: every tracked file, every commit message, and everything written on
    GitHub for `repo`, reported and never changed. Returns {"repo", "public", "hits"}; nothing is
    set, added or made public. A hit names its place (and its link, for GitHub text), never the text."""
    if not terms:
        raise Refused("no terms were given; nothing could be checked")
    info = json.loads(run(["gh", "repo", "view", repo, "--json", "visibility"]).stdout)
    with tempfile.TemporaryDirectory(prefix="go-public-") as tmp:
        run(["gh", "repo", "clone", repo, tmp, "--", "--quiet"])
        hits = scan(tmp, terms, allow, run)
    hits += scan_github(repo, terms, allow, run)
    say(f"{repo}: {len(hits)} place(s) carry a private term" + (":\n  " + "\n  ".join(hits) if hits else ""))
    return {"repo": repo, "public": info["visibility"].upper() == "PUBLIC", "hits": hits}


def _items(repo: str, path: str, run) -> list[dict]:
    """Every item of a paginated REST list under repos/`repo`/, one JSON object a line."""
    out = run(["gh", "api", "--paginate", f"repos/{repo}/{path}", "--jq", ".[] | @json"]).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def github_texts(repo: str, run=sh) -> list[dict]:
    """What people wrote on GitHub for `repo`, outside its files and commits: issue and pull request
    titles and descriptions, their earlier titles, issue and pull request comments, review comments,
    review summaries, commit comments, release names and notes, and every earlier revision of each
    text GitHub keeps an edit history for. Each entry is {"place", "url", "text", "edit"}, where
    "edit" is when an earlier revision was replaced (None for the text as it is now)."""
    texts: list[dict] = []
    editable: dict[str, tuple[str, str]] = {}          # node id -> (place, url), for edit history

    def add(place, url, text, node=None):
        if text:
            texts.append({"place": place, "url": url, "text": text, "edit": None})
        if node:
            editable[node] = (place, url)

    for it in _items(repo, "issues?state=all&per_page=100", run):
        kind = "pull request" if "pull_request" in it else "issue"
        place = f"{kind} #{it['number']}"
        add(f"{place} title", it["html_url"], it.get("title"))
        add(f"{place} description", it["html_url"], it.get("body"), it.get("node_id"))
        for ev in _items(repo, f"issues/{it['number']}/timeline?per_page=100", run):
            if ev.get("event") == "renamed":
                add(f"{place} earlier title", it["html_url"], (ev.get("rename") or {}).get("from"))
        if kind == "pull request":
            for rv in _items(repo, f"pulls/{it['number']}/reviews?per_page=100", run):
                add(f"{place} review", rv.get("html_url") or it["html_url"], rv.get("body"), rv.get("node_id"))
    for c in _items(repo, "issues/comments?per_page=100", run):
        add("comment", c["html_url"], c.get("body"), c.get("node_id"))
    for c in _items(repo, "pulls/comments?per_page=100", run):
        add("review comment", c["html_url"], c.get("body"), c.get("node_id"))
    for c in _items(repo, "comments?per_page=100", run):
        add("commit comment", c["html_url"], c.get("body"), c.get("node_id"))
    for r in _items(repo, "releases?per_page=100", run):
        add(f"release {r.get('tag_name')}", r["html_url"], f"{r.get('name') or ''}\n{r.get('body') or ''}".strip())
    texts += edit_history(editable, run)
    return texts


#: the kinds of node GitHub keeps an edit history for, as the scan reads them
EDITABLE = ("Issue", "PullRequest", "IssueComment", "PullRequestReview", "PullRequestReviewComment",
            "CommitComment")
EDITS = ("query($ids: [ID!]!) { nodes(ids: $ids) { id "
         + " ".join(f"... on {k} {{ userContentEdits(first: 100) {{ nodes {{ diff editedAt deletedAt }} }} }}"
                    for k in EDITABLE)
         + " } }")


def edit_history(editable: dict[str, tuple[str, str]], run=sh) -> list[dict]:
    """The earlier revisions GitHub keeps of each text, by node id -> (place, url), fifty nodes a
    query. A revision already deleted has no text and is left out."""
    out: list[dict] = []
    ids = list(editable)
    for i in range(0, len(ids), 50):
        args = ["gh", "api", "graphql", "-f", f"query={EDITS}"]
        for node in ids[i:i + 50]:
            args += ["-f", f"ids[]={node}"]
        data = json.loads(run(args).stdout).get("data") or {}
        for node in data.get("nodes") or []:
            if not node:
                continue
            place, url = editable.get(node.get("id"), ("", ""))
            for e in ((node.get("userContentEdits") or {}).get("nodes") or []):
                if e and e.get("diff") and not e.get("deletedAt"):
                    out.append({"place": place, "url": url, "text": e["diff"], "edit": e.get("editedAt")})
    return out


def scan_github(repo: str, terms: list[str], allow: list[str], run=sh) -> list[str]:
    """Where a term appears in what people wrote on GitHub for `repo` (`github_texts`), each with
    its link and the term's number, never the text. A hit in an edit revision also says what a
    person has to do about it, since the API cannot delete one."""
    tp = [re.compile(t, re.I) for t in terms]
    ap = [re.compile(a, re.I) for a in allow]
    hits = []
    for t in github_texts(repo, run):
        found = matches(t["text"], tp, ap)
        if not found:
            continue
        where = f"{t['place']} edit revision of {t['edit']}" if t["edit"] else t["place"]
        hit = f"{where} term {','.join(map(str, found))} {t['url']}"
        hits.append(f"{hit} ({EDIT_STEP})" if t["edit"] else hit)
    return hits


def points_to_reporting(repo: str, ref: str | None = None, run=sh) -> bool:
    """Whether `repo`'s security policy sends reports to GitHub's private vulnerability reporting."""
    for path in SECURITY_PATHS:
        text = api_file(repo, path, ref, run)
        if text is not None:
            return bool(REPORTING.search(text))
    return False


def turn_on_reporting(repo: str, ref: str | None = None, run=sh) -> bool:
    """Turn on private vulnerability reporting when SECURITY.md points to it, and read it back.
    Returns whether it is on; a policy that does not point to it leaves the setting alone."""
    if not points_to_reporting(repo, ref, run):
        return False
    run(["gh", "api", "-X", "PUT", f"repos/{repo}/private-vulnerability-reporting"])
    r = run(["gh", "api", f"repos/{repo}/private-vulnerability-reporting"], check=False)
    try:
        return bool(json.loads(r.stdout or "{}").get("enabled"))
    except ValueError:
        return False


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
