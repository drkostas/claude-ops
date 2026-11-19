"""look — open web pages the way a person on a phone sees them, take pictures, and check them for broken layout.

A change to a web app's pages is easiest to judge from pictures of those pages, before and after the change,
and of each button's states. `look` opens each address at a phone's size (390 by 844 by default), takes a
picture once the page has loaded and one at the end of the page, and checks the page itself for the faults a
picture shows a person at once.

- `cut_off` is text that does not fit. It is an element whose own text is wider or taller than its box, a box
  that hides its overflow and holds text larger than itself (a button of fixed width with a long label inside it),
  or a tappable element whose text reaches outside its own rectangle. A box that scrolls one way and hides the
  other (a page in a scroll view, a row of chips that scrolls sideways) is measured only the way it hides. The
  detail says when the box ends the text with an ellipsis on purpose, and when the text runs over its button.
- `short_fill` is a fill that stops short of the button it is laid over. A fill is a painted layer with no text
  (an SVG, a canvas, an image, or a box with a background) set at the top left corner of the box it is laid in and
  at least half as wide or half as tall as that box. The detail says how much of the width and height it covers.
- `overlap` is two tappable elements on top of each other.
- `off_screen` is a tappable element, or an element with text, that reaches past the screen's left or right edge.
- `empty` is a page with no text and no image.
- `not_loaded` is a page that did not load within the time given.

With taps (the default), it then taps each visible button, up to `max_taps` per page, on a fresh load each
time, and takes a picture after each tap. Every request the page sends with a method other than GET is
answered by `look` itself and never reaches the server, so a tap runs nothing. Each load has a browser context
of its own, routed as a whole, so a page the tap opens (a popup, a form with target _blank) is answered the same
way, and the context and every page in it are closed after the tap. Service workers are blocked, since a request
a worker handles, or one it starts itself, passes by the routes. A WebSocket is opened against nothing, so its
messages never reach the server either, and a page that acts over a WebSocket gets no answer (its acts are not
covered). A GET does reach the server, so point `look` at a copy of the app and its data, never at the real one.
A button whose tap sends such a request is tapped once for each way of answering it.

- `yes` answers at once with a success.
- `no` answers at once with a refusal.
- `slow` holds the answer, takes a picture while the page waits, then answers yes.
- `none` never answers.

After a tap that stays on the same address, the page is checked again as it is then, so a sheet or a menu the tap
opened is checked too. A tap that sends nothing, stays on the same address and changes nothing on the page is
`dead_tap`, unless the button is already the one chosen (`aria-selected`, `aria-checked` or `aria-pressed` true,
or a checked input), as a filter chip that is on already. A button
still there, enabled and with the same words after a `yes` is `offered_again`, unless it matches `again`, a
CSS selector for buttons meant to stay. A button whose words at its place differ on a fresh load (the page
drew its buttons in another order) is not tapped, and the tap says so. Each box (an input or a text area) is shown empty and with text, and
the report lists the buttons enabled in each case, so a button meant to wait for text can be seen both ways.

The report (`report.json` in the output folder) lists each page's pictures, findings, taps and boxes, and the
buttons left out by `max_taps`, so a short run never reads as a full one. `sheet` puts two reports' pictures
of the same page side by side, one picture per page, to compare a page before and after a change.

    claude-ops look http://localhost:8081 /home /items/42 --out /tmp/look
    claude-ops look http://localhost:8081 /home --out /tmp/look --no-taps
    claude-ops look http://localhost:8081 /home --out /tmp/look --answers answers.json --again '[data-stays]'
    claude-ops look sheet /tmp/look-before/report.json /tmp/look/report.json --out /tmp/sheets

An answers file may replace any of `{"yes": {"status": 200, "body": "{}"}, "no": {"status": 409, "body":
"{}"}, "slow_s": 5}`. It needs Playwright for Python and its Chromium (`pip install 'claude-ops[look]'` and
`python -m playwright install chromium`).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: a phone's screen, in CSS pixels
VIEWPORT = (390, 844)
#: the ways a tap's request is answered, in the order they are tried
MODES = ("yes", "no", "slow", "none")
#: what can be tapped
TAPPABLE = ('button, [role="button"], a[href], input[type="submit"], input[type="button"]')
#: what can be typed into
BOXES = ('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="checkbox"])'
         ':not([type="radio"]), textarea')
#: the text typed into a box
SAMPLE_TEXT = "Example text"
#: how many buttons and boxes are tried on one page
MAX_TAPS = 8
MAX_BOXES = 3
#: how long a page has to load, and how long to wait after each load or tap for the page to settle
TIMEOUT_S = 90.0
SETTLE_MS = 800
#: how long a slow answer is held
SLOW_S = 5.0

_JSON = {"content-type": "application/json"}


@dataclass
class Answer:
    """How a request the page sends is answered."""
    status: int = 200
    body: str = "{}"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Answers:
    yes: Answer = field(default_factory=lambda: Answer(200, '{"ok": true}'))
    no: Answer = field(default_factory=lambda: Answer(409, '{"ok": false, "error": "refused"}'))
    slow_s: float = SLOW_S


def answers_from(data: dict[str, Any] | None) -> Answers:
    """Answers from a dict as an answers file holds it, the defaults where it says nothing."""
    out = Answers()
    for key in ("yes", "no"):
        given = (data or {}).get(key)
        if isinstance(given, dict):
            base = getattr(out, key)
            body = given.get("body", base.body)
            setattr(out, key, Answer(int(given.get("status", base.status)),
                                     body if isinstance(body, str) else json.dumps(body),
                                     dict(given.get("headers") or {})))
    if (data or {}).get("slow_s") is not None:
        out.slow_s = float(data["slow_s"])
    return out


def slug(path: str) -> str:
    """A file name part for a page's address: `/items/42?at=end` gives `items-42-at-end`."""
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-") or "root"


