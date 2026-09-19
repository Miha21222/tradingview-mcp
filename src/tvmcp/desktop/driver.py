"""Raw-CDP driver for TradingView Desktop.

Playwright's `connect_over_cdp` hangs on TradingView's Electron browser target
(observed 2026-08-26: ws connects, attach handshake never completes - a known
Electron limitation), so this driver speaks the DevTools protocol directly over
`websocket-client`: `/json/list` to find the chart page target, then
Runtime.evaluate / Input.dispatchKeyEvent / Page.captureScreenshot on its page
websocket. No browser-level attach involved.

All text read from the TradingView UI (symbol, interval, titles) is untrusted
input: returned as data, never interpreted.
"""

from __future__ import annotations

import base64
import json
import time
from contextlib import contextmanager

from fastmcp.exceptions import ToolError

# TradingView canonical timeframe -> the string TV's interval quick-type accepts
_TF_KEYS = {
    "M1": "1",
    "M5": "5",
    "M15": "15",
    "M30": "30",
    "H1": "60",
    "H4": "240",
    "D1": "1D",
}

_STATUS_JS = """
(() => ({
  title: document.title,
  url: location.href,
  visible: document.visibilityState === 'visible',
  symbol: document.querySelector('#header-toolbar-symbol-search')?.innerText?.trim() || null,
  interval: document.querySelector('#header-toolbar-intervals [aria-checked="true"]')?.textContent?.trim()
    || document.querySelector('#header-toolbar-intervals')?.innerText?.trim()?.split('\\n')[0] || null,
}))()
"""

_KEYS = {
    "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "s": {"key": "s", "code": "KeyS", "windowsVirtualKeyCode": 83},
}
CTRL = 2  # CDP Input.dispatchKeyEvent modifier bit for Ctrl


class _Cdp:
    """Minimal synchronous CDP client bound to one page target."""

    def __init__(self, ws_url: str, timeout: float = 15.0):
        from websocket import create_connection

        self._ws = create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise ToolError(f"CDP {method} failed: {msg['error'].get('message')}")
                return msg.get("result", {})
            # interleaved events are ignored

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class DesktopPage:
    """The active TradingView chart page, driven over raw CDP."""

    def __init__(self, cdp: _Cdp):
        self._cdp = cdp

    def call(self, method: str, params: dict | None = None) -> dict:
        """Raw CDP call on this page's session (Browser.*, Page.*); ToolError on failure."""
        return self._cdp.call(method, params)

    def eval(self, expr: str, await_promise: bool = False):
        params = {"expression": expr, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        res = self._cdp.call("Runtime.evaluate", params)
        return res.get("result", {}).get("value")

    def type_text(self, text: str) -> None:
        for ch in text:
            self._cdp.call(
                "Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "key": ch}
            )
            self._cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})
            time.sleep(0.03)

    def press(self, key: str, modifiers: int = 0) -> None:
        spec = dict(_KEYS[key])
        if modifiers:
            spec["modifiers"] = modifiers
            spec.pop("text", None)  # Ctrl+key is a shortcut, not typed text
        self._cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", **spec})
        self._cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", **{k: v for k, v in spec.items() if k != "text"}})

    def screenshot(self, path) -> None:
        self._cdp.call("Page.bringToFront")
        time.sleep(0.3)
        res = self._cdp.call("Page.captureScreenshot", {"format": "png"})
        with open(path, "wb") as f:
            f.write(base64.b64decode(res["data"]))


def _chart_targets(cdp_url: str) -> list[dict]:
    import httpx

    try:
        r = httpx.get(cdp_url.rstrip("/") + "/json/list", timeout=5)
        r.raise_for_status()
        targets = r.json()
    except Exception as exc:
        raise ToolError(
            f"Cannot reach TradingView Desktop CDP at {cdp_url}: {exc}. Launch the "
            "app with tv_desktop_launch (or scripts/start-tv-desktop.ps1; it must "
            "be started with --remote-debugging-port; note 9222 is often taken by "
            "another CDP tool - the launcher defaults to 9223, set TV_CDP_URL to match)."
        ) from exc
    charts = [
        t for t in targets
        if t.get("type") == "page" and "tradingview.com/chart" in (t.get("url") or "")
    ]
    if not charts:
        raise ToolError(
            "Connected to CDP but found no TradingView chart tab. Open a chart in "
            "the app (or log in) and retry."
        )
    return charts


# --- target selection (M8) ---------------------------------------------------
#
# A layout with several chart tabs (or a stale hidden one) used to be picked by
# `document.visibilityState` alone, which lies for a minimized window and ties
# between tabs. Each chart target is now scored by a short in-page probe:
# requestAnimationFrame ticks over ~300 ms (a painting tab), TradingViewApi
# presence, a visible Monaco editor, and visibilityState. The winner is BOUND
# for `_REBIND_AFTER_S`; after that we re-score and switch only if ANOTHER tab
# paints (raf > 0) and scores strictly higher - a minimized window throttles
# every tab, and thrashing between equally-dead tabs helps nobody. Every call
# still opens a fresh websocket (the M4 model); only the target id is cached.
# Each Runtime.evaluate stays well under the 15 s websocket timeout.

