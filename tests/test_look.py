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

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def do_POST(self):   # noqa: N802
            posts.append(self.path)
            self.send_response(500)
            self.end_headers()

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
