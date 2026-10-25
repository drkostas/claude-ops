"""One Tavily MCP server for several keys: it moves to the next key at a key's limit, and speaks MCP."""
import io
import json

import pytest

from claude_ops import tavily


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def keys(tmp_path, n=3, clock=None):
    return tavily.Keys([f"key-{i}" for i in range(1, n + 1)], state=tmp_path / "rest.json",
                       clock=clock or Clock())


def test_keys_come_from_the_environment():
    assert tavily.keys_from_env({"TAVILY_API_KEYS": "a,b, c"}) == ["a", "b", "c"]
    assert tavily.keys_from_env({"TAVILY_API_KEY": "solo"}) == ["solo"]
    assert tavily.keys_from_env({}) == []


def test_a_key_at_its_limit_rests_and_the_next_is_used(tmp_path):
    clock = Clock()
    ks = keys(tmp_path, clock=clock)
    calls = []

    def send(method, url, key, body):
        calls.append(key)
        return (429, {"detail": "limit"}) if key == "key-1" else (200, {"results": [], "key": key})

    key, status, out = tavily.call(ks, "POST", "/search", {"query": "x"}, send)
    assert (key, status) == ("key-2", 200) and calls == ["key-1", "key-2"]
    # the next request starts with the key that worked, and skips the resting one
    calls.clear()
    tavily.call(ks, "POST", "/search", {"query": "y"}, send)
    assert calls == ["key-2"]
    # another server reading the same state skips it too, and never sees a key in the file
    other = tavily.Keys(["key-1", "key-2", "key-3"], state=tmp_path / "rest.json", clock=clock)
    assert other.usable() == ["key-2", "key-3"]
    assert "key-1" not in (tmp_path / "rest.json").read_text()
    # an hour later the key is used again
    clock.t += tavily.REST_S[429] + 1
    assert "key-1" in other.usable()


def test_every_key_at_its_limit_is_said_with_when_one_comes_back(tmp_path):
    ks = keys(tmp_path, n=2)

    def send(method, url, key, body):
        return 432, {"detail": "plan limit"}

    with pytest.raises(tavily.Exhausted):
        tavily.call(ks, "POST", "/search", {"query": "x"}, send)
    text, err = tavily.run_tool(ks, "tavily_search", {"query": "x"}, send)
    assert err and "every Tavily key is at its limit" in text


def test_an_invalid_key_rests_a_day_and_other_errors_are_answered(tmp_path):
    ks = keys(tmp_path, n=2)

    def send(method, url, key, body):
        return (401, {"detail": "bad key"}) if key == "key-1" else (400, {"detail": "bad query"})

    text, err = tavily.run_tool(ks, "tavily_search", {"query": "x", "include_domains": []}, send)
    assert err and "bad query" in text
    assert ks.usable() == ["key-2"]


def test_each_tool_posts_to_its_endpoint_without_empty_arguments(tmp_path):
    ks = keys(tmp_path)
    seen = []

    def send(method, url, key, body):
        seen.append((method, url, body))
        return 200, {"ok": True}

    for name in ("tavily_search", "tavily_extract", "tavily_crawl", "tavily_map"):
        text, err = tavily.run_tool(ks, name, {"query": "q", "urls": ["https://example.org"],
                                               "url": "https://example.org", "country": ""}, send)
        assert not err and json.loads(text) == {"ok": True}
    assert [u.rsplit("/", 1)[-1] for _m, u, _b in seen] == ["search", "extract", "crawl", "map"]
    assert all("country" not in b for _m, _u, b in seen)
    assert tavily.run_tool(ks, "tavily_nothing", {}, send)[1] is True


def test_research_is_started_and_polled_with_the_key_that_started_it(tmp_path):
    ks = keys(tmp_path)
    answers = iter([(200, {"status": "pending"}), (200, {"status": "completed", "content": "the report"})])
    seen = []

    def send(method, url, key, body):
        seen.append((method, url.rsplit("/", 2)[-2:], key))
        if method == "POST":
            return (429, {}) if key == "key-1" else (200, {"request_id": "r1"})
        return next(answers)

    text, err = tavily.run_tool(ks, "tavily_research", {"input": "why"}, send, sleep=lambda s: None)
    assert not err and text == "the report"
    assert [k for m, _u, k in seen if m == "GET"] == ["key-2", "key-2"]


def test_it_speaks_mcp_over_stdio(tmp_path):
    ks = keys(tmp_path)

    def send(method, url, key, body):
        return 200, {"results": [{"title": "t", "url": "https://example.org"}]}

    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "tavily_search", "arguments": {"query": "x"}}},
             {"jsonrpc": "2.0", "id": 4, "method": "no/such"}]
    out = io.StringIO()
    assert tavily.serve(io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n\nnot json\n"), out, ks, send) == 0
    got = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [g["id"] for g in got] == [1, 2, 3, 4]
    assert got[0]["result"]["capabilities"] == {"tools": {}} and got[0]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in got[1]["result"]["tools"]] == [
        "tavily_search", "tavily_extract", "tavily_crawl", "tavily_map", "tavily_research"]
    assert got[2]["result"]["isError"] is False and "example.org" in got[2]["result"]["content"][0]["text"]
    assert got[3]["error"]["code"] == -32601


def test_without_a_key_it_says_so_and_stops():
    assert tavily.serve(io.StringIO(""), io.StringIO(), tavily.Keys([])) == 2
