"""look: pictures of each page, each button's states and the in-page checks, on pages served from a folder."""
from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from claude_ops import look

PAGES = {
    # a fixed narrow box holding a long word, two buttons on top of each other, and one past the right edge
    "faults.html": """<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0">
<div style="width:60px;white-space:nowrap;overflow:hidden">Averyveryverylongwordthatdoesnotfit</div>
<button style="position:absolute;top:100px;left:10px;width:120px">First</button>
<button style="position:absolute;top:105px;left:40px;width:120px">Second</button>
<button style="position:absolute;top:200px;left:340px;width:120px">Past the edge</button>
<div style="overflow-x:auto;width:200px;white-space:nowrap"><span style="display:inline-block;width:600px">A row that scrolls sideways</span></div>
</body>""",
    "empty.html": "<!doctype html><body></body>",
    # Send posts and draws itself again as it was (the shape of a button that comes back after its tap),
    # Stays does the same but is marked as meant to stay, and Nothing does nothing at all
    "again.html": """<!doctype html><meta name=viewport content="width=device-width"><body>
<div id=out>ready</div>
<div id=row></div>
<button id=nothing>Nothing</button>
<script>
function draw() {
  document.getElementById('row').innerHTML =
    '<button id=send>Send</button> <button id=stays data-stays>Stays</button>';
  for (const id of ['send', 'stays']) document.getElementById(id).onclick = () => {
    document.getElementById('out').textContent = 'waiting';
    fetch('/act', {method: 'POST', body: '{}'}).then(r => r.json()).then(j => {
      document.getElementById('out').textContent = j.ok ? 'done' : 'refused: ' + j.error;
      draw();
    });
  };
}
draw();
</script></body>""",
    # text that does not fit its button: clipped in a span, clipped in a flex row, and running over a visible edge
    "clip.html": """<!doctype html><meta name=viewport content="width=device-width"><body>
<button style="display:block;width:60px;overflow:hidden;white-space:nowrap;margin:10px"><span>Hidden label that is long</span></button>
<div role=button style="display:flex;width:60px;overflow:hidden;margin:10px"><div style="flex-shrink:0;white-space:nowrap">Flex label hidden away</div></div>
<div role=button style="display:flex;width:60px;overflow:visible;margin:10px"><div style="flex-shrink:0;white-space:nowrap">Flex label running over</div></div>
<button style="display:block;margin:10px">Fits</button>
</body>""",
    # a form that posts into a new window, and a window opened by script that posts as it loads
    "popup.html": """<!doctype html><meta name=viewport content="width=device-width"><body>
<form method=post action="/act-form" target="_blank"><button>Send form</button></form>
<button onclick="window.open('/poster.html')">Open poster</button>
</body>""",
    "poster.html": """<!doctype html><body>poster<script>fetch('/act-popup', {method: 'POST', body: '{}'})</script></body>""",
    # a service worker that posts as it installs and forwards the page's posts itself
    "sw.html": """<!doctype html><meta name=viewport content="width=device-width"><body>
<button id=p>Post through worker</button>
<script>
if (navigator.serviceWorker) navigator.serviceWorker.register('/sw.js');
document.getElementById('p').onclick = () => fetch('/act-sw', {method: 'POST', body: '{}'});
</script></body>""",
    "sw.js": """self.addEventListener('install', e => { self.skipWaiting();
  e.waitUntil(fetch('/sw-install', {method: 'POST', body: '{}'}).catch(() => {})); });
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => { if (e.request.method === 'POST') e.respondWith(fetch(e.request)); });""",
    # a page that talks over a WebSocket as it loads and on a tap
    "ws.html": """<!doctype html><meta name=viewport content="width=device-width"><body>
<script>const w = new WebSocket('ws://' + location.host + '/ws'); w.onopen = () => w.send('hi');</script>
<button onclick="w.readyState === 1 && w.send('tap'); document.body.append('sent')">Socket</button>
</body>""",
    # Approve stays greyed out until the box holds text
    "box.html": """<!doctype html><meta name=viewport content="width=device-width"><body>
<input id=t placeholder="Type it, then Approve">
<button id=ok disabled>Approve</button> <button>Decline</button>
<script>
const t = document.getElementById('t');
t.oninput = () => { document.getElementById('ok').disabled = !t.value; };
</script></body>""",
}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception as e:   # noqa: BLE001
        pytest.skip(f"no Chromium for Playwright: {e}")
    root = tmp_path_factory.mktemp("site")
    for name, text in PAGES.items():
        (root / name).write_text(text)
    posts: list[str] = []
    loads = {"moving": 0}

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def do_POST(self):   # noqa: N802
            posts.append(self.path)
            self.send_response(500)
            self.end_headers()

        def do_GET(self):   # noqa: N802
            if self.path.split("?")[0] == "/ws":
                posts.append(f"websocket {self.path}")   # a socket that reached the server counts as reaching it
                self.send_response(404)
                self.end_headers()
                return
            if self.path == "/moving.html":
                # the first load draws Alpha then Beta, every later load Beta then Alpha
                loads["moving"] += 1
                order = ["Alpha", "Beta"] if loads["moving"] == 1 else ["Beta", "Alpha"]
                body = ("<!doctype html><body>" + "".join(f"<button>{w}</button>" for w in order)
                        + "</body>").encode()
                self.send_response(200)
                self.send_header("content-type", "text/html")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", posts
    server.shutdown()


