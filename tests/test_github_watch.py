from claude_ops.github_watch import (parse_fingerprint, pull_request_fields, pull_request_fingerprint,
                                     release_tags)
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