# the page's own checks: a list of {kind, detail}
CHECKS_JS = r"""
(sel) => {
  const W = document.documentElement.clientWidth || window.innerWidth;
  const out = [];
  const shown = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0;
  };
  const words = el => ((el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.tagName) + '')
    .trim().replace(/\s+/g, ' ').slice(0, 60);
  // an element in a box that scrolls sideways (a row of chips, a carousel) may reach past the edge on purpose
  const scrolls = el => {
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (/(auto|scroll)/.test(s.overflowX) && a.scrollWidth > a.clientWidth) return true;
    }
    return false;
  };
  const outside = el => { const r = el.getBoundingClientRect(); return (r.left < -1 || r.right > W + 1) && !scrolls(el); };
  for (const el of document.querySelectorAll('body *')) {
    if (!shown(el)) continue;
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own) continue;
    const s = getComputedStyle(el);
    if (outside(el)) out.push({kind: 'off_screen', detail: words(el)});
    if (el.clientWidth === 0 || /(auto|scroll)/.test(s.overflowX + ' ' + s.overflowY)) continue;
    if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) {
      const meant = s.textOverflow === 'ellipsis' || (s.webkitLineClamp && s.webkitLineClamp !== 'none');
      out.push({kind: 'cut_off', detail: words(el) + (meant ? ' (ends with an ellipsis)' : '')});
    }
  }
  // a box that hides its overflow and holds text larger than itself (a fixed-width button with a long label in a
  // span, the way react-native-web draws a Text inside a Pressable). Only a direction the box hides counts: a box
  // that scrolls down and hides what runs sideways (react-native-web's scroll view, holding a whole page) keeps the
  // text below the screen within reach, and so does a row of chips that scrolls sideways.
  const clips = s => /(hidden|clip)/.test(s.overflowX + ' ' + s.overflowY);
  const hides = v => /(hidden|clip)/.test(v);
  const ellipsis = s => s.textOverflow === 'ellipsis' || (s.webkitLineClamp && s.webkitLineClamp !== 'none');
  for (const el of document.querySelectorAll('body *')) {
    if (!shown(el) || el.clientWidth === 0) continue;
    const s = getComputedStyle(el);
    if (!clips(s) || !(el.innerText || '').trim()) continue;
    if ((hides(s.overflowX) && el.scrollWidth > el.clientWidth + 1) ||
        (hides(s.overflowY) && el.scrollHeight > el.clientHeight + 1))
      out.push({kind: 'cut_off', detail: words(el) + (ellipsis(s) ? ' (ends with an ellipsis)' : '')});
  }
  const taps = [...document.querySelectorAll(sel)].filter(shown);
  for (const el of taps) if (outside(el)) out.push({kind: 'off_screen', detail: words(el)});
  // a tappable element that shows its overflow, whose text reaches outside its own rectangle
  for (const el of taps) {
    if (clips(getComputedStyle(el)) || !(el.innerText || '').trim()) continue;
    const range = document.createRange();
    range.selectNodeContents(el);
    const t = range.getBoundingClientRect(), r = el.getBoundingClientRect();
    if (t.width && (t.left < r.left - 1 || t.right > r.right + 1 || t.top < r.top - 1 || t.bottom > r.bottom + 1))
      out.push({kind: 'cut_off', detail: words(el) + ' (runs over its button)'});
  }
  for (let i = 0; i < taps.length; i++) for (let j = i + 1; j < taps.length; j++) {
    const a = taps[i], b = taps[j];
    if (a.contains(b) || b.contains(a)) continue;
    const p = a.getBoundingClientRect(), q = b.getBoundingClientRect();
    const w = Math.min(p.right, q.right) - Math.max(p.left, q.left), h = Math.min(p.bottom, q.bottom) - Math.max(p.top, q.top);
    if (w > 1 && h > 1) out.push({kind: 'overlap', detail: words(a) + ' and ' + words(b)});
  }
  // a fill is a painted layer laid over a button (an SVG, a canvas, an image, or a box with a background), with no
  // text, set at the top left corner of the box it is laid in and as wide or as tall as half of it at least. It is
  // there to cover that box, so a fill that stops short of the right or the bottom edge is drawn wrong.
  const alpha = c => { const m = /rgba?\(([^)]*)\)/.exec(c || ''); if (!m) return c === 'transparent' ? 0 : 1;
                       const p = m[1].split(/[\s,/]+/).filter(Boolean); return p.length > 3 ? parseFloat(p[3]) : 1; };
  const painted = el => {
    if (/^(svg|canvas|img|video)$/i.test(el.tagName)) return true;
    const s = getComputedStyle(el);
    return s.backgroundImage !== 'none' || alpha(s.backgroundColor) > 0;
  };
  const pct = (a, b) => Math.round(100 * a / b);
  for (const t of taps) {
    for (const el of t.querySelectorAll('*')) {
      const drawing = el.closest('svg');
      if ((drawing && drawing !== el) || el.closest(sel) !== t || !shown(el) || !painted(el)) continue;
      if ((el.textContent || '').trim()) continue;   // a face that holds the label, not a layer under it
      let laid = el;
      while (laid && laid !== t && getComputedStyle(laid).position !== 'absolute') laid = laid.parentElement;
      if (!laid || laid === t) continue;   // drawn in the label's line (an icon), not laid over the button
      const frame = laid.offsetParent && t.contains(laid.offsetParent) ? laid.offsetParent : t;
      const b = frame.getBoundingClientRect(), e = el.getBoundingClientRect();
      if (Math.abs(e.left - b.left) > 2 || Math.abs(e.top - b.top) > 2) continue;   // a dot or a mark, not a fill
      if (e.width < b.width / 2 && e.height < b.height / 2) continue;
      if (e.right < b.right - 2 || e.bottom < b.bottom - 2)
        out.push({kind: 'short_fill', detail: words(t) + ` (its fill covers ${pct(e.width, b.width)}% of its width` +
                                              ` and ${pct(e.height, b.height)}% of its height)`});
    }
  }
  const text =(document.body && document.body.innerText || '').trim();
  const pictures = [...document.querySelectorAll('img, svg, canvas, video')].filter(shown);
  if (!text && !pictures.length) out.push({kind: 'empty', detail: 'no text and no image'});
  return out;
}
"""