def _kinds(page):
    return sorted({f["kind"] for f in page["findings"]})


def test_slug():
    assert look.slug("/items/42?at=end") == "items-42-at-end"
    assert look.slug("/") == "root"


def test_answers_from_a_file_keeps_the_defaults_it_does_not_name():
    a = look.answers_from({"no": {"status": 403, "body": {"ok": False, "error": "no"}}, "slow_s": 2})
    assert (a.yes.status, a.yes.body) == (200, '{"ok": true}')
    assert (a.no.status, json.loads(a.no.body)) == (403, {"ok": False, "error": "no"})
    assert a.slow_s == 2
    assert look.answers_from(None).slow_s == look.SLOW_S


def test_the_checks_find_cut_off_text_overlap_and_a_button_past_the_edge(site, tmp_path):
    base, _ = site
    report = look.look(base, ["/faults.html"], tmp_path, taps=False, boxes=False, settle_ms=100)
    page = report["pages"][0]
    assert {"cut_off", "overlap", "off_screen"} <= set(_kinds(page))
    details = {f["kind"]: [] for f in page["findings"]}
    for f in page["findings"]:
        details[f["kind"]].append(f["detail"])
    assert any("Averyvery" in d for d in details["cut_off"])
    assert "First and Second" in details["overlap"]
    assert "Past the edge" in details["off_screen"]
    assert not any("scrolls sideways" in d for d in details["off_screen"]), "a sideways scroller is not a fault"
    assert set(page["pictures"]) == {"load", "end"}
    assert all((tmp_path / f).exists() for f in page["pictures"].values())
    assert json.loads((tmp_path / "report.json").read_text())["pages"][0]["path"] == "/faults.html"