_PROBE_JS = """
/*tvmcp:probe*/
(new Promise((resolve) => {
  let raf = 0;
  const t0 = performance.now();
  const tick = () => { raf++; if (performance.now() - t0 < 300) requestAnimationFrame(tick); };
  try { requestAnimationFrame(tick); } catch (e) {}
  setTimeout(() => {
    let monaco = false;
    try {
      const m = document.querySelector('.monaco-editor');
      monaco = !!(m && m.getBoundingClientRect().height > 0);
    } catch (e) {}
    resolve({raf: raf, api: !!window.TradingViewApi, monaco_visible: monaco,
             visibility: document.visibilityState});
  }, 320);
}))
"""

_REBIND_AFTER_S = 5.0
_bound: dict | None = None  # {target_id, score, at, chart_tabs} - reset in tests
_now = time.monotonic  # injectable clock for tests


def _score(probe: dict | None) -> int:
    if not probe:
        return -1
    return (
        (8 if (probe.get("raf") or 0) > 0 else 0)
        + (4 if probe.get("api") else 0)
        + (2 if probe.get("monaco_visible") else 0)
        + (1 if probe.get("visibility") == "visible" else 0)
    )


def _connect(target: dict) -> _Cdp:
    return _Cdp(target["webSocketDebuggerUrl"])


def _probe_target(target: dict) -> dict:
    """Score one chart target; a failed eval scores -1 (never raises)."""
    out = {"id": target.get("id"), "score": -1, "raf": 0}
    try:
        cdp = _connect(target)
    except Exception as exc:
        out["error"] = str(exc)
        return out
    try:
        res = DesktopPage(cdp).eval(_PROBE_JS, await_promise=True) or {}
        out.update(res)
        out["score"] = _score(res)
    except Exception as exc:
        out["error"] = str(exc)
    finally:
        cdp.close()
    return out


def _select_target(charts: list[dict]) -> dict:
    global _bound
    now = _now()
    by_id = {t.get("id"): t for t in charts}
    if _bound and _bound["target_id"] in by_id:
        if now - _bound["at"] < _REBIND_AFTER_S:
            _bound["chart_tabs"] = len(charts)
            return by_id[_bound["target_id"]]
        scored = [_probe_target(t) for t in charts]
        cur = next(s for s in scored if s["id"] == _bound["target_id"])
        best = max(scored, key=lambda s: s["score"])
        if best["id"] != cur["id"] and best["raf"] > 0 and best["score"] > cur["score"]:
            _bound = {"target_id": best["id"], "score": best["score"], "at": now,
                      "chart_tabs": len(charts)}
        else:
            _bound.update(score=cur["score"], at=now, chart_tabs=len(charts))
        return by_id[_bound["target_id"]]
    scored = [_probe_target(t) for t in charts]
    best = max(scored, key=lambda s: s["score"])  # ties -> first chart tab
    if best["score"] < 0:
        raise ToolError("Could not evaluate in any TradingView chart tab; retry.")
    _bound = {"target_id": best["id"], "score": best["score"], "at": now,
              "chart_tabs": len(charts)}
    return by_id[best["id"]]


def bound_target() -> dict | None:
    """The chart target the driver is currently bound to (for tv_desktop_status)."""
    if not _bound:
        return None
    return {"id": _bound["target_id"], "score": _bound["score"],
            "chart_tabs": _bound["chart_tabs"]}


@contextmanager
def cdp_page(cdp_url: str):
    """Yield the best-scoring TradingView chart page (bound for a few seconds)."""
    charts = _chart_targets(cdp_url)
    target = _select_target(charts)
    cdp = _connect(target)
    try:
        yield DesktopPage(cdp)
    finally:
        cdp.close()


def read_status(page) -> dict:
    status = page.eval(_STATUS_JS) or {}
    return {
        "title": status.get("title"),
        "url": status.get("url"),
        "symbol": status.get("symbol"),
        "interval": status.get("interval"),
    }


def screenshot(page, out_path) -> None:
    page.screenshot(out_path)


# --- navigation ---------------------------------------------------------------
#
# Preferred path (M7, borrowed from tradesdontlie/tradingview-mcp chart.js):
# the in-page charting API `activeChart().setSymbol(sym, {})` /
# `setResolution(res, {})`, then wait in-page until the chart reports the
# requested symbol/resolution with no loading spinner for 3 consecutive polls.
# Fallback when TradingViewApi is not exposed: the old keyboard quick-search.

_NAV_JS = """
/*tvmcp:nav*/
(new Promise(async (resolve) => {
  if (!window.TradingViewApi) return resolve({no_api: true});
  const p = __PAYLOAD__;
  const ch = window.TradingViewApi.activeChart();
  const norm = (r) => String(r || '').toUpperCase().replace(/^1([DWM])$/, '$1');
  try {
    if (p.symbol != null) ch.setSymbol(p.symbol, {});
    if (p.resolution != null) ch.setResolution(p.resolution, {});
  } catch (e) { return resolve({error: String(e)}); }
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const spinning = () => {
    const el = document.querySelector('[class*="loader"], [data-name="loading"]');
    return !!(el && el.offsetParent !== null);
  };
  const wantSym = p.symbol == null ? null : p.symbol.toUpperCase().split(':').pop();
  let last = null, stable = 0;
  const t0 = Date.now();
  while (Date.now() - t0 < p.timeout_ms) {
    await sleep(200);
    let sym, res;
    try { sym = ch.symbol(); res = ch.resolution(); } catch (e) { stable = 0; continue; }
    const ok = (wantSym == null || String(sym).toUpperCase().replace(/:/g, '').endsWith(wantSym))
      && (p.resolution == null || norm(res) === norm(p.resolution));
    if (!ok || spinning()) { stable = 0; continue; }
    const sig = sym + '|' + res;
    if (sig === last) stable++; else { stable = 0; last = sig; }
    if (stable >= 3) break;
  }
  let sym = null, res = null;
  try { sym = ch.symbol(); res = ch.resolution(); } catch (e) {}
  resolve({api_symbol: sym, api_resolution: res, ready: stable >= 3,
           waited_ms: Date.now() - t0});
}))
"""