# the visible tappable elements, in document order: [{i, words, enabled}], i being the index among all matches
BUTTONS_JS = r"""
([sel, again]) => [...document.querySelectorAll(sel)].map((el, i) => {
  const r = el.getBoundingClientRect(), s = getComputedStyle(el);
  const shown = r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  const words = ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').trim().replace(/\s+/g, ' ').slice(0, 60);
  const enabled = !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  const chosen = ['aria-selected', 'aria-checked', 'aria-pressed'].some(a => el.getAttribute(a) === 'true') || el.checked === true;
  return {i, words, enabled, shown, chosen, stays: !!(again && el.matches(again))};
}).filter(b => b.shown)
"""

# what the page shows, to tell whether a tap changed it
STATE_JS = r"""
(sel) => [location.href, (document.body && document.body.innerText || ''),
          [...document.querySelectorAll(sel)].map(el => !el.disabled && el.getAttribute('aria-disabled') !== 'true').join(',')].join('\n')
"""

# scroll the page and every scrolling box in it to its end
END_JS = r"""
() => {
  window.scrollTo(0, document.documentElement.scrollHeight);
  for (const el of document.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight;
  }
}
"""


class _Held:
    """The requests a page sent that `look` answers, with the ones it holds."""

    def __init__(self, answers: Answers, mode: str):
        self.answers, self.mode = answers, mode
        self.sent: list[str] = []
        self.held: list[Any] = []
        self.sockets: list[str] = []
        self.closing = False

    def socket(self, ws) -> None:
        """A WebSocket the page opens: never connected to the server, so what the page sends on it goes nowhere."""
        self.sockets.append(ws.url)

    def __call__(self, route, request) -> None:
        if self.closing:
            route.abort()
            return
        if request.method in ("GET", "HEAD"):
            route.continue_()
            return
        origin = request.headers.get("origin") or "*"
        cors = {"access-control-allow-origin": origin, "access-control-allow-credentials": "true",
                "access-control-allow-headers": request.headers.get("access-control-request-headers") or "*",
                "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS"}
        if request.method == "OPTIONS":
            route.fulfill(status=204, headers=cors)
            return
        self.sent.append(f"{request.method} {request.url}")
        if self.mode in ("slow", "none"):
            self.held.append((route, cors))
            return
        self.answer(route, cors, self.answers.yes if self.mode == "yes" else self.answers.no)

    @staticmethod
    def answer(route, cors: dict[str, str], a: Answer) -> None:
        try:
            route.fulfill(status=a.status, body=a.body, headers={**_JSON, **cors, **a.headers})
        except Exception:   # noqa: BLE001 the page went away before the answer
            pass

    def release(self) -> None:
        for route, cors in self.held:
            self.answer(route, cors, self.answers.yes)
        self.held = []

    def drop(self) -> None:
        """End every held request before its page closes. Without it, a request held on a page that was then
        closed was seen to reach the server, which is the one thing a tap here must never do."""
        for route, _ in self.held:
            try:
                route.abort()
            except Exception:   # noqa: BLE001
                pass
        self.held = []


