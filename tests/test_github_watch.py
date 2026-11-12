from claude_ops.github_watch import (item_events, item_state, open_items_by, parse_fingerprint,
                                     pull_request_fields, pull_request_fingerprint, release_tags)
from claude_ops.watch import State, run_source


def fake_api(releases=("v2.1.1", "v2.1.0"), pr=None, reviews=0):
    """A stand-in for GitHub's public API that records the paths it was asked for."""
    asked = []
    state = {"releases": list(releases),
             "pr": dict(pr or {"state": "open", "merged": False, "comments": 0, "review_comments": 0}),
             "reviews": reviews}

    def get(path):
        asked.append(path)
        if "/releases" in path:
            return [{"tag_name": t} for t in state["releases"]]
        if path.endswith("/reviews?per_page=100"):
            return [{"id": i} for i in range(state["reviews"])]
        return dict(state["pr"])
    return get, asked, state


def test_release_tags_are_the_tags_newest_first():
    get, asked, _ = fake_api(releases=("v3", "v2", "v1"))
    assert release_tags("owner/project", get=get) == ["v3", "v2", "v1"]
    assert asked == ["/repos/owner/project/releases?per_page=20"]


def test_a_pull_request_is_two_requests_and_its_fields_are_counted():
    get, asked, _ = fake_api(pr={"state": "open", "merged": False, "comments": 2, "review_comments": 1}, reviews=3)
    assert pull_request_fields("owner/project", 12, get=get) == {
        "state": "open", "merged": False, "comments": 2, "review_comments": 1, "reviews": 3}
    assert asked == ["/repos/owner/project/pulls/12", "/repos/owner/project/pulls/12/reviews?per_page=100"]


def test_the_fingerprint_reads_back_into_its_fields():
    get, _, _ = fake_api(pr={"state": "closed", "merged": True, "comments": 4, "review_comments": 0}, reviews=1)
    fp = pull_request_fingerprint("owner/project", 12, get=get)
    assert fp == "closed;merged=1;comments=4;review_comments=0;reviews=1"
    assert parse_fingerprint(fp) == {"state": "closed", "merged": True, "comments": 4,
                                     "review_comments": 0, "reviews": 1}


def test_a_watcher_hears_once_about_a_new_release_and_once_about_each_change(tmp_path):
    get, _, api = fake_api()
    state = State(tmp_path / "s.json")
    sent = []

    def send(target, text, transport="iterm"):
        sent.append(text)
        return {"result": "sent"}

    def poll():
        return [run_source(state, "releases", lambda: release_tags("o/p", get=get), lambda new: "releases " + ",".join(new),
                           "chat", send=send)["result"],
                run_source(state, "pr", lambda: [pull_request_fingerprint("o/p", 12, get=get)],
                           lambda new: "pr " + new[0], "chat", send=send)["result"]]

    assert poll() == ["baseline", "baseline"] and sent == []          # the first run says nothing
    assert poll() == ["nothing-new", "nothing-new"] and sent == []
    api["releases"].insert(0, "v2.2.0")
    assert poll() == ["sent", "nothing-new"] and sent == ["releases v2.2.0"]
    api["pr"]["comments"] = 1
    assert poll() == ["nothing-new", "sent"] and sent[-1] == "pr open;merged=0;comments=1;review_comments=0;reviews=0"
    assert poll() == ["nothing-new", "nothing-new"]                    # one change is one message
    api["reviews"] = 1
    assert poll()[1] == "sent"                                         # a review is a change too
    api["pr"].update(state="closed", merged=True)
    assert poll()[1] == "sent" and sent[-1].startswith("pr closed;merged=1")


def test_a_rate_limit_is_a_failed_fetch_that_run_source_counts(tmp_path):
    def get(path):
        raise OSError("HTTP Error 403: rate limit exceeded")

    state = State(tmp_path / "s.json")
    r = run_source(state, "releases", lambda: release_tags("o/p", get=get), lambda n: "x", "chat")
    assert r["result"] == "fetch-failed" and r["fails"] == 1


ME, THEM, BOT = {"login": "me", "type": "User"}, {"login": "maintainer", "type": "User"}, {"login": "ci[bot]", "type": "Bot"}


def fake_lists(lists: dict):
    """A stand-in API that answers each path, without its paging, from `lists`."""
    asked = []

    def get(path):
        asked.append(path)
        return lists[path.split("?")[0]]
    return get, asked