def _nav(page, symbol: str | None, resolution: str | None, timeout_ms: int = 8000) -> dict:
    payload = {"symbol": symbol, "resolution": resolution, "timeout_ms": timeout_ms}
    res = page.eval(_NAV_JS.replace("__PAYLOAD__", json.dumps(payload)), await_promise=True)
    if not res or res.get("no_api"):
        return {"no_api": True}
    if res.get("error"):
        raise ToolError(f"TradingView chart API rejected the change: {res['error']}")
    return res


def set_symbol(page, tv_symbol: str) -> dict:
    """Switch symbol via the chart API (keyboard quick-search as fallback)."""
    nav = _nav(page, tv_symbol, None)
    if nav.get("no_api"):
        page.press("Escape")  # close any open dialog first
        page.type_text(tv_symbol)
        time.sleep(0.8)  # let the symbol-search overlay resolve the ticker
        page.press("Enter")
        time.sleep(1.5)  # chart reload
        return {"method": "keyboard", **read_status(page)}
    return {"method": "api", **nav, **read_status(page)}


def set_timeframe(page, canonical_tf: str) -> dict:
    """Switch resolution via the chart API (interval quick-type as fallback)."""
    key = _TF_KEYS[canonical_tf]
    nav = _nav(page, None, key)
    if nav.get("no_api"):
        page.press("Escape")
        page.type_text(key)
        time.sleep(0.5)
        page.press("Enter")
        time.sleep(1.0)
        return {"method": "keyboard", **read_status(page)}
    return {"method": "api", **nav, **read_status(page)}


# --- viewport (scroll to a date / set the visible range) ----------------------
#
# The chart lazy-loads ~300 bars; a range older than the loaded history clamps
# silently. So page back with `mainSeries().requestMoreData(1000)` while
# `requestMoreDataAvailable()` and the earliest loaded bar is still newer than
# `from`, then `timeScale().zoomToBarsRange` (the public `setVisibleRange`
# throws "Not implemented" in the desktop build). Borrowed from tradesdontlie
# chart.js.

# Shared history-paging helper (viewport + bars): `pageBack(ms, from, maxPages)`
# requests 1000 more bars while the earliest loaded bar is newer than `from`
# and the feed still has more; returns {pages, earliest, exhausted}.
_PAGE_BACK_JS = """
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const earliestOf = (ms) => {
    try { const b = ms.bars(); const fv = b.valueAt(b.firstIndex()); return fv ? fv[0] : null; }
    catch (e) { return null; }
  };
  const pageBack = async (ms, from, maxPages) => {
    let pages = 0, earliest = earliestOf(ms), exhausted = false;
    for (let i = 0; ms && from != null && i < maxPages; i++) {
      const b = ms.bars();
      earliest = earliestOf(ms);
      let more = true;
      try { more = ms.requestMoreDataAvailable(); } catch (e) {}
      if (earliest == null || earliest <= from) break;
      if (!more) { exhausted = true; break; }
      const before = b.firstIndex();
      try { ms.requestMoreData(1000); } catch (e) { break; }
      pages++;
      for (let j = 0; j < 20; j++) {
        await sleep(200);
        if (ms.bars().firstIndex() !== before) break;
      }
    }
    if (ms) earliest = earliestOf(ms) ?? earliest;
    return {pages: pages, earliest: earliest, exhausted: exhausted};
  };
"""

_VIEWPORT_JS = """
/*tvmcp:viewport*/
(new Promise(async (resolve) => {
  if (!window.TradingViewApi) return resolve({no_api: true});
  const p = __PAYLOAD__;
  const ch = window.TradingViewApi.activeChart();
  __PAGING__
  let ms = null;
  try { ms = ch._chartWidget.model().mainSeries(); } catch (e) {}
  const pg = await pageBack(ms, p.from, p.max_pages);
  const pages = pg.pages, earliest = pg.earliest, exhausted = pg.exhausted;
  // `setVisibleRange` is the public charting API but throws "Not implemented"
  // in the desktop build (verified 2026-09-19) - zoom by bar index instead.
  let method = 'zoomToBarsRange';
  try {
    const m = ch._chartWidget.model();
    const bars = m.mainSeries().bars();
    const fi = bars.firstIndex(), li = bars.lastIndex();
    let fromIdx = fi, toIdx = li, seenFrom = false;
    for (let i = fi; i <= li; i++) {
      const v = bars.valueAt(i);
      if (!v) continue;
      if (!seenFrom && v[0] >= p.from) { fromIdx = i; seenFrom = true; }
      if (v[0] <= p.to) toIdx = i;
    }
    m.timeScale().zoomToBarsRange(fromIdx, toIdx);
  } catch (e) {
    method = 'setVisibleRange';
    try { await ch.setVisibleRange({from: p.from, to: p.to}); }
    catch (e2) { return resolve({error: String(e) + ' / ' + String(e2)}); }
  }
  await sleep(400);
  let vis = null;
  try { vis = ch.getVisibleRange(); } catch (e) {}
  resolve({requested: {from: p.from, to: p.to}, visible: vis, method: method,
           pages_loaded: pages, earliest_loaded: earliest,
           history_exhausted: exhausted,
           clamped: earliest != null && earliest > p.from});
}))
"""