def _close(page, held: _Held) -> None:
    """End the load: its held requests first, then its context, which closes every page a tap opened in it."""
    held.closing = True
    held.drop()
    page.context.close()


def _open(browser, url: str, held: _Held, *, viewport: tuple[int, int], timeout_s: float, settle_ms: int):
    """A page of its own context, routed as a whole (a popup's first request is not seen by a page's routes), with
    service workers blocked (a request a worker handles passes by the routes) and WebSockets opened against
    nothing."""
    context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]}, device_scale_factor=2,
                                  is_mobile=True, has_touch=True, service_workers="block")
    try:
        context.route("**/*", held)
        context.route_web_socket(re.compile(".*"), held.socket)
        page = context.new_page()
        page.goto(url, wait_until="load", timeout=timeout_s * 1000)
    except Exception:
        context.close()
        raise
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_s, 15) * 1000)
    except Exception:   # noqa: BLE001 a page that keeps a request open never goes idle
        pass
    page.wait_for_timeout(settle_ms)
    return page


def _add(found: list[dict], new: list[dict], picture: str) -> None:
    seen = {(f["kind"], f["detail"]) for f in found}
    for f in new:
        if (f["kind"], f["detail"]) not in seen:
            seen.add((f["kind"], f["detail"]))
            found.append({**f, "picture": picture})