def test_an_empty_page_is_a_finding(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/empty.html"], tmp_path, taps=False, boxes=False, settle_ms=100)["pages"][0]
    assert _kinds(page) == ["empty"]


def test_a_page_that_does_not_load_says_so(tmp_path):
    pytest.importorskip("playwright.sync_api")
    try:
        page = look.look("http://127.0.0.1:9", ["/"], tmp_path, settle_ms=100, timeout_s=5)["pages"][0]
    except Exception as e:   # noqa: BLE001
        pytest.skip(f"no Chromium for Playwright: {e}")
    assert _kinds(page) == ["not_loaded"]


def test_taps_show_each_answer_and_never_reach_the_server(site, tmp_path):
    base, posts = site
    answers = look.Answers(slow_s=1.5)
    report = look.look(base, ["/again.html"], tmp_path, boxes=False, again="[data-stays]", answers=answers,
                       settle_ms=150)
    page = report["pages"][0]
    taps = {t["button"]: t for t in page["taps"]}
    assert set(taps) == {"Nothing", "Send", "Stays"}
    assert set(taps["Send"]["pictures"]) == {"yes", "no", "slow-waiting", "slow", "none"}
    assert taps["Send"]["sent"] and taps["Send"]["sent"][0].startswith("POST ")
    assert set(taps["Nothing"]["pictures"]) == {"yes"} and taps["Nothing"]["sent"] == []
    found = {(f["kind"], f["detail"]) for f in page["findings"]}
    assert ("offered_again", "Send") in found
    assert ("offered_again", "Stays") not in found, "a button marked as meant to stay is not reported"
    assert ("dead_tap", "Nothing") in found
    assert posts == [], "no request other than GET reaches the server"
    assert all((tmp_path / f).exists() for t in page["taps"] for f in t["pictures"].values())


def test_a_box_is_shown_empty_and_with_text(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/box.html"], tmp_path, taps=False, settle_ms=100)["pages"][0]
    assert page["boxes"] == [{"box": "Type it, then Approve", "picture": "box-html-box1.png",
                              "enabled_empty": ["Decline"], "enabled_text": ["Approve", "Decline"]}]


def test_max_taps_lists_what_it_left_out(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/again.html"], tmp_path, boxes=False, max_taps=1, settle_ms=100,
                     answers=look.Answers(slow_s=0.5))["pages"][0]
    assert [t["button"] for t in page["taps"]] == ["Send"]   # document order
    assert page["left_out"] == ["Stays", "Nothing"]
    assert "2 button(s) left out" in look.summary({"pages": [page]})[0]


def test_sheet_puts_two_looks_side_by_side(site, tmp_path):
    base, _ = site
    before, after = tmp_path / "before", tmp_path / "after"
    a = look.look(base, ["/box.html"], before, taps=False, boxes=False, settle_ms=100)
    b = look.look(base, ["/box.html"], after, taps=False, boxes=False, settle_ms=100)
    written = look.sheet(a, b, tmp_path / "sheets", before_dir=before, after_dir=after)
    assert written == ["box-html-sheet.png"]
    assert (tmp_path / "sheets" / "box-html-sheet.png").stat().st_size > 1000


def test_the_command_writes_the_report(site, tmp_path, capsys):
    base, _ = site
    assert look.main([base, "/empty.html", "--out", str(tmp_path), "--no-taps", "--settle-ms", "100"]) == 0
    out = capsys.readouterr().out
    assert "/empty.html: 2 picture(s), 0 tap(s), empty 1" in out
    assert str(tmp_path / "report.json") in out


def test_text_clipped_by_its_button_is_cut_off(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/clip.html"], tmp_path, taps=False, boxes=False, settle_ms=100)["pages"][0]
    cut = [f["detail"] for f in page["findings"] if f["kind"] == "cut_off"]
    assert any("Hidden label" in d for d in cut), f"a span's long label in a fixed-width button: {cut}"
    assert any("Flex label hidden away" in d for d in cut), f"a flex row that hides its overflow: {cut}"
    assert any("Flex label running over" in d and "runs over its button" in d for d in cut), \
        f"a label that runs over a button that shows its overflow: {cut}"
    assert not any(d.startswith("Fits") for d in cut), "a button whose label fits is not cut off"


@pytest.mark.parametrize("path", ["/popup.html", "/sw.html", "/ws.html"])
def test_a_popup_a_service_worker_and_a_websocket_never_reach_the_server(site, tmp_path, path):
    base, posts = site
    posts.clear()
    report = look.look(base, [path], tmp_path, boxes=False, settle_ms=300, answers=look.Answers(slow_s=0.5))
    page = report["pages"][0]
    assert page["taps"], f"the page's buttons were tapped: {page}"
    assert posts == [], f"nothing other than a GET reached the server: {posts}"
    if path == "/popup.html":
        sent = [u for t in page["taps"] for u in t["sent"]]
        assert any(u.endswith("/act-form") for u in sent) and any(u.endswith("/act-popup") for u in sent), \
            f"the popup's posts were answered by look: {sent}"


def test_a_button_that_moved_on_a_fresh_load_is_not_tapped(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/moving.html"], tmp_path, boxes=False, settle_ms=100)["pages"][0]
    assert [t["button"] for t in page["taps"]] == ["Alpha", "Beta"]
    assert all(t.get("skipped") and not t["pictures"] for t in page["taps"]), page["taps"]
    assert "2 tap(s) skipped (the button moved)" in look.summary({"pages": [page]})[0]
