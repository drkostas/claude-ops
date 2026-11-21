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
    # Start posts, takes itself away on the answer, then reads the record again, which still offers it, since an answer
    # look gives changed nothing there
    "reread.html": """<!doctype html><meta name=viewport content="width=device-width"><body>
<div id=row></div>
<script>
function draw(offer) {
  document.getElementById('row').innerHTML = offer ? '<button id=start>Start</button>' : '<div>starting</div>';
  if (offer) document.getElementById('start').onclick = () => {
    fetch('/act', {method: 'POST', body: '{}'}).then(r => r.json()).then(j => {
      draw(!j.ok);
      fetch('/state.json').then(r => r.json()).then(s => draw(s.offer));
    });
  };
}
draw(true);
</script></body>""",
    "state.json": '{"offer": true}',
    # a menu laid over the page, whose last item and a button of the page behind it cross at the bottom edge, so the
    # middle of where they cross is just below the screen; and two buttons that cross there with nothing over them
    "drawer.html": """<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0;height:2000px">
<button style="position:absolute;top:calc(100vh - 20px);left:100px;width:200px;height:40px">Restart</button>
<button style="position:absolute;top:calc(100vh - 20px);left:330px;width:50px;height:40px">Edge one</button>
<button style="position:absolute;top:calc(100vh - 10px);left:340px;width:40px;height:40px">Edge two</button>
<div style="position:fixed;top:0;left:0;width:300px;bottom:0;background:#123;overflow:hidden">
<div role=button style="position:absolute;top:calc(100vh - 25px);left:10px;width:250px;height:50px;color:#fff">Work in progress</div>
</div></body>""",
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
    # fills laid over buttons: one drawn short (an SVG at 90% of the width and a third of the height), one layer
    # with a gradient a quarter as tall as its button, and ones that are right or are no fill at all (a full
    # fill, a chosen chip's fill, an icon in the label's line, a dot in a corner, a bar that is no button)
    "fills.html": """<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0;font:14px sans-serif">
<div role=button aria-label="Short fill" style="position:relative;overflow:hidden;width:200px;height:44px;margin:10px;border-radius:22px">
<div style="position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none"><svg width="180" height="15"><rect width="180" height="15" fill="#95f"/></svg></div>
<span style="position:relative">Short fill</span></div>
<div role=button style="position:relative;width:200px;height:44px;margin:10px">
<div style="position:absolute;left:0;top:0;width:200px;height:11px;background:linear-gradient(#95f,#e5a)"></div>Thin layer</div>
<div role=button style="position:relative;overflow:hidden;width:200px;height:44px;margin:10px;border-radius:22px">
<div style="position:absolute;top:0;left:0;right:0;bottom:0"><svg width="200" height="44"><rect width="200" height="44" rx="22" fill="#95f"/></svg></div>
<span style="position:relative">Full fill</span></div>
<div role=button aria-selected=true style="position:relative;overflow:hidden;width:80px;height:30px;margin:10px">
<div style="position:absolute;inset:0;background:linear-gradient(90deg,#95f,#e5a)"></div><span style="position:relative">Chosen</span></div>
<button style="position:relative;width:120px;height:40px;margin:10px"><svg width="16" height="16"><circle cx="8" cy="8" r="8"/></svg> Icon</button>
<button style="position:relative;width:120px;height:40px;margin:10px">Badge<span style="position:absolute;top:2px;right:2px;width:8px;height:8px;background:red;border-radius:4px"></span></button>
<div style="position:relative;width:200px;height:10px;margin:10px;background:#333"><div style="position:absolute;left:0;top:0;bottom:0;width:30%;background:#95f"></div></div>
</body>""",
    # a sheet that a tap opens, whose Send has its fill drawn a third as tall as itself
    "sheet-opens.html": """<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0;font:14px sans-serif">
<button id=ask>Ask</button>
<div id=sheet hidden><div role=button style="position:relative;overflow:hidden;width:160px;height:42px;margin:10px">
<div style="position:absolute;top:0;left:0;right:0;bottom:0"><svg width="144" height="14"><rect width="144" height="14" fill="#95f"/></svg></div>
<span style="position:relative">Send</span></div></div>
<script>document.getElementById('ask').onclick = () => { document.getElementById('sheet').hidden = false; };</script>
</body>""",
    # a sheet with an opaque background laid over two buttons, one of them with a short fill, and a layer with no
    # background holding a button over another button, so both of those are seen
    "covered.html": """<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0;font:14px sans-serif">
<button style="position:absolute;top:20px;left:10px;width:150px;height:40px">Under</button>
<div role=button style="position:absolute;top:80px;left:10px;width:200px;height:44px;overflow:hidden">
<div style="position:absolute;inset:0;pointer-events:none"><svg width="180" height="15"><rect width="180" height="15" fill="#95f"/></svg></div>
<span style="position:relative">Fill under the sheet</span></div>
<button style="position:absolute;top:200px;left:10px;width:150px;height:40px">Beneath</button>
<button style="position:absolute;top:100px;left:250px;width:40px;height:30px;padding:0;border:0;border-radius:20px;overflow:hidden">Dot</button>
<div style="position:fixed;left:0;right:0;top:0;height:150px;background:#223">
<button style="position:absolute;top:25px;left:40px;width:150px;height:40px">Over</button>
<button style="position:absolute;top:120px;left:286px;width:100px;height:28px">Over a corner</button></div>
<div style="position:fixed;left:0;right:0;top:180px;height:80px">
<button style="position:absolute;top:25px;left:40px;width:150px;height:40px">Floating</button></div>
</body>""",
    # taps that hand the person elsewhere (a new tab, an app that takes a link's scheme, a file picker) and one that
    # does nothing at all
    "elsewhere.html": """<!doctype html><meta name=viewport content="width=device-width"><body>
<button onclick="window.open('/faults.html', '_blank', 'noopener')">New tab</button>
<button onclick="window.open('vnc://192.0.2.1', '_blank', 'noopener')">Screen</button>
<button onclick="location.href = 'vnc://192.0.2.1'">Screen here</button>
<button onclick="document.getElementById('f').click()">Pick</button><input id=f type=file style="display:none">
<button>Nothing</button>
</body>""",
    # below the screen's edge: two buttons of the page on top of each other in its scroll box, and a menu's item over
    # a page's button, each in a box of its own that scrolls
    "below.html": """<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0;font:14px sans-serif">
<div style="height:100vh;overflow-y:auto"><div style="position:relative;height:1500px">
<button style="position:absolute;top:1200px;left:200px;width:150px;height:40px">Low one</button>
<button style="position:absolute;top:1210px;left:220px;width:150px;height:40px">Low two</button>
<button style="position:absolute;top:1300px;left:10px;width:120px;height:40px">Page low</button></div></div>
<div style="position:fixed;left:0;top:0;width:150px;height:100vh;overflow-y:auto;background:#112">
<div style="position:relative;height:1500px">
<button style="position:absolute;top:1305px;left:20px;width:120px;height:40px">Menu low</button></div></div>
</body>""",
    # a row of filter chips: All is chosen, and a tap on it changes nothing; Queued chooses itself
    "chips.html": """<!doctype html><meta name=viewport content="width=device-width"><body>
<div id=row></div><div id=out>all</div>
<script>
let on = 'All';
function draw() {
  document.getElementById('row').innerHTML = ['All', 'Queued'].map(w =>
    `<div role=button aria-selected=${w === on}>${w}</div>`).join(' ');
  for (const el of document.querySelectorAll('[role=button]')) el.onclick = () => {
    if (on === el.textContent) return;
    on = el.textContent; document.getElementById('out').textContent = on.toLowerCase(); draw();
  };
}
draw();
</script></body>""",
    # a page drawn the way react-native-web draws a scroll view: a box that hides what runs sideways and scrolls
    # what runs down, holding the whole page, and a row of chips that scrolls sideways and hides what runs down
    "scroller.html": """<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0;font:14px sans-serif">
<div style="height:100vh;overflow-x:hidden;overflow-y:auto">
<div style="width:300px;overflow-x:auto;overflow-y:hidden;white-space:nowrap"><span>All topics</span> <span style="display:inline-block;width:500px">and a row of many more chips</span></div>
<p style="height:3000px">A page much taller than the screen</p>
</div>
</body>""",
    # a row whose tap only changes the colour of its switch (no words change), and a button that does nothing
    "toggle.html": """<!doctype html><meta name=viewport content="width=device-width"><body style="font:14px sans-serif">
<div role=button id=row style="display:flex;justify-content:space-between;width:220px;padding:10px">Hide amounts
<span id=knob style="display:inline-block;width:40px;height:20px;border-radius:10px;background:#555"></span></div>
<button>Still</button>
<script>
document.getElementById('row').onclick = () => {
  const k = document.getElementById('knob');
  k.style.background = k.style.background === 'rgb(149, 85, 255)' ? '#555' : '#95f';
};
</script></body>""",
    # Approve stays greyed out until the box holds text
    "box.html":"""<!doctype html><meta name=viewport content="width=device-width"><body>
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
    loads = {"moving": 0, "late": 0}

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
            if self.path == "/late.html":
                # the first load draws Gamma at once, every later load a second and a half after it loaded, as a
                # page does whose data comes slowly
                loads["late"] += 1
                wait = 0 if loads["late"] == 1 else 1500
                body = ("<!doctype html><body><div id=out>ready</div><script>setTimeout(() => {"
                        "const b = document.createElement('button'); b.textContent = 'Gamma';"
                        "b.onclick = () => { document.getElementById('out').textContent = 'gamma'; };"
                        f"document.body.append(b); }}, {wait});</script></body>").encode()
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


def test_a_page_that_reads_again_after_a_yes_is_pictured_before_that_read(site, tmp_path):
    # the answer was look's, so the record still offers Start, and a page that read it again drew the button back
    # and read as offering it again, in one build and not the other, by how soon its picture was taken
    base, posts = site
    report = look.look(base, ["/reread.html"], tmp_path, boxes=False, answers=look.Answers(slow_s=0.5), settle_ms=400)
    page = report["pages"][0]
    found = {(f["kind"], f["detail"]) for f in page["findings"]}
    assert ("offered_again", "Start") not in found, found
    assert {t["button"] for t in page["taps"]} == {"Start"} and page["taps"][0]["sent"]
    assert posts == [], "no request other than GET reaches the server"


def test_where_a_menu_covers_the_part_on_the_screen_there_is_no_overlap(site, tmp_path):
    # the middle of where the two cross is below the screen, and the part on the screen is under the menu
    base, _ = site
    report = look.look(base, ["/drawer.html"], tmp_path, boxes=False, taps=False)
    overlaps = sorted(f["detail"] for f in report["pages"][0]["findings"] if f["kind"] == "overlap")
    assert overlaps == ["Edge one and Edge two"], overlaps


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


def test_a_button_a_fresh_load_draws_late_is_waited_for_and_tapped(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/late.html"], tmp_path, boxes=False, settle_ms=100)["pages"][0]
    assert [t["button"] for t in page["taps"]] == ["Gamma"]
    tap = page["taps"][0]
    assert not tap.get("skipped") and tap["pictures"].get("yes"), tap
    assert not [f for f in page["findings"] if f["kind"] == "dead_tap"], page["findings"]


def test_a_tap_that_changes_only_its_own_colour_is_not_dead(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/toggle.html"], tmp_path, boxes=False, settle_ms=100)["pages"][0]
    dead = [f["detail"] for f in page["findings"] if f["kind"] == "dead_tap"]
    assert dead == ["Still"], page["findings"]


def test_a_fill_that_stops_short_of_its_button_is_short_fill(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/fills.html"], tmp_path, taps=False, boxes=False, settle_ms=100)["pages"][0]
    short = [f["detail"] for f in page["findings"] if f["kind"] == "short_fill"]
    assert any(d.startswith("Short fill") and "90% of its width and 34% of its height" in d for d in short), short
    assert any(d.startswith("Thin layer") and "25% of its height" in d for d in short), short
    assert len(short) == 2, f"a full fill, a chosen chip's fill, an icon, a dot and a bar are no short fill: {short}"


def test_what_a_sheet_covers_is_no_overlap_and_no_short_fill(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/covered.html"], tmp_path, taps=False, boxes=False, settle_ms=100)["pages"][0]
    found = [(f["kind"], f["detail"]) for f in page["findings"]]
    assert ("overlap", "Beneath and Floating") in found, found
    assert [f for f in found if f[0] in ("overlap", "short_fill")] == [("overlap", "Beneath and Floating")], found


def test_a_tap_that_opens_a_tab_an_app_or_a_file_picker_is_not_dead(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/elsewhere.html"], tmp_path, boxes=False, settle_ms=100)["pages"][0]
    dead = [f["detail"] for f in page["findings"] if f["kind"] == "dead_tap"]
    assert dead == ["Nothing"], dead
    opened = {t["button"]: t.get("opened") for t in page["taps"]}
    assert opened["New tab"] and opened["New tab"][0].endswith("/faults.html"), opened
    assert opened["Screen"] == ["vnc://192.0.2.1"] and opened["Screen here"] == ["vnc://192.0.2.1"], opened
    assert opened["Pick"] == ["a file picker"] and opened["Nothing"] is None, opened


def test_below_the_screen_only_buttons_one_box_scrolls_overlap(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/below.html"], tmp_path, taps=False, boxes=False, settle_ms=100)["pages"][0]
    overlaps = [f["detail"] for f in page["findings"] if f["kind"] == "overlap"]
    assert overlaps == ["Low one and Low two"], overlaps


def test_a_fill_drawn_short_after_a_tap_is_found_too(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/sheet-opens.html"], tmp_path, boxes=False, settle_ms=150)["pages"][0]
    found = [(f["kind"], f["detail"], f["picture"]) for f in page["findings"] if f["kind"] == "short_fill"]
    assert len(found) == 1 and found[0][1].startswith("Send") and found[0][2].endswith("tap1-yes.png"), found


def test_a_tap_on_a_chip_already_chosen_is_not_dead(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/chips.html"], tmp_path, boxes=False, settle_ms=100)["pages"][0]
    taps = {t["button"]: t for t in page["taps"]}
    assert set(taps) == {"All", "Queued"}
    assert taps["All"].get("chosen") is True and not taps["Queued"].get("chosen")
    assert not [f for f in page["findings"] if f["kind"] == "dead_tap"], page["findings"]


def test_what_runs_the_way_a_box_scrolls_is_not_cut_off(site, tmp_path):
    base, _ = site
    page = look.look(base, ["/scroller.html"], tmp_path, taps=False, boxes=False, settle_ms=100)["pages"][0]
    assert not [f for f in page["findings"] if f["kind"] == "cut_off"], page["findings"]


def _report(*pages):
    return {"pages": [{"path": path, "findings": list(found)} for path, found in pages]}


def test_new_findings_are_what_the_later_look_has_and_the_earlier_does_not():
    before = _report(("/home", [{"kind": "dead_tap", "detail": "All", "picture": "a.png"},
                                {"kind": "short_fill", "detail": "Send (its fill covers 89% of its width and 32% of its height)",
                                 "picture": "b.png"}]),
                     ("/list", []))
    after = _report(("/home", [{"kind": "dead_tap", "detail": "All", "picture": "c.png"},
                               {"kind": "short_fill", "detail": "Send (its fill covers 90% of its width and 31% of its height)",
                                "picture": "d.png"},
                               {"kind": "short_fill", "detail": "↑ (its fill covers 90% of its width and 33% of its height)",
                                "picture": "e.png"},
                               {"kind": "short_fill", "detail": "↑ (its fill covers 90% of its width and 33% of its height)",
                                "picture": "f.png"}]),
                    ("/list", [{"kind": "overlap", "detail": "Save and Close", "picture": "g.png"}]),
                    ("/more", [{"kind": "cut_off", "detail": "A long title", "picture": "h.png"}]))
    new = look.new_findings(before, after)
    assert [(f["path"], f["kind"], f["picture"]) for f in new] == [
        ("/home", "short_fill", "e.png"), ("/list", "overlap", "g.png"), ("/more", "cut_off", "h.png")]
    assert new[-1]["new_page"] is True and "new_page" not in new[0]
    assert look.by_kind(new) == {"cut_off": 1, "overlap": 1, "short_fill": 1}
    assert look.new_findings(after, after) == []


def test_a_finding_from_a_tap_the_earlier_look_did_not_make_is_not_compared():
    def page(taps, findings):
        return {"pages": [{"path": "/work", "taps": taps, "findings": findings}]}
    before = page([{"button": "All", "skipped": "the button is gone on a fresh load", "pictures": {}},
                   {"button": "Queued", "pictures": {"yes": "q0.png"}},
                   {"button": "Send", "skipped": "the button is gone on a fresh load", "pictures": {}}], [])
    after = page([{"button": "All", "pictures": {"yes": "a.png"}}, {"button": "Queued", "pictures": {"yes": "q.png"}},
                  {"button": "Send", "pictures": {}}],
                 [{"kind": "dead_tap", "detail": "All", "picture": "a.png"},
                  {"kind": "dead_tap", "detail": "Queued", "picture": "q.png"},
                  {"kind": "dead_tap", "detail": "Send (not tappable: covered)", "picture": ""},
                  {"kind": "short_fill", "detail": "↑ (its fill covers 90%)", "picture": "work-load.png"}])
    new, unchecked = look.compare(before, after)
    assert [f["detail"] for f in new] == ["Queued", "↑ (its fill covers 90%)"], new
    assert [(f["detail"], f["not_tapped_before"]) for f in unchecked] == [
        ("All", "All"), ("Send (not tappable: covered)", "Send")], unchecked
    assert look.new_findings(before, after) == new and look.not_compared(before, after) == unchecked
    # a report without taps (an older look) compares every finding as before
    assert [f["detail"] for f in look.new_findings(_report(("/work", [])), after)] == [
        "All", "Queued", "Send (not tappable: covered)", "↑ (its fill covers 90%)"]


def test_a_finding_s_key_drops_only_a_measure_at_its_end():
    assert look.finding_key("/", {"kind": "short_fill", "detail": "Send (its fill covers 90% of its width)"}) == \
        ("/", "short_fill", "Send")
    assert look.finding_key("/", {"kind": "overlap", "detail": "Save (draft) and Close"}) == \
        ("/", "overlap", "Save (draft) and Close")


def test_look_diff_prints_the_new_findings_and_exits_1(tmp_path, capsys):
    b, a = tmp_path / "b.json", tmp_path / "a.json"
    b.write_text(json.dumps(_report(("/home", []))))
    a.write_text(json.dumps(_report(("/home", [{"kind": "short_fill", "detail": "Send (its fill covers 90%)",
                                                 "picture": "p.png"}]))))
    assert look.main(["diff", str(b), str(a)]) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "new: short_fill 1" and "/home: short_fill Send (its fill covers 90%) (p.png)" in out
    assert look.main(["diff", str(b), str(b), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"by_kind": {}, "findings": [], "not_compared": []}
    b.write_text(json.dumps({"pages": [{"path": "/home", "findings": [],
                                        "taps": [{"button": "All", "skipped": "gone", "pictures": {}}]}]}))
    a.write_text(json.dumps({"pages": [{"path": "/home", "taps": [{"button": "All", "pictures": {"yes": "t.png"}}],
                                        "findings": [{"kind": "dead_tap", "detail": "All", "picture": "t.png"}]}]}))
    assert look.main(["diff", str(b), str(a)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["new: nothing", "not compared, the earlier look did not tap their button: 1",
                   "/home: dead_tap All (t.png)"], out