def _paging_js(template: str, payload: dict) -> str:
    return (template.replace("__PAGING__", _PAGE_BACK_JS)
            .replace("__PAYLOAD__", json.dumps(payload)))


def set_visible_range(page, from_time: int, to_time: int, max_pages: int = 25) -> dict:
    payload = {"from": from_time, "to": to_time, "max_pages": max_pages}
    res = page.eval(_paging_js(_VIEWPORT_JS, payload), await_promise=True)
    if not res or res.get("no_api"):
        raise ToolError(_NO_API)
    if res.get("error"):
        raise ToolError(f"Could not set the visible range: {res['error']}")
    return res


# --- bars (read the main series OHLCV off the live chart) ---------------------
#
# `mainSeries().bars()` rows are `[time, open, high, low, close, volume, ...]`
# (time = unix seconds). Empty/NaN cells are mapped to null explicitly because
# CDP's returnByValue drops NaN. When `since` is earlier than the earliest
# loaded bar, history is paged back with the same helper the viewport uses.

_BARS_JS = """
/*tvmcp:bars*/
(new Promise(async (resolve) => {
  if (!window.TradingViewApi) return resolve({no_api: true});
  const p = __PAYLOAD__;
  const ch = window.TradingViewApi.activeChart();
  __PAGING__
  let ms = null;
  try { ms = ch._chartWidget.model().mainSeries(); } catch (e) {}
  if (!ms) return resolve({error: 'main series is not reachable on this chart'});
  const pg = await pageBack(ms, p.since, p.max_pages);
  const fin = (x) => (typeof x === 'number' && Number.isFinite(x)) ? x : null;
  const b = ms.bars();
  const fi = b.firstIndex(), li = b.lastIndex();
  let rows = [], latest = null;
  for (let i = fi; i <= li; i++) {
    const v = b.valueAt(i);
    if (!v) continue;
    if (typeof v[0] === 'number') latest = v[0];
    if (p.since != null && v[0] < p.since) continue;
    rows.push([v[0], fin(v[1]), fin(v[2]), fin(v[3]), fin(v[4]), fin(v[5])]);
  }
  const total = rows.length;
  if (rows.length > p.count) rows = rows.slice(-p.count);
  let sym = null, res = null;
  try { sym = ch.symbol(); res = String(ch.resolution()); } catch (e) {}
  resolve({symbol: sym, resolution: res, rows: rows, total_after_since: total,
           loaded_bars: b.size(), earliest_loaded: pg.earliest, latest_loaded: latest,
           pages_loaded: pg.pages, history_exhausted: pg.exhausted,
           clamped: p.since != null && pg.earliest != null && pg.earliest > p.since});
}))
"""


def read_bars(page, count: int, since_ts: int | None = None, max_pages: int = 25) -> dict:
    """Rows `[t,o,h,l,c,v]` of the active chart's main series (newest last).

    `since_ts` (unix seconds) pages history back until that time is loaded
    (or the feed runs out - `clamped`), then keeps the last `count` rows at or
    after it. `latest_loaded` is the newest bar BEFORE the since filter, so a
    caller can tell "nothing after since" from "nothing loaded".
    """
    payload = {"count": int(count), "since": since_ts, "max_pages": max_pages}
    res = page.eval(_paging_js(_BARS_JS, payload), await_promise=True)
    if not res or res.get("no_api"):
        raise ToolError(_NO_API)
    if res.get("error"):
        raise ToolError(f"Could not read bars: {res['error']}")
    return res


def scroll_to_date(page, center_time: int, bars_each_side: int, resolution_minutes: int) -> dict:
    half = bars_each_side * resolution_minutes * 60
    res = set_visible_range(page, center_time - half, center_time + half)
    return {"centered_on": center_time, "bars_each_side": bars_each_side, **res}


# --- drawings (via the in-page charting-library API, not UI clicks) ---------
#
# TradingView Desktop exposes `window.TradingViewApi` with the charting-library
# chart API (createShape/createMultipointShape/getAllShapes/removeEntity) -
# verified live 2026-08-26. Shape ids returned by create* are opaque objects
# that don't survive `returnByValue`, so every mutation runs as one atomic JS
# block that diffs getAllShapes() before/after and returns plain string ids.
# `exportData` is NOT supported in the desktop build - price anchors come from
# the pane's price scale instead.

# kind -> (TV shape name, required point count)
DRAW_KINDS = {
    "rectangle": 2,        # FVG / order block / range box
    "trend_line": 2,
    "ray": 2,
    "horizontal_line": 1,  # level; time optional (viewport middle)
    "vertical_line": 1,    # time marker; price optional
    "text": 1,             # floating label
}

_NO_API = (
    "This TradingView Desktop page does not expose TradingViewApi (chart still "
    "loading, or a non-chart tab won). Wait for the chart to render and retry; "
    "if it persists, the app build changed and the drawing slice needs rework."
)

