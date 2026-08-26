"""`data` toolset: historical OHLCV from TV-independent feeds (Dukascopy, OANDA) with Parquet cache."""

from __future__ import annotations

from typing import Annotated, Any

import pandas as pd
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bars import choose_provider, load_bars, make_providers, window
from ..cache import BarCache
from ..config import Settings
from ..data_providers import DataProviderError
from ..symbols import resolve, resolve_timeframe


def register(mcp: Any, settings: Settings) -> None:
    cache = BarCache(settings.cache_dir)
    dukascopy, oanda = make_providers(settings)

    @mcp.tool(tags={"data"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_data_providers_status() -> dict:
        """Which OHLCV data providers are usable right now, and why not if not."""
        return {
            "providers": [vars(dukascopy.status()), vars(oanda.status())],
            "cache_dir": str(settings.cache_dir),
            "default_provider": "oanda" if settings.oanda_api_key else "dukascopy",
        }

    @mcp.tool(tags={"data"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_data_get_bars(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1 (aliases like 15m/1h accepted)")] = "M15",
        start: Annotated[str | None, Field(description="ISO date/datetime UTC, e.g. 2026-07-01. Default: computed back from `count`.")] = None,
        end: Annotated[str | None, Field(description="ISO date/datetime UTC. Default: now.")] = None,
        count: Annotated[int, Field(description="Max bars returned (tail of range)", ge=1)] = 500,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda. auto prefers OANDA when a key is configured.")] = "auto",
    ) -> dict:
        """Fetch historical OHLCV bars. Cached in Parquet; repeat queries are instant.

        Providers: dukascopy (free, deep history to ~2000s, first fetch of a range is
        slow) and oanda (real broker mid prices, needs OANDA_API_KEY). Bars are UTC,
        returned as arrays [time_iso, open, high, low, close, volume]. The `provider`
        field in the result names the feed - feeds disagree; levels are feed-specific.
        """
        sym = resolve(symbol)
        tf = resolve_timeframe(timeframe)
        count = min(count, settings.max_bars)

        end_ts = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp.now(tz="UTC")
        if start:
            start_ts = pd.Timestamp(start, tz="UTC")
        else:
            start_ts, _ = window(count, tf.minutes, end_ts)

        chosen = choose_provider(provider, settings)
        try:
            df = load_bars(settings, cache, sym, tf, start_ts, end_ts, count, chosen)
        except DataProviderError as exc:
            raise ToolError(str(exc)) from exc

        bars = [
            [
                t.isoformat().replace("+00:00", "Z"),
                round(float(o), 6), round(float(h), 6),
                round(float(lo), 6), round(float(c), 6), float(v),
            ]
            for t, o, h, lo, c, v in df.itertuples(index=False)
        ]
        return {
            "symbol": sym.canonical,
            "tv_symbol": sym.tv,
            "provider": chosen,
            "timeframe": tf.canonical,
            "count": len(bars),
            "columns": ["time", "open", "high", "low", "close", "volume"],
            "bars": bars,
        }
