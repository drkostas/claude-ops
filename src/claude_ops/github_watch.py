"""Sources for a watcher on GitHub: a repository's releases, one pull request, or everything
an author has open in other people's repositories.

`claude_ops.watch.run_source` wants a `fetch()` that returns a list of item ids, and wakes the chat
when an id is new. These two functions are such sources for a project the user does not own and wants
to follow, for example to carry a patch until the project merges it.

- `release_tags(repo)` returns the tag of each of the newest releases, newest first. A new release
  is a new id.
- `pull_request_fingerprint(repo, number)` returns one id that changes when somebody comments on the
  pull request or reviews it, or when it is closed or merged. A changed id is a new id, so the chat is
  woken once per change.
- `parse_fingerprint(fingerprint)` reads such an id back into its fields, for the message.

To follow every issue and pull request an author opened in repositories they do not own, without
naming each one, use `open_items_by(author)` to list them and `item_events(...)` for what other
people did on each: every comment, review, review comment and pushed commit, one id per event, so
a seen-set wakes the chat once per event. `item_state(...)` says whether one that left the open
list was closed or merged.

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
import urllib.parse
import urllib.request
from collections.abc import Callable

API = "https://api.github.com"
USER_AGENT = "claude-ops-github-watch"


def _get(path: str, *, timeout: float = 30.0, token: str | None = None):
    """The JSON answer of one API path, or an exception (a network error, a rate limit)."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def api_get(token: str | None = None) -> Callable:
    """A `get` for the functions here. With a token, requests are authenticated, which raises
    GitHub's limit from 60 to 5000 an hour. The token is sent in a header and never logged."""
    return lambda path: _get(path, token=token)


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


#: the most pages of 100 read from one list, so a runaway thread cannot use up the hourly limit
MAX_PAGES = 5


def _pages(path: str, get: Callable) -> list:
    sep = "&" if "?" in path else "?"
    out: list = []
    for page in range(1, MAX_PAGES + 1):
        batch = get(f"{path}{sep}per_page=100&page={page}")
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def open_items_by(author: str, *, include_own: bool = False, get: Callable = _get) -> list[dict]:
    """Every open issue and pull request `author` opened, newest change first, as
    {"repo", "number", "pull_request", "title", "updated_at", "url"}.

    Repositories `author` owns are left out unless `include_own`, because those usually have
    their own follower. One search request covers up to 100 items (GitHub's search allows 10
    unauthenticated requests a minute)."""
    terms = [f"author:{author}", "is:open"] + ([] if include_own else [f"-user:{author}"])
    query = urllib.parse.quote_plus(" ".join(terms), safe=":")
    out = []
    for page in range(1, MAX_PAGES + 1):
        found = get(f"/search/issues?q={query}&sort=updated&order=desc&per_page=100&page={page}")
        items = found.get("items", [])
        for item in items:
            out.append({
                "repo": item["repository_url"].split("/repos/", 1)[1],
                "number": item["number"],
                "pull_request": "pull_request" in item,
                "title": item.get("title", ""),
                "updated_at": item.get("updated_at", ""),
                "url": item.get("html_url", ""),
            })
        if len(items) < 100:
            break
    return out


def _login(user) -> str:
    return (user or {}).get("login") or ""


def _is_bot(user) -> bool:
    return (user or {}).get("type") == "Bot"


def item_events(repo: str, number: int, *, pull_request: bool, me: str, get: Callable = _get) -> list[dict]:
    """What people other than `me` did on one issue or pull request, oldest first, as
    {"id", "kind", "who", "at", "url"}.

    The kinds are "comment", "review" (with its state, such as "review:changes_requested"),
    "review_comment" and "commit" (a commit someone else pushed to the pull request's branch, for
    example a maintainer merging the base branch in). Each id is unique and never changes, so a
    seen-set holds what was already reported. Bot accounts and pending reviews are left out.
    An issue is one request, a pull request four."""
    base = f"/repos/{repo}"
    events: list[dict] = []
    for c in _pages(f"{base}/issues/{number}/comments", get):
        if _login(c.get("user")) != me and not _is_bot(c.get("user")):
            events.append({"id": f"comment:{c['id']}", "kind": "comment", "who": _login(c.get("user")),
                           "at": c.get("created_at", ""), "url": c.get("html_url", "")})
    if pull_request:
        for r in _pages(f"{base}/pulls/{number}/reviews", get):
            if r.get("state") == "PENDING" or _login(r.get("user")) == me or _is_bot(r.get("user")):
                continue
            events.append({"id": f"review:{r['id']}", "kind": f"review:{str(r.get('state', '')).lower()}",
                           "who": _login(r.get("user")), "at": r.get("submitted_at") or "",
                           "url": r.get("html_url", "")})
        for c in _pages(f"{base}/pulls/{number}/comments", get):
            if _login(c.get("user")) != me and not _is_bot(c.get("user")):
                events.append({"id": f"review_comment:{c['id']}", "kind": "review_comment",
                               "who": _login(c.get("user")), "at": c.get("created_at", ""),
                               "url": c.get("html_url", "")})
        for c in _pages(f"{base}/pulls/{number}/commits", get):
            who = _login(c.get("author")) or (c.get("commit") or {}).get("author", {}).get("name", "")
            if who == me or _is_bot(c.get("author")):
                continue
            events.append({"id": f"commit:{c['sha']}", "kind": "commit", "who": who,
                           "at": (c.get("commit") or {}).get("committer", {}).get("date", ""),
                           "url": c.get("html_url", "")})
    return sorted(events, key=lambda e: e["at"])


def item_state(repo: str, number: int, *, get: Callable = _get) -> str:
    """"open", "closed" or "merged" for one issue or pull request. One request."""
    issue = get(f"/repos/{repo}/issues/{number}")
    if (issue.get("pull_request") or {}).get("merged_at"):
        return "merged"
    return issue.get("state", "")