_LIST_JS = """
/*tvmcp:list*/
(() => {
  if (!window.TradingViewApi) return {no_api: true};
  const ch = window.TradingViewApi.activeChart();
  const out = {
    symbol: ch.symbol(),
    resolution: ch.resolution(),
    visible_time_range: ch.getVisibleRange(),
    visible_price_range: null,
  };
  try {
    out.visible_price_range =
      ch.getPanes()[0].getMainSourcePriceScale().getVisiblePriceRange();
  } catch (e) {}
  out.shapes = ch.getAllShapes().map(s => {
    let points = null, text = null;
    try { points = ch.getShapeById(s.id).getPoints(); } catch (e) {}
    try { text = ch.getShapeById(s.id).getProperties().text || null; } catch (e) {}
    return {id: s.id, name: s.name, points: points, text: text};
  });
  return out;
})()
"""

_DRAW_JS = """
/*tvmcp:draw*/
(new Promise(async (resolve) => {
  if (!window.TradingViewApi) return resolve({no_api: true});
  const p = __PAYLOAD__;
  const ch = window.TradingViewApi.activeChart();
  const vr = ch.getVisibleRange();
  let midPrice = null;
  try {
    const pr = ch.getPanes()[0].getMainSourcePriceScale().getVisiblePriceRange();
    if (pr) midPrice = (pr.from + pr.to) / 2;
  } catch (e) {}
  const pts = p.points.map(pt => ({
    time: pt.time != null ? pt.time : Math.round((vr.from + vr.to) / 2),
    price: pt.price != null ? pt.price : midPrice,
  }));
  if (pts.some(pt => pt.price == null))
    return resolve({error: 'price omitted but the pane price scale is unavailable'});
  const before = new Set(ch.getAllShapes().map(s => s.id));
  const opts = {shape: p.shape, lock: p.lock, disableSelection: false,
                overrides: p.overrides};
  if (p.text) opts.text = p.text;
  try {
    if (pts.length === 1) ch.createShape(pts[0], opts);
    else ch.createMultipointShape(pts, opts);
  } catch (e) { return resolve({error: String(e)}); }
  // getAllShapes lags creation by a tick in the desktop build - poll for the new id
  for (let i = 0; i < 20; i++) {
    const created = ch.getAllShapes().filter(s => !before.has(s.id));
    if (created.length)
      return resolve({created: created.map(s => ({id: s.id, name: s.name})),
                      points: pts});
    await new Promise(r => setTimeout(r, 100));
  }
  resolve({created: [], points: pts});
}))
"""

_REMOVE_JS = """
/*tvmcp:remove*/
(() => {
  if (!window.TradingViewApi) return {no_api: true};
  const ch = window.TradingViewApi.activeChart();
  const target = ch.getAllShapes().find(s => s.id === __ID__);
  if (!target) return {found: false, present: ch.getAllShapes().map(s => s.id)};
  let text = null;
  try { text = ch.getShapeById(target.id).getProperties().text || null; } catch (e) {}
  ch.removeEntity(target.id);
  return {found: true, id: target.id, name: target.name, text: text};
})()
"""


def _hex_to_rgba(color: str, opacity: float) -> str:
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{opacity})"


def _overrides(kind: str, color: str, fill_opacity: float) -> dict:
    if kind == "rectangle":
        return {
            "color": color,
            "backgroundColor": _hex_to_rgba(color, fill_opacity),
            "fillBackground": True,
            "linewidth": 1,
        }
    if kind == "text":
        return {"color": color}
    width = 2 if kind in ("trend_line", "ray") else 1
    return {"linecolor": color, "linewidth": width}


def list_drawings(page) -> dict:
    res = page.eval(_LIST_JS)
    if not res or res.get("no_api"):
        raise ToolError(_NO_API)
    return res


def draw(page, kind: str, points: list[dict], text: str | None,
         color: str, fill_opacity: float, lock: bool) -> dict:
    payload = {
        "shape": kind,
        "points": [{"time": p.get("time"), "price": p.get("price")} for p in points],
        "text": text,
        "lock": lock,
        "overrides": _overrides(kind, color, fill_opacity),
    }
    res = page.eval(_DRAW_JS.replace("__PAYLOAD__", json.dumps(payload)),
                    await_promise=True)
    if not res or res.get("no_api"):
        raise ToolError(_NO_API)
    if res.get("error"):
        raise ToolError(f"Drawing failed: {res['error']}")
    if not res.get("created"):
        raise ToolError(
            "TradingView accepted the call but no new drawing appeared - the "
            "shape kind may be unsupported by this app build; try another kind "
            "or verify with tv_desktop_screenshot."
        )
    return res


def remove_drawing(page, drawing_id: str) -> dict:
    res = page.eval(_REMOVE_JS.replace("__ID__", json.dumps(drawing_id)))
    if not res or res.get("no_api"):
        raise ToolError(_NO_API)
    if not res.get("found"):
        raise ToolError(
            f"No drawing with id {drawing_id!r} on the active chart. List current "
            "ids with tv_desktop_list_drawings."
        )
    return res


# --- studies (read the user's indicators; in-page study model) ---------------
#
# `getStudyById(id)` returns the charting-library IStudyApi; its private
# `_study` is the study model with `data()` (plot-value series, rows
# `[unix_time, plot0, plot1, ...]`) and `graphics()._primitivesCollection`
# (Pine box.new/line.new/label.new output). Verified live 2026-08-27.
# Landmines:
# (1) protected/invite-only Pine scripts carry their encrypted source as a
#     multi-KB hidden `text` input - inputs MUST be filtered to visible ones
#     and value strings capped, or one study blows the output budget;
# (2) dwg* collections nest as Map(name -> Map(? -> store)); the store's
#     `_primitivesDataById` Map holds the primitive dicts (box: x1/x2 bar
#     index, y1/y2 price; colors packed as ARGB uint32);
# (3) box/line x coordinates are SERVER-side graphic indexes, not client bar
#     indexes: `graphics()._indexes[x]` translates them to the client bar
#     index (-2000000 = before loaded history), and
#     `series().bars().valueAt(ti)[0]` gives the unix time; indexes with no
#     mapping are extrapolated from the tail offset + bar spacing
#     (approximate across session gaps);
# (4) plot rows hold NaN for empty values - CDP's returnByValue JSON-drops
#     NaN, so the JS maps non-finite to null explicitly.