def test_open_items_are_searched_outside_the_authors_own_repositories():
    hit = {"repository_url": "https://api.github.com/repos/other/project", "number": 7, "title": "Fix",
           "updated_at": "2026-01-02T00:00:00Z", "html_url": "https://example.org/7", "pull_request": {}}
    issue = {**hit, "number": 8, "pull_request": None}
    del issue["pull_request"]
    asked = []

    def get(path):
        asked.append(path)
        return {"items": [hit, issue]}
    items = open_items_by("me", get=get)
    assert [(i["repo"], i["number"], i["pull_request"]) for i in items] == [("other/project", 7, True),
                                                                           ("other/project", 8, False)]
    assert asked[0].startswith("/search/issues?q=author:me+is:open+-user:me&")
    open_items_by("me", include_own=True, get=get)
    assert "-user" not in asked[-1]


def test_events_by_other_people_each_have_their_own_id():
    get, asked = fake_lists({
        "/repos/o/p/issues/3/comments": [{"id": 1, "user": THEM, "created_at": "2026-01-01T00:00:00Z"},
                                         {"id": 2, "user": ME, "created_at": "2026-01-02T00:00:00Z"},
                                         {"id": 3, "user": BOT, "created_at": "2026-01-03T00:00:00Z"}],
        "/repos/o/p/pulls/3/reviews": [{"id": 10, "user": THEM, "state": "CHANGES_REQUESTED",
                                        "submitted_at": "2026-01-04T00:00:00Z"},
                                       {"id": 11, "user": THEM, "state": "PENDING"}],
        "/repos/o/p/pulls/3/comments": [{"id": 20, "user": THEM, "created_at": "2026-01-05T00:00:00Z"}],
        "/repos/o/p/pulls/3/commits": [{"sha": "aaa", "author": ME, "commit": {"committer": {"date": "2025-12-01T00:00:00Z"}}},
                                       {"sha": "bbb", "author": THEM, "commit": {"committer": {"date": "2026-01-06T00:00:00Z"}}},
                                       {"sha": "ccc", "author": None,
                                        "commit": {"author": {"name": "Someone"}, "committer": {"date": "2026-01-07T00:00:00Z"}}}],
    })
    events = item_events("o/p", 3, pull_request=True, me="me", get=get)
    assert [(e["id"], e["kind"], e["who"]) for e in events] == [
        ("comment:1", "comment", "maintainer"),
        ("review:10", "review:changes_requested", "maintainer"),
        ("review_comment:20", "review_comment", "maintainer"),
        ("commit:bbb", "commit", "maintainer"),
        ("commit:ccc", "commit", "Someone"),
    ]


def test_an_issue_reads_only_its_comments():
    get, asked = fake_lists({"/repos/o/p/issues/4/comments": [{"id": 5, "user": THEM, "created_at": "x"}]})
    assert [e["id"] for e in item_events("o/p", 4, pull_request=False, me="me", get=get)] == ["comment:5"]
    assert [a.split("?")[0] for a in asked] == ["/repos/o/p/issues/4/comments"]


def test_a_long_thread_is_read_in_pages_up_to_a_limit():
    asked = []

    def get(path):
        asked.append(path)
        return [{"id": len(asked) * 1000 + i, "user": THEM, "created_at": "x"} for i in range(100)]
    events = item_events("o/p", 4, pull_request=False, me="me", get=get)
    assert len(asked) == 5 and len(events) == 500


def test_the_state_of_an_item_that_left_the_open_list():
    assert item_state("o/p", 1, get=lambda path: {"state": "closed", "pull_request": {"merged_at": "2026-01-01"}}) == "merged"
    assert item_state("o/p", 1, get=lambda path: {"state": "closed", "pull_request": {"merged_at": None}}) == "closed"
    assert item_state("o/p", 1, get=lambda path: {"state": "open"}) == "open"


def test_an_unlinked_commit_is_ours_when_its_git_author_or_committer_says_so():
    def commit(sha, **kw):
        return {"sha": sha, "author": None, "committer": kw.get("committer"),
                "commit": {"author": {"name": kw.get("name", "A Person"), "email": kw.get("email", "a@example.org")},
                           "committer": {"date": sha}}}
    get, _ = fake_lists({
        "/repos/o/p/issues/3/comments": [], "/repos/o/p/pulls/3/reviews": [], "/repos/o/p/pulls/3/comments": [],
        "/repos/o/p/pulls/3/commits": [commit("1", email="Me@Example.org"), commit("2", name="My Name"),
                                       commit("3", committer=ME), commit("4", name="Someone Else")],
    })
    events = item_events("o/p", 3, pull_request=True, me="me", also_me=("me@example.org", "my name"), get=get)
    assert [(e["id"], e["who"]) for e in events] == [("commit:4", "Someone Else")]
