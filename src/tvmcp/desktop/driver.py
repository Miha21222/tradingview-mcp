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

    def eval(self, expr: str):
        res = self._cdp.call(
            "Runtime.evaluate", {"expression": expr, "returnByValue": True}
        )
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