# Shared JS helpers injected into every study block: study lookup by id or
# case-insensitive title substring, and packed-ARGB -> {hex, alpha} color.
_STUDY_HELPERS_JS = """
  const ch = window.TradingViewApi.activeChart();
  const findStudy = (q) => {
    const all = ch.getAllStudies();
    let m = all.filter(s => s.id === q);
    if (!m.length)
      m = all.filter(s => (s.name || '').toLowerCase().includes(q.toLowerCase()));
    if (m.length === 1) return m[0];
    return {__miss: true, not_found: !m.length,
            candidates: all.map(s => ({id: s.id, title: s.name}))};
  };
  const color = (v) => {
    if (v == null || typeof v !== 'number') return null;
    const a = (v >>> 24) & 255, r = (v >>> 16) & 255,
          g = (v >>> 8) & 255, b = v & 255;
    if (!a) return null;  // zero alpha = theme palette index, not a packed ARGB
    const hex = '#' + [r, g, b].map(x => x.toString(16).padStart(2, '0')).join('');
    return {hex: hex, alpha: Math.round((a / 255) * 100) / 100};
  };
  const fin = (x) => (typeof x === 'number' && Number.isFinite(x)) ? x : null;
"""

_LIST_STUDIES_JS = """
/*tvmcp:studies*/
(() => {
  if (!window.TradingViewApi) return {no_api: true};
  __HELPERS__
  const out = {symbol: ch.symbol(), resolution: ch.resolution(), studies: []};
  for (const s of ch.getAllStudies()) {
    const st = {id: s.id, title: s.name};
    try {
      const api = ch.getStudyById(s.id);
      st.visible = api.isVisible();
      st.pane = api.paneIndex();
      st.loading = api.isLoading();
      st.error = api.hasError();
      st.bars = api.dataLength();
      const model = api._study;
      const mi = model.metaInfo();
      const styles = mi.styles || {};
      st.plots = (mi.plots || []).map(p => ({
        id: p.id, type: p.type,
        title: (styles[p.id] && styles[p.id].title) || null,
      }));
      const vals = {};
      for (const v of api.getInputValues()) vals[v.id] = v.value;
      if (vals.pineId) st.pine_id = String(vals.pineId).slice(0, 80);
      const skip = {text: 1, pineId: 1, pineVersion: 1, pineFeatures: 1,
                    __profile: 1};
      st.inputs = api.getInputsInfo()
        .filter(i => !i.isHidden && !i.isFake && !skip[i.id])
        .map(i => {
          let v = vals[i.id];
          if (typeof v === 'string' && v.length > 200) v = v.slice(0, 200);
          return {id: i.id, name: i.name, type: i.type, value: fin(v) ?? v ?? null};
        });
      const counts = {};
      const countStore = (v, depth) => {
        if (!v || depth > 3) return 0;
        if (v._primitivesDataById instanceof Map) return v._primitivesDataById.size;
        if (v instanceof Map) {
          let n = 0;
          for (const x of v.values()) n += countStore(x, depth + 1);
          return n;
        }
        if (Array.isArray(v)) return v.length;
        return 0;
      };
      const pc = model.graphics()._primitivesCollection;
      for (const k of Object.keys(pc)) {
        const n = countStore(pc[k], 0);
        if (n) counts[k.replace(/^dwg/, '')] = n;
      }
      st.graphics = counts;
    } catch (e) { st.read_error = String(e).slice(0, 200); }
    out.studies.push(st);
  }
  return out;
})()
"""

_READ_PLOTS_JS = """
/*tvmcp:plots*/
(() => {
  if (!window.TradingViewApi) return {no_api: true};
  __HELPERS__
  const p = __PAYLOAD__;
  const s = findStudy(p.query);
  if (s.__miss) return s;
  const api = ch.getStudyById(s.id);
  const mi = api._study.metaInfo();
  const styles = mi.styles || {};
  const d = api._study.data();
  if (d.isEmpty()) return {id: s.id, title: s.name, rows: [], plots: [],
                           note: 'study has no data - hidden studies are ' +
                                 'unloaded by TV; toggle it visible and retry'};
  const li = d.lastIndex(), fi = d.firstIndex();
  const from = Math.max(fi, li - p.count + 1);
  const rows = [];
  for (let i = from; i <= li; i++) {
    const v = d.valueAt(i);
    if (!v) continue;
    const vals = Array.from(v).slice(1).map(fin);
    if (p.nonempty_only && !vals.some(x => x !== null)) continue;
    rows.push([v[0], ...vals]);
  }
  return {
    id: s.id, title: s.name,
    plots: (mi.plots || []).map(pl => ({
      id: pl.id, type: pl.type,
      title: (styles[pl.id] && styles[pl.id].title) || null,
    })),
    columns: ['time', ...(mi.plots || []).map(pl => pl.id)],
    total_bars: d.size(),
    rows: rows,
  };
})()
"""

