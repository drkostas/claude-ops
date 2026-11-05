"""Sources for a watcher on a GitHub repository: its releases, and one pull request.

`claude_ops.watch.run_source` wants a `fetch()` that returns a list of item ids, and wakes the chat
when an id is new. These two functions are such sources for a project the user does not own and wants
to follow, for example to carry a patch until the project merges it.

- `release_tags(repo)` returns the tag of each of the newest releases, newest first. A new release
  is a new id.
- `pull_request_fingerprint(repo, number)` returns one id that changes when somebody comments on the
  pull request or reviews it, or when it is closed or merged. A changed id is a new id, so the chat is
  woken once per change.
- `parse_fingerprint(fingerprint)` reads such an id back into its fields, for the message.

They read GitHub's public API with no credentials, so a scheduler needs no token. GitHub allows 60
such requests an hour per address. A pull request is two requests (the pull request, and its reviews)
and a release list is one, so a watcher on both that runs every 30 minutes makes 6 an hour.

    from claude_ops.watch import State, lock, run_source
    from claude_ops.github_watch import release_tags, pull_request_fingerprint

    run_source(state, "releases", lambda: release_tags("owner/project"), describe, "my-chat")
    run_source(state, "pull-request", lambda: [pull_request_fingerprint("owner/project", 12)],
               describe_change, "my-chat")
"""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable

API = "https://api.github.com"
USER_AGENT = "claude-ops-github-watch"


def _get(path: str, *, timeout: float = 30.0):
    """The JSON answer of one public API path, or an exception (a network error, a rate limit)."""
    req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json",
                                                      "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def release_tags(repo: str, *, count: int = 20, get: Callable = _get) -> list[str]:
    """The tags of the newest `count` releases of `owner/name`, newest first."""
    return [r["tag_name"] for r in get(f"/repos/{repo}/releases?per_page={count}")]


def pull_request_fields(repo: str, number: int, *, get: Callable = _get) -> dict:
    """What changes on a pull request that its follower wants to hear about."""
    pr = get(f"/repos/{repo}/pulls/{number}")
    reviews = get(f"/repos/{repo}/pulls/{number}/reviews?per_page=100")
    return {"state": pr["state"], "merged": bool(pr["merged"]), "comments": pr["comments"],
            "review_comments": pr["review_comments"], "reviews": len(reviews)}


def pull_request_fingerprint(repo: str, number: int, *, get: Callable = _get) -> str:
    """One id for the pull request's current state, such as
    "open;merged=0;comments=2;review_comments=0;reviews=1". It differs from the last one whenever
    a comment, a review comment or a review was added, or the pull request was closed or merged."""
    f = pull_request_fields(repo, number, get=get)
    return (f"{f['state']};merged={int(f['merged'])};comments={f['comments']};"
            f"review_comments={f['review_comments']};reviews={f['reviews']}")


def parse_fingerprint(fingerprint: str) -> dict:
    """The fields of a `pull_request_fingerprint` back, as {"state", "merged", "comments",
    "review_comments", "reviews"}, with the numbers as integers and merged as a boolean."""
    state, *rest = fingerprint.split(";")
    fields = dict(part.split("=", 1) for part in rest)
    return {"state": state, "merged": fields["merged"] == "1", "comments": int(fields["comments"]),
            "review_comments": int(fields["review_comments"]), "reviews": int(fields["reviews"])}
