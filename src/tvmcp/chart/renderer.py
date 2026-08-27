"""Headless PNG rendering of candles + markup via Playwright + Lightweight Charts v5.

The static page in `static/` is loaded from disk (no web server, no npm, no network
at runtime). Data is injected through `page.evaluate` as Unix-seconds (UTC) so the
rendered image is deterministic. Browser launch is per-call (simplest, no persistent
service); golden tests reuse the same path with a fixed viewport/DPR/timezone.
"""

from __future__ import annotations

import pandas as pd

from .assets import index_html
from .markup import BoxMarkup, KillzoneMarkup, LineMarkup, Markup, MarkerMarkup, TextMarkup


def _unix(value) -> int:
    t = pd.Timestamp(value)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return int(t.timestamp())


def build_spec(df: pd.DataFrame, markup: Markup, width: int, height: int) -> dict:
    """Turn a bars DataFrame + Markup into the JSON spec the browser consumes."""
    bars = [
        {
            "time": _unix(t),
            "open": round(float(o), 6),
            "high": round(float(h), 6),
            "low": round(float(lo), 6),
            "close": round(float(c), 6),
        }
        for t, o, h, lo, c in df[["time", "open", "high", "low", "close"]].itertuples(index=False)
    ]
    items = []
    for m in markup.markup:
        if isinstance(m, BoxMarkup):
            items.append(
                {"type": m.type, "time": _unix(m.time), "direction": m.direction,
                 "top": m.top, "bottom": m.bottom, "color": m.color, "label": m.label}
            )
        elif isinstance(m, LineMarkup):
            items.append({"type": m.type, "time": _unix(m.time), "level": m.level,
                          "label": m.label, "color": m.color})
        elif isinstance(m, KillzoneMarkup):
            items.append({"type": "killzone", "start": _unix(m.start), "end": _unix(m.end),
                          "label": m.label, "color": m.color})
        elif isinstance(m, TextMarkup):
            items.append({"type": "text", "time": _unix(m.time), "price": m.price,
                          "text": m.text, "color": m.color})
        elif isinstance(m, MarkerMarkup):
            items.append({"type": "marker", "time": _unix(m.time), "price": m.price,
                          "direction": m.direction, "label": m.label, "color": m.color})
    return {"bars": bars, "markup": items, "grid": markup.grid, "width": width, "height": height}


def render_png(spec: dict, out_path, dpr: float = 1.0) -> None:
    """Render `spec` to `out_path` (a PNG file) using headless Chromium."""
    from playwright.sync_api import sync_playwright

    index = index_html()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            from fastmcp.exceptions import ToolError

            raise ToolError(
                f"Headless Chromium failed to launch: {exc}. If it is not "
                "installed, run: uv run playwright install chromium "
                "(tv_setup_doctor diagnoses this and other prerequisites)."
            ) from exc
        try:
            page = browser.new_page(
                viewport={"width": spec["width"], "height": spec["height"]},
                device_scale_factor=dpr,
            )
            page.goto(index.as_uri())
            page.evaluate("__tvmcp_render", spec)
            page.wait_for_timeout(80)
            el = page.query_selector("#chart")
            el.screenshot(path=str(out_path), omit_background=False)
        finally:
            browser.close()