def look_page(browser, base: str, path: str, out: Path, *, answers: Answers, taps: bool = True,
              boxes: bool = True, max_taps: int = MAX_TAPS, again: str = "", viewport: tuple[int, int] = VIEWPORT,
              timeout_s: float = TIMEOUT_S, settle_ms: int = SETTLE_MS) -> dict[str, Any]:
    """Look at one page with an open browser, each load in a context of its own, and write its pictures into
    `out`."""
    url = base.rstrip("/") + "/" + path.lstrip("/") if not path.startswith("http") else path
    name = slug(path)
    rec: dict[str, Any] = {"path": path, "url": url, "pictures": {}, "findings": [], "taps": [], "boxes": [],
                           "left_out": []}

    def shot(page, label: str) -> str:
        file = out / f"{name}-{label}.png"
        page.screenshot(path=str(file))
        rec["pictures"][label] = file.name
        return file.name

    held = _Held(answers, "yes")
    try:
        page = _open(browser, url, held, viewport=viewport, timeout_s=timeout_s, settle_ms=settle_ms)
    except Exception as e:   # noqa: BLE001
        rec["findings"].append({"kind": "not_loaded", "detail": str(e).splitlines()[0][:200], "picture": ""})
        return rec
    pic = shot(page, "load")
    _add(rec["findings"], page.evaluate(CHECKS_JS, TAPPABLE), pic)
    page.evaluate(END_JS)
    page.wait_for_timeout(min(settle_ms, 400))
    pic = shot(page, "end")
    _add(rec["findings"], page.evaluate(CHECKS_JS, TAPPABLE), pic)
    buttons = page.evaluate(BUTTONS_JS, [TAPPABLE, again or None])
    _close(page, held)

    if boxes:
        held = _Held(answers, "yes")
        page = _open(browser, url, held, viewport=viewport, timeout_s=timeout_s, settle_ms=settle_ms)
        count = min(page.locator(BOXES).count(), MAX_BOXES)
        _close(page, held)
        for i in range(count):
            held = _Held(answers, "yes")
            page = _open(browser, url, held, viewport=viewport, timeout_s=timeout_s, settle_ms=settle_ms)
            box = page.locator(BOXES).nth(i)
            if not box.is_visible():
                _close(page, held)
                continue
            label = (box.get_attribute("placeholder") or box.get_attribute("aria-label") or f"box {i + 1}")
            empty = [b["words"] for b in page.evaluate(BUTTONS_JS, [TAPPABLE, None]) if b["enabled"]]
            box.fill(SAMPLE_TEXT)
            page.wait_for_timeout(min(settle_ms, 400))
            pic = shot(page, f"box{i + 1}")
            typed = [b["words"] for b in page.evaluate(BUTTONS_JS, [TAPPABLE, None]) if b["enabled"]]
            rec["boxes"].append({"box": label, "picture": pic, "enabled_empty": empty, "enabled_text": typed})
            _close(page, held)

    if taps:
        tried = [b for b in buttons if b["enabled"]][:max_taps]
        rec["left_out"] = [b["words"] for b in buttons if b["enabled"]][max_taps:]
        for n, b in enumerate(tried, 1):
            rec["taps"].append(_tap(browser, url, b, n, rec, shot, answers=answers, again=again,
                                    viewport=viewport, timeout_s=timeout_s, settle_ms=settle_ms))
    return rec


