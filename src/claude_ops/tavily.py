"""One MCP server for Tavily that holds several API keys and moves to the next one at a key's limit.

Each Tavily key has its own usage limit. Running one MCP server per key means one process per key in
every Claude Code chat, and moving to the next server by hand when a key says it is at its limit. This
server takes all the keys and does the moving itself:

    TAVILY_API_KEYS=key-one,key-two,key-three python3 -m claude_ops.tavily

As an MCP server in a Claude Code configuration:

    {"command": "python3", "args": ["-m", "claude_ops.tavily"], "env": {"TAVILY_API_KEYS": "..."}}

It speaks MCP over stdio (one JSON-RPC message a line) and offers the tools of the tavily-mcp package,
with the same names and arguments: tavily_search, tavily_extract, tavily_crawl, tavily_map and
tavily_research. A key Tavily refuses for its limit (HTTP 429, 432, 433) rests for REST_S, and an
invalid one (401) for a day; the request goes on with the next key. Which keys rest is kept in a small
state file (STATE, a hash of each key and when it may be used again, never the key), so the servers of
several chats skip the same spent key. When every key rests, the tool says so and when the first comes
back. It needs no package beyond the standard library.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

from claude_ops import __version__

API = "https://api.tavily.com"
#: statuses that mean this key cannot be used now: invalid, or at its limit
REST_S = {401: 24 * 3600, 429: 3600, 432: 24 * 3600, 433: 24 * 3600}
#: where each key's rest is kept, by a hash of the key (CLAUDE_OPS_TAVILY_STATE names another)
STATE = Path(os.environ.get("CLAUDE_OPS_TAVILY_STATE")
             or Path.home() / ".local" / "state" / "claude-ops" / "tavily-keys.json")
PROTOCOL = "2025-06-18"
TIMEOUT_S = 60

#: (post or get) -> (status, body): what talks to Tavily, replaced in tests
Http = Callable[[str, str, str, Optional[dict]], Tuple[int, dict]]


def keys_from_env(env: dict | None = None) -> list[str]:
    """The keys: TAVILY_API_KEYS, separated by commas or spaces, or a single TAVILY_API_KEY."""
    env = os.environ if env is None else env
    raw = env.get("TAVILY_API_KEYS") or env.get("TAVILY_API_KEY") or ""
    return [k for k in raw.replace(",", " ").split() if k]


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class Keys:
    """The keys in order, the one in use, and which rest until when (shared through `state`)."""

    def __init__(self, keys: list[str], state: Path = STATE, clock: Callable[[], float] = time.time):
        self.keys, self.state, self.clock, self.at = list(keys), state, clock, 0

    def _rests(self) -> dict[str, float]:
        try:
            return {k: float(v) for k, v in json.loads(self.state.read_text()).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def usable(self) -> list[str]:
        """The keys that do not rest now, starting from the one in use."""
        rests, now = self._rests(), self.clock()
        order = self.keys[self.at:] + self.keys[:self.at]
        return [k for k in order if rests.get(fingerprint(k), 0) <= now]

    def rest(self, key: str, status: int) -> None:
        """`key` was refused with `status`: it rests, and the next key is the one in use."""
        rests = {k: v for k, v in self._rests().items() if v > self.clock()}
        rests[fingerprint(key)] = self.clock() + REST_S.get(status, 3600)
        try:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state.with_name(self.state.name + f".{os.getpid()}")
            tmp.write_text(json.dumps(rests))
            tmp.replace(self.state)
        except OSError:
            pass                                     # resting in this process only
        if key in self.keys:
            self.at = (self.keys.index(key) + 1) % len(self.keys)

    def back_at(self) -> float | None:
        """When the first resting key may be used again."""
        mine = {fingerprint(k) for k in self.keys}
        times = [v for k, v in self._rests().items() if k in mine]
        return min(times) if times else None


def http(method: str, url: str, key: str, body: dict | None) -> tuple[int, dict]:
    """One request to Tavily with `key`: (status, the JSON body or {"error": text})."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": f"claude-ops/{__version__} tavily"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": str(e)}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return 0, {"error": f"Tavily did not answer: {e}"}


class Exhausted(Exception):
    """Every key rests."""


def call(keys: Keys, method: str, path: str, body: dict | None, send: Http = http) -> tuple[str, int, dict]:
    """Send one request with the first usable key, moving on past a key that is refused for its
    limit or as invalid. (the key used, status, body). Raises Exhausted when every key rests."""
    for key in keys.usable():
        status, out = send(method, f"{API}{path}", key, body)
        if status in REST_S:
            keys.rest(key, status)
            continue
        return key, status, out
    raise Exhausted()


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _bool(desc: str, default: bool = False) -> dict:
    return {"type": "boolean", "description": desc, "default": default}


def _list(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc, "default": []}


def _depths() -> dict:
    return {"max_depth": {"type": "integer", "minimum": 1, "default": 1, "description": "How many links deep from the start"},
            "max_breadth": {"type": "integer", "minimum": 1, "default": 20, "description": "Links followed a page"},
            "limit": {"type": "integer", "minimum": 1, "default": 50, "description": "Pages in all, at most"},
            "instructions": _str("What to look for, in plain words"),
            "select_paths": _list("Path patterns (regular expressions) to keep"),
            "select_domains": _list("Domain patterns (regular expressions) to keep"),
            "allow_external": _bool("Follow links to other domains", True)}


