"""`chart` toolset: render candles + SMC markup to a PNG via headless Chromium.

Leverages Lightweight Charts v5 (vendored static bundle) + Playwright. The tool is
opt-in behind the `chart` toolset (default off). Output PNGs land in a managed
`chart_dir` with collision-safe filenames; caller-controlled arbitrary paths are not
accepted.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, Callable

import pandas as pd
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bars import choose_provider, load_bars, window
from ..cache import BarCache
from ..config import Settings
from ..symbols import resolve, resolve_timeframe
from .markup import Markup, parse_markup
from .renderer import build_spec, render_png

_MAX_RENDER_BARS = 500
_MIN_RENDER_BARS = 20
_MAX_DIM = 2000
_MIN_DIM = 200


def _load(settings: Settings, cache: BarCache, symbol: str, timeframe: str, count: int, provider: str):
    sym = resolve(symbol)
    tf = resolve_timeframe(timeframe)
    end_ts = pd.Timestamp.now(tz="UTC")
    start_ts, _ = window(count, tf.minutes, end_ts)
    df = load_bars(settings, cache, sym, tf, start_ts, end_ts, count, provider)
    return sym, tf, df


def _validate_times(markup: Markup, first_ts: pd.Timestamp, last_ts: pd.Timestamp) -> None:
    """Ensure every anchored markup time falls inside the loaded bar range."""
    lo, hi = first_ts.timestamp(), last_ts.timestamp()
    for m in markup.markup:
        for key in ("time", "start", "end"):
            ts = getattr(m, key, None)
            if ts is None:
                continue
            t = pd.Timestamp(ts)
            if t.tzinfo is None:
                t = t.tz_localize("UTC")
            s = t.timestamp()
            if not (lo - 1 <= s <= hi + 1):
                raise ToolError(
                    f"markup {key}={ts} is outside the loaded bar range "
                    f"({first_ts.isoformat()} .. {last_ts.isoformat()}); enlarge `count` or fix the time"
                )


def register(mcp: Any, settings: Settings, loader: Callable | None = None, renderer: Callable | None = None) -> None:
    cache = BarCache(settings.cache_dir)
    load = loader or (lambda s, tf, c, p: _load(settings, cache, s, tf, c, p))
    draw = renderer or render_png
    out_dir = settings.chart_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    @mcp.tool(tags={"chart"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_chart_render(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Most-recent bars to chart", ge=20, le=500)] = 150,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        markup_json: Annotated[str, Field(description="Versioned JSON spec of SMC markup (see README/PLAN); empty = candles only")] = "",
        width: Annotated[int, Field(description="Output width px", ge=_MIN_DIM, le=_MAX_DIM)] = 1200,
        height: Annotated[int, Field(description="Output height px", ge=_MIN_DIM, le=_MAX_DIM)] = 700,
    ) -> dict:
        """Render candles + SMC markup to a PNG file and return its path.

        Markup primitives (anchored to bar times, ISO-8601 UTC): fvg/ob box (time,
        direction, top, bottom), line/bos/choch (time, level, optional label),
        killzone (start, end, optional label); plus `grid` (bool) and `version`.
        Example:
          {"version":1,"grid":true,"markup":[
             {"type":"fvg","time":"2026-08-24T17:15:00Z","direction":"bullish","top":1.1662,"bottom":1.1660},
             {"type":"killzone","start":"2026-08-24T06:00:00Z","end":"2026-08-24T09:00:00Z","label":"London"}]}
        """
        markup = parse_markup(markup_json)
        sym, tf, df = load(symbol, timeframe, count, provider)

        count = int(len(df))
        if count < _MIN_RENDER_BARS:
            raise ToolError(
                f"only {count} bars available for {sym.canonical} {tf.canonical}; "
                f"need >= {_MIN_RENDER_BARS} to render a useful chart"
            )
        df = df.tail(min(count, _MAX_RENDER_BARS))

        _validate_times(markup, df["time"].iloc[0], df["time"].iloc[-1])

        spec = build_spec(df, markup, width, height)
        fname = f"{sym.canonical}_{tf.canonical}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        out_path = out_dir / fname
        draw(spec, out_path)

        return {
            "path": str(out_path),
            "symbol": sym.canonical,
            "tv_symbol": sym.tv,
            "provider": choose_provider(provider, settings),
            "timeframe": tf.canonical,
            "width": width,
            "height": height,
            "bars": count,
            "markup_count": len(markup.markup),
        }