_READ_GRAPHICS_JS = """
/*tvmcp:graphics*/
(() => {
  if (!window.TradingViewApi) return {no_api: true};
  __HELPERS__
  const p = __PAYLOAD__;
  const s = findStudy(p.query);
  if (s.__miss) return s;
  const model = ch.getStudyById(s.id)._study;
  let t = () => null, timeErr = null;
  try {
    const idx = model.graphics()._indexes;
    const bars = model.series().bars();
    const bfi = bars.firstIndex(), bli = bars.lastIndex();
    let span = 60;
    if (bli > bfi) {
      const a = bars.valueAt(bli), b = bars.valueAt(bli - 1);
      if (a && b) span = a[0] - b[0];
    }
    const lastX = idx.length - 1;
    const offset = (lastX >= 0 && idx[lastX] > -2000000) ? lastX - idx[lastX] : null;
    t = (x) => {
      if (x == null) return null;
      const ti = (x >= 0 && x < idx.length) ? idx[x] : null;
      if (ti != null && ti > -2000000) {
        const v = bars.valueAt(ti);
        if (v) return v[0];
      }
      if (offset === null) return null;
      const ref = bars.valueAt(bfi);
      return ref ? ref[0] + ((x - offset) - bfi) * span : null;
    };
  } catch (e) { timeErr = String(e); }
  const stores = (coll) => {
    const found = [];
    const walk = (v, depth) => {
      if (!v || depth > 3) return;
      if (v._primitivesDataById instanceof Map) { found.push(v._primitivesDataById); return; }
      if (v instanceof Map) for (const x of v.values()) walk(x, depth + 1);
    };
    walk(coll, 0);
    return found;
  };
  const pc = model.graphics()._primitivesCollection;
  const out = {id: s.id, title: s.name, counts: {}};
  if (timeErr) out.time_mapping_error = timeErr;
  if (p.compact) {
    // Compact mode (borrowed from tradesdontlie data.js): horizontal lines
    // collapse to unique price levels, boxes to unique {high, low} zones.
    const round = (v) => Math.round(v * 1e8) / 1e8;
    const lv = new Map();
    for (const st of stores(pc.dwglines)) for (const l of st.values()) {
      if (!Number.isFinite(l.y1) || l.y1 !== l.y2) continue;
      const k = round(l.y1);
      const e = lv.get(k) || {price: k, count: 0, last_time: null};
      e.count++;
      const tt = t(l.x2);
      if (tt != null && (e.last_time == null || tt > e.last_time)) e.last_time = tt;
      lv.set(k, e);
    }
    const zn = new Map();
    for (const st of stores(pc.dwgboxes)) for (const b of st.values()) {
      if (!Number.isFinite(b.y1) || !Number.isFinite(b.y2)) continue;
      const hi = round(Math.max(b.y1, b.y2)), lo = round(Math.min(b.y1, b.y2));
      const k = hi + '|' + lo;
      const e = zn.get(k) || {high: hi, low: lo, count: 0, last_time: null, text: null};
      e.count++;
      const tt = t(b.x2);
      if (tt != null && (e.last_time == null || tt > e.last_time)) e.last_time = tt;
      if (b.t && !e.text) e.text = String(b.t).slice(0, 80);
      zn.set(k, e);
    }
    let lines = 0, boxes = 0;
    for (const st of stores(pc.dwglines)) lines += st.size;
    for (const st of stores(pc.dwgboxes)) boxes += st.size;
    out.counts = {lines: lines, boxes: boxes, levels: lv.size, zones: zn.size};
    out.levels = [...lv.values()].sort((a, b) => b.price - a.price).slice(0, p.limit);
    out.zones = [...zn.values()].sort((a, b) => b.high - a.high).slice(0, p.limit);
    return out;
  }
  const kinds = p.kinds;
  const take = (name, coll, map) => {
    if (kinds && !kinds.includes(name)) return;
    let items = [];
    for (const st of stores(coll)) items.push(...st.values());
    out.counts[name] = items.length;
    items.sort((a, b) => (a.id || 0) - (b.id || 0));
    out[name] = items.slice(-p.limit).map(map);
  };
  take('boxes', pc.dwgboxes, (b) => ({
    id: b.id, time1: t(b.x1), time2: t(b.x2),
    price1: fin(b.y1), price2: fin(b.y2),
    text: b.t || null, extend: b.ex || null,
    bg_color: color(b.bc), border_color: color(b.c),
  }));
  take('lines', pc.dwglines, (l) => ({
    id: l.id, time1: t(l.x1), price1: fin(l.y1),
    time2: t(l.x2), price2: fin(l.y2),
    extend: l.ex || null, color: color(l.ci), width: l.w ?? null,
  }));
  take('labels', pc.dwglabels, (l) => ({
    id: l.id, time: t(l.x), price: fin(l.y),
    text: (l.t || '').slice(0, 200) || null, color: color(l.ci ?? l.c),
  }));
  take('polylines', pc.dwgpolylines, (l) => ({
    id: l.id,
    points: (l.points || []).slice(0, 50).map(pt => ({time: t(pt.x), price: fin(pt.y)})),
    color: color(l.ci ?? l.c),
  }));
  return out;
})()
"""


def _study_js(template: str, payload: dict | None = None) -> str:
    js = template.replace("__HELPERS__", _STUDY_HELPERS_JS)
    if payload is not None:
        js = js.replace("__PAYLOAD__", json.dumps(payload))
    return js