TOOLS = [
    {"name": "tavily_search", "description": "Search the web with Tavily and get the results with their content.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": _str("What to search for"),
         "search_depth": {"type": "string", "enum": ["basic", "advanced", "fast", "ultra-fast"], "default": "basic",
                          "description": "How deep the search goes"},
         "topic": {"type": "string", "enum": ["general"], "default": "general", "description": "The kind of search"},
         "time_range": {"type": "string", "enum": ["day", "week", "month", "year"], "description": "Only results this recent"},
         "start_date": _str("Only results after this date (YYYY-MM-DD)"),
         "end_date": _str("Only results before this date (YYYY-MM-DD)"),
         "max_results": {"type": "number", "minimum": 5, "maximum": 20, "default": 5, "description": "How many results"},
         "include_images": _bool("Include images"),
         "include_image_descriptions": _bool("Include a description of each image"),
         "include_raw_content": _bool("Include each page's whole text"),
         "include_domains": _list("Only these domains"),
         "exclude_domains": _list("Never these domains"),
         "country": _str("Prefer results from this country"),
         "include_favicon": _bool("Include each site's icon"),
         "exact_match": {"type": "boolean", "description": "Only results with the exact phrase in quotes"}}}},
    {"name": "tavily_extract", "description": "Read the content of one or more web pages with Tavily.",
     "inputSchema": {"type": "object", "required": ["urls"], "properties": {
         "urls": {"type": "array", "items": {"type": "string"}, "description": "The pages to read"},
         "extract_depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic",
                           "description": "advanced reads tables and embedded content too"},
         "include_images": _bool("Include images"),
         "format": {"type": "string", "enum": ["markdown", "text"], "default": "markdown", "description": "The form of the content"},
         "include_favicon": _bool("Include each site's icon"),
         "query": _str("Order the content by how well it matches this")}}},
    {"name": "tavily_crawl", "description": "Crawl a site from a start page with Tavily and read the pages found.",
     "inputSchema": {"type": "object", "required": ["url"], "properties": {
         "url": _str("The page to start from"), **_depths(),
         "extract_depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic",
                           "description": "advanced reads tables and embedded content too"},
         "format": {"type": "string", "enum": ["markdown", "text"], "default": "markdown", "description": "The form of the content"},
         "include_favicon": _bool("Include each site's icon")}}},
    {"name": "tavily_map", "description": "List the pages of a site from a start page with Tavily, without reading them.",
     "inputSchema": {"type": "object", "required": ["url"], "properties": {"url": _str("The page to start from"), **_depths()}}},
    {"name": "tavily_research", "description": "Ask Tavily to research a question across many sources and write a report. It can take minutes.",
     "inputSchema": {"type": "object", "required": ["input"], "properties": {
         "input": _str("The question or topic"),
         "model": {"type": "string", "enum": ["mini", "pro", "auto"], "default": "auto",
                   "description": "mini is quicker, pro goes deeper"}}}},
]
ENDPOINT = {"tavily_search": "/search", "tavily_extract": "/extract", "tavily_crawl": "/crawl", "tavily_map": "/map"}
#: research: the first wait between polls, the growth, the longest wait, and how long in all
POLL = (2.0, 1.5, 10.0)
POLL_FOR_S = {"mini": 300, "pro": 900, "auto": 900}


def research(keys: Keys, args: dict, send: Http = http, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Start a research task and poll it until it is done. The task belongs to the key that started it."""
    model = args.get("model") or "auto"
    key, status, out = call(keys, "POST", "/research", {"input": args.get("input"), "model": model}, send)
    rid = out.get("request_id")
    if status >= 400 or not rid:
        return {"error": f"Tavily did not start the research ({status}): {out}"}
    wait, waited = POLL[0], 0.0
    while waited < POLL_FOR_S.get(model, 900):
        sleep(wait)
        waited += wait
        status, out = send("GET", f"{API}/research/{rid}", key, None)
        if status >= 400:
            return {"error": f"Tavily answered {status} for research task {rid}: {out}"}
        if out.get("status") == "completed":
            return {"content": out.get("content") or ""}
        if out.get("status") == "failed":
            return {"error": f"research task {rid} failed"}
        wait = min(wait * POLL[1], POLL[2])
    return {"error": f"research task {rid} took longer than {POLL_FOR_S.get(model, 900)} seconds"}


def run_tool(keys: Keys, name: str, args: dict, send: Http = http, sleep=time.sleep) -> tuple[str, bool]:
    """(the text the tool answers, whether it is an error)."""
    try:
        if name == "tavily_research":
            out = research(keys, args, send, sleep)
            return (out["content"], False) if "content" in out else (out["error"], True)
        if name not in ENDPOINT:
            return f"no tool called {name}", True
        body = {k: v for k, v in args.items() if v not in (None, "", [])}
        _key, status, out = call(keys, "POST", ENDPOINT[name], body, send)
        return json.dumps(out, indent=2, ensure_ascii=False), status >= 400 or status == 0
    except Exhausted:
        back = keys.back_at()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(back)) if back else "unknown"
        return f"every Tavily key is at its limit or invalid; the first may be used again at {when}", True


def handle(msg: dict, keys: Keys, send: Http = http) -> dict | None:
    """One JSON-RPC message in, the answer out (None for a notification)."""
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "tavily", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params") or {}
        text, err = run_tool(keys, p.get("name") or "", p.get("arguments") or {}, send)
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    elif mid is None:
        return None
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return None if mid is None else {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout, keys: Keys | None = None, send: Http = http) -> int:
    keys = keys or Keys(keys_from_env())
    if not keys.keys:
        print("set TAVILY_API_KEYS (keys separated by commas) or TAVILY_API_KEY", file=sys.stderr)
        return 2
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        out = handle(msg, keys, send) if isinstance(msg, dict) else None
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