def _tap(browser, url: str, button: dict, n: int, rec: dict, shot, *, answers: Answers, again: str,
         viewport: tuple[int, int], timeout_s: float, settle_ms: int) -> dict[str, Any]:
    tap: dict[str, Any] = {"button": button["words"], "sent": [], "pictures": {}}
    for mode in MODES:
        held = _Held(answers, mode)
        page = _open(browser, url, held, viewport=viewport, timeout_s=timeout_s, settle_ms=settle_ms)
        href = page.url
        now = next((b["words"] for b in page.evaluate(BUTTONS_JS, [TAPPABLE, None]) if b["i"] == button["i"]), None)
        if now != button["words"]:
            # the page drew its buttons in another order this time, so this place holds another button, and a
            # picture filed under this one would show that one
            tap["skipped"] = (f"the button at its place reads {now!r} on a fresh load" if now is not None
                              else "the button is gone on a fresh load")
            _close(page, held)
            return tap
        before = page.evaluate(STATE_JS, TAPPABLE)
        try:
            page.locator(TAPPABLE).nth(button["i"]).click(timeout=5000)
        except Exception as e:   # noqa: BLE001
            _add(rec["findings"], [{"kind": "dead_tap", "detail": f"{button['words']} (not tappable: "
                                    f"{str(e).splitlines()[0][:120]})"}], "")
            _close(page, held)
            return tap
        if mode == "slow":
            page.wait_for_timeout(1000)
            tap["pictures"]["slow-waiting"] = shot(page, f"tap{n}-slow-waiting")
            page.wait_for_timeout(max(0, answers.slow_s * 1000 - 1000))
            held.release()
        elif mode == "none":
            page.wait_for_timeout(answers.slow_s * 1000)
        page.wait_for_timeout(settle_ms)
        tap["pictures"][mode] = shot(page, f"tap{n}-{mode}")
        if mode == "yes" and page.url == href:
            # what the tap opened (a sheet, a menu) is checked like the page itself
            _add(rec["findings"], page.evaluate(CHECKS_JS, TAPPABLE), tap["pictures"]["yes"])
        if mode == "yes":
            tap["sent"] = held.sent
            if not held.sent:
                if page.url == href and page.evaluate(STATE_JS, TAPPABLE) == before:
                    if button.get("chosen"):
                        tap["chosen"] = True   # it is already the one chosen, so a tap on it changes nothing
                    else:
                        _add(rec["findings"], [{"kind": "dead_tap", "detail": button["words"]}],
                             tap["pictures"]["yes"])
                _close(page, held)
                return tap   # nothing was sent, so the other answers would show the same
            same = [b for b in page.evaluate(BUTTONS_JS, [TAPPABLE, again or None])
                    if b["words"] == button["words"] and b["enabled"] and not b["stays"]]
            if same and page.url == href and button["words"] and not button.get("stays"):
                _add(rec["findings"], [{"kind": "offered_again", "detail": button["words"]}],
                     tap["pictures"]["yes"])
        _close(page, held)
    return tap


def look(base: str, paths: list[str], out: str | Path, *, answers: Answers | None = None, taps: bool = True,
         boxes: bool = True, max_taps: int = MAX_TAPS, again: str = "", viewport: tuple[int, int] = VIEWPORT,
         timeout_s: float = TIMEOUT_S, settle_ms: int = SETTLE_MS) -> dict[str, Any]:
    """Look at each page of `base`, write the pictures and report.json into `out`, and return the report."""
    from playwright.sync_api import sync_playwright   # noqa: PLC0415 only this command needs it

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    answers = answers or Answers()
    report: dict[str, Any] = {"base": base, "viewport": list(viewport), "pages": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for path in paths:
                report["pages"].append(look_page(browser, base, path, out, answers=answers, taps=taps,
                                                 boxes=boxes, max_taps=max_taps, again=again, viewport=viewport,
                                                 timeout_s=timeout_s, settle_ms=settle_ms))
        finally:
            browser.close()
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def sheet(before: dict[str, Any], after: dict[str, Any], out: str | Path, *, before_dir: str | Path,
          after_dir: str | Path, titles: tuple[str, str] = ("before", "after")) -> list[str]:
    """One picture per page that both reports hold, its pictures of the same name side by side. Returns the
    files written into `out`."""
    from playwright.sync_api import sync_playwright   # noqa: PLC0415

    out, before_dir, after_dir = Path(out), Path(before_dir).resolve(), Path(after_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    earlier = {pg["path"]: pg for pg in before.get("pages", [])}
    written: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 900, "height": 600})
            for pg in after.get("pages", []):
                old = earlier.get(pg["path"])
                if not old:
                    continue
                rows = []
                for label, file in pg["pictures"].items():
                    left = old["pictures"].get(label)
                    cell = (lambda d, f: f'<img src="{(d / f).as_uri()}">' if f else "<p>no picture</p>")
                    rows.append(f"<tr><th colspan=2>{html.escape(label)}</th></tr>"
                                f"<tr><td>{cell(before_dir, left)}</td><td>{cell(after_dir, file)}</td></tr>")
                doc = out / f"{slug(pg['path'])}-sheet.html"
                doc.write_text("<!doctype html><meta charset=utf-8><style>body{font:14px sans-serif;margin:8px}"
                               "img{width:390px;border:1px solid #888}td{vertical-align:top;padding:4px}"
                               "th{text-align:left;padding-top:12px}</style>"
                               f"<h3>{html.escape(pg['path'])}</h3><table><tr><th>{html.escape(titles[0])}</th>"
                               f"<th>{html.escape(titles[1])}</th></tr>{''.join(rows)}</table>")
                page.goto(doc.resolve().as_uri())
                page.wait_for_load_state("load")
                png = out / f"{slug(pg['path'])}-sheet.png"
                page.screenshot(path=str(png), full_page=True)
                written.append(png.name)
        finally:
            browser.close()
    return written