def _check_study_res(res, query: str | None = None):
    if not res or res.get("no_api"):
        raise ToolError(_NO_API)
    if res.get("__miss"):
        cands = ", ".join(f"{c['id']} ({c['title']})" for c in res.get("candidates", []))
        reason = "matches no study" if res.get("not_found") else "is ambiguous"
        raise ToolError(
            f"Study query {query!r} {reason} on the active chart. "
            f"Studies present: {cands or 'none'}. Pass an id or a more "
            "specific title substring (see tv_desktop_list_studies)."
        )
    return res


def list_studies(page) -> dict:
    return _check_study_res(page.eval(_study_js(_LIST_STUDIES_JS)))


def read_study_plots(page, query: str, count: int, nonempty_only: bool) -> dict:
    res = page.eval(_study_js(
        _READ_PLOTS_JS,
        {"query": query, "count": count, "nonempty_only": nonempty_only},
    ))
    return _check_study_res(res, query)


def read_study_graphics(page, query: str, limit: int, kinds: list[str] | None,
                        compact: bool = False) -> dict:
    res = page.eval(_study_js(
        _READ_GRAPHICS_JS,
        {"query": query, "limit": limit, "kinds": kinds, "compact": compact},
    ))
    return _check_study_res(res, query)


# --- study inputs (write) -----------------------------------------------------
#
# `getInputValues()` -> mutate `.value` -> `setInputValues(arr)` (charting API,
# borrowed from tradesdontlie indicators.js). Inputs may be addressed by id or
# by user-facing name (case-insensitive). The block reads the values back after
# the write and reports mismatches - TradingView's own Copilot was caught
# reporting "done" while the inputs stayed unchanged; never trust the write.

_SET_INPUTS_JS = """
/*tvmcp:inputs*/
(() => {
  if (!window.TradingViewApi) return {no_api: true};
  __HELPERS__
  const p = __PAYLOAD__;
  const s = findStudy(p.query);
  if (s.__miss) return s;
  const api = ch.getStudyById(s.id);
  const skip = {text: 1, pineId: 1, pineVersion: 1, pineFeatures: 1, __profile: 1};
  const info = api.getInputsInfo().filter(i => !i.isHidden && !i.isFake && !skip[i.id]);
  const byName = {};
  for (const i of info) { byName[i.id.toLowerCase()] = i.id; if (i.name) byName[String(i.name).toLowerCase()] = i.id; }
  const wanted = {}, unknown = [];
  for (const k of Object.keys(p.inputs)) {
    const id = byName[k.toLowerCase()];
    if (id == null) unknown.push(k); else wanted[id] = p.inputs[k];
  }
  const cur = api.getInputValues();
  const val = (v) => (typeof v === 'string' && v.length > 200) ? v.slice(0, 200) : (fin(v) ?? v ?? null);
  if (unknown.length) {
    const known = {};
    for (const v of cur) known[v.id] = v.value;
    return {id: s.id, title: s.name, unknown: unknown,
            available: info.map(i => ({id: i.id, name: i.name, type: i.type, value: val(known[i.id])}))};
  }
  const before = {};
  for (const v of cur) if (Object.prototype.hasOwnProperty.call(wanted, v.id)) {
    before[v.id] = val(v.value);
    v.value = wanted[v.id];
  }
  try { api.setInputValues(cur); } catch (e) { return {error: String(e)}; }
  const after = {};
  for (const v of api.getInputValues()) if (Object.prototype.hasOwnProperty.call(wanted, v.id)) after[v.id] = val(v.value);
  const mismatched = Object.keys(wanted).filter(k => String(after[k]) !== String(wanted[k]));
  return {id: s.id, title: s.name, before: before, after: after,
          applied: !mismatched.length, mismatched: mismatched};
})()
"""


def set_study_inputs(page, query: str, inputs: dict) -> dict:
    res = page.eval(_study_js(_SET_INPUTS_JS, {"query": query, "inputs": inputs}))
    res = _check_study_res(res, query)
    if res.get("error"):
        raise ToolError(f"TradingView rejected the input change: {res['error']}")
    if res.get("unknown"):
        avail = ", ".join(f"{a['id']} ({a['name']})" for a in res.get("available", []))
        raise ToolError(
            f"Unknown input(s) {res['unknown']} for study {res['title']!r}. "
            f"Available inputs (id (name)): {avail or 'none visible'}."
        )
    return res


_RESOLUTION_JS = """
/*tvmcp:resolution*/
(() => {
  if (!window.TradingViewApi) return {no_api: true};
  return {resolution: String(window.TradingViewApi.activeChart().resolution())};
})()
"""


def resolution_to_minutes(res: str) -> int:
    """TV resolution string ('15', '240', 'D', '1D', 'W', '1M', '30S') -> minutes."""
    r = (res or "").strip().upper()
    if r.endswith("S"):
        return max(1, int(r[:-1] or 1) // 60)
    units = {"D": 1440, "W": 10080, "M": 43200}
    if r and r[-1] in units:
        return int(r[:-1] or 1) * units[r[-1]]
    return int(r) if r.isdigit() else 60


def read_resolution_minutes(page) -> int:
    res = page.eval(_RESOLUTION_JS)
    if not res or res.get("no_api"):
        raise ToolError(_NO_API)
    return resolution_to_minutes(res.get("resolution"))
