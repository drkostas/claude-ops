"""Join the broken paragraphs in text already posted on GitHub (`claude-ops unwrap-github`).

The plain-voice hook stops new text with a paragraph broken across lines. This finds what was posted
before it: the bodies of the issues and pull requests a user opened, the comments they wrote on them
and the notes of the releases they published, in every repository they own (or the ones named), and
rewrites each with `markdown.unwrap`, which joins the lines of each paragraph and list item and leaves
code, tables, headings and quotes as they were. Nothing else in the text changes, and a text already
on one line per paragraph is left alone, so a second run finds nothing.

Without `--apply` it only reports what it would change. Archived repositories cannot be edited and
are skipped. Each edit is one `gh api` PATCH, a second apart (GitHub limits how fast content may be
written).

    claude-ops unwrap-github [--repo OWNER/NAME ...] [--apply] [--limit N]
"""
from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable

from claude_ops import markdown

#: how long to wait between two edits, in seconds
PAUSE_S = 1.0


def gh_json(args: list[str], *, stdin: str | None = None, run=subprocess.run) -> object:
    """`gh api <args>`'s JSON answer; a paginated list is read whole (`--paginate --slurp`)."""
    r = run(["gh", "api", *args], input=stdin, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    out = json.loads(r.stdout or "null")
    if "--slurp" in args and isinstance(out, list):
        return [x for page in out for x in (page if isinstance(page, list) else [page])]
    return out


def pages(path: str, gh=gh_json) -> list[dict]:
    return gh(["--paginate", "--slurp", path]) or []


def owned_repositories(gh=gh_json) -> list[dict]:
    return [r for r in pages("user/repos?affiliation=owner&per_page=100", gh) if not r.get("fork")]


def found_in(repo: str, login: str, gh=gh_json) -> list[dict]:
    """Every text `login` posted in `repo` that has a broken paragraph: {kind, path, url, before, after}."""
    out = []

    def consider(kind: str, path: str, url: str, body: str | None):
        if body and markdown.is_wrapped(body):
            out.append({"kind": kind, "path": path, "url": url, "before": body, "after": markdown.unwrap(body)})

    for it in pages(f"repos/{repo}/issues?state=all&creator={login}&per_page=100", gh):
        kind = "pull request" if "pull_request" in it else "issue"
        consider(kind, f"repos/{repo}/issues/{it['number']}", it.get("html_url", ""), it.get("body"))
    for cm in pages(f"repos/{repo}/issues/comments?per_page=100", gh):
        if (cm.get("user") or {}).get("login") == login:
            consider("comment", f"repos/{repo}/issues/comments/{cm['id']}", cm.get("html_url", ""), cm.get("body"))
    for rel in pages(f"repos/{repo}/releases?per_page=100", gh):
        if (rel.get("author") or {}).get("login") == login:
            consider("release", f"repos/{repo}/releases/{rel['id']}", rel.get("html_url", ""), rel.get("body"))
    return out


def apply(fix: dict, gh=gh_json) -> None:
    gh(["-X", "PATCH", fix["path"], "--input", "-"], stdin=json.dumps({"body": fix["after"]}))


def run(repos: list[str] | None = None, *, write: bool = False, limit: int | None = None, gh=gh_json,
        sleep: Callable[[float], None] = time.sleep, say=print) -> dict:
    """Find, and with `write` rewrite, every broken paragraph the user posted. {found, fixed, failed, skipped}."""
    login = gh(["user"])["login"]
    if repos:
        names = repos
        skipped: list[str] = []
    else:
        listed = owned_repositories(gh)
        names = [r["full_name"] for r in listed if not r.get("archived")]
        skipped = [r["full_name"] for r in listed if r.get("archived")]
    found = fixed = 0
    failed: list[str] = []
    for repo in names:
        try:
            fixes = found_in(repo, login, gh)
        except RuntimeError as e:
            failed.append(f"{repo}: {e}")
            continue
        if fixes:
            say(f"{repo}: {len(fixes)} to join")
        for fix in fixes:
            if limit is not None and found >= limit:
                break
            found += 1
            if not write:
                say(f"  would join the {fix['kind']} {fix['url']}")
                continue
            try:
                apply(fix, gh)
                fixed += 1
                say(f"  joined the {fix['kind']} {fix['url']}")
            except RuntimeError as e:
                failed.append(f"{fix['url']}: {e}")
            sleep(PAUSE_S)
    return {"found": found, "fixed": fixed, "failed": failed, "skipped": skipped}