def summary(report: dict[str, Any]) -> list[str]:
    lines = []
    for pg in report["pages"]:
        kinds: dict[str, int] = {}
        for f in pg["findings"]:
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
        said = ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "no findings"
        left = f", {len(pg['left_out'])} button(s) left out" if pg["left_out"] else ""
        skipped = sum(1 for t in pg["taps"] if t.get("skipped"))
        left += f", {skipped} tap(s) skipped (the button moved)" if skipped else ""
        lines.append(f"{pg['path']}: {len(pg['pictures'])} picture(s), {len(pg['taps'])} tap(s), {said}{left}")
    return lines


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["sheet"]:
        p = argparse.ArgumentParser(prog="claude-ops look sheet",
                                    description="put two looks' pictures of the same page side by side")
        p.add_argument("before", help="the earlier look's report.json")
        p.add_argument("after", help="the later look's report.json")
        p.add_argument("--out", required=True, help="the folder for the sheets")
        a = p.parse_args(argv[1:])
        b, c = Path(a.before), Path(a.after)
        for name in sheet(json.loads(b.read_text()), json.loads(c.read_text()), a.out,
                          before_dir=b.parent, after_dir=c.parent):
            print(Path(a.out) / name)
        return 0
    p = argparse.ArgumentParser(prog="claude-ops look",
                                description="open pages at a phone's size, take pictures, show each button's "
                                            "states and check for broken layout")
    p.add_argument("base", help="the web app's address, such as http://localhost:8081")
    p.add_argument("paths", nargs="+", help="the pages to look at, such as /home")
    p.add_argument("--out", required=True, help="the folder for the pictures and report.json")
    p.add_argument("--no-taps", action="store_true", help="take only the load and end pictures")
    p.add_argument("--no-boxes", action="store_true", help="do not type into the boxes")
    p.add_argument("--max-taps", type=int, default=MAX_TAPS, help="buttons tapped on one page")
    p.add_argument("--again", default="", help="a CSS selector for buttons meant to stay after a tap")
    p.add_argument("--answers", help="a JSON file with the yes and no answers and slow_s")
    p.add_argument("--timeout", type=float, default=TIMEOUT_S, help="seconds a page has to load")
    p.add_argument("--settle-ms", type=int, default=SETTLE_MS, help="wait after each load or tap")
    p.add_argument("--viewport", default=f"{VIEWPORT[0]}x{VIEWPORT[1]}", help="width x height")
    a = p.parse_args(argv)
    w, _, h = a.viewport.partition("x")
    answers = answers_from(json.loads(Path(a.answers).read_text()) if a.answers else None)
    report = look(a.base, a.paths, a.out, answers=answers, taps=not a.no_taps, boxes=not a.no_boxes,
                  max_taps=a.max_taps, again=a.again, viewport=(int(w), int(h)), timeout_s=a.timeout,
                  settle_ms=a.settle_ms)
    for line in summary(report):
        print(line)
    print(Path(a.out) / "report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
