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
}


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

    def press(self, key: str) -> None:
        spec = _KEYS[key]
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
            "app with scripts/start-tv-desktop.ps1 (it must be started with "
            "--remote-debugging-port; note 9222 may be taken by another tool - the "
            "launcher defaults to 9223, set TV_CDP_URL to match)."
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


@contextmanager
def cdp_page(cdp_url: str):
    """Yield the visible TradingView chart page (first chart tab as fallback)."""
    charts = _chart_targets(cdp_url)
    chosen, cdp = None, None
    try:
        for t in charts:
            c = _Cdp(t["webSocketDebuggerUrl"])
            page = DesktopPage(c)
            try:
                status = page.eval(_STATUS_JS) or {}
            except ToolError:
                c.close()
                continue
            if status.get("visible") or t is charts[-1]:
                chosen, cdp = page, c
                break
            c.close()
        if chosen is None:  # every eval failed
            raise ToolError("Could not evaluate in any TradingView chart tab; retry.")
        yield chosen
    finally:
        if cdp is not None:
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


def set_symbol(page, tv_symbol: str) -> dict:
    """Type the symbol into TV's quick search and confirm; return new status."""
    page.press("Escape")  # close any open dialog first
    page.type_text(tv_symbol)
    time.sleep(0.8)  # let the symbol-search overlay resolve the ticker
    page.press("Enter")
    time.sleep(1.5)  # chart reload
    return read_status(page)


def set_timeframe(page, canonical_tf: str) -> dict:
    """Type the interval quick-key (e.g. '15', '240', '1D') and confirm."""
    key = _TF_KEYS[canonical_tf]
    page.press("Escape")
    page.type_text(key)
    time.sleep(0.5)
    page.press("Enter")
    time.sleep(1.0)
    return read_status(page)


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
