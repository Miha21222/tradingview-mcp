"""`scan` toolset: SMC/ICT pattern detection (FVG, OB, structure, liquidity, sessions, prev H/L).

Wraps the pinned `smartmoneyconcepts` (0.0.27) through pure adapters in
`scan/detectors.py`. Bars are loaded via the shared service in `tvmcp/bars.py`
(same Dukascopy/OANDA providers + Parquet cache as the `data` toolset).

Notes on behaviour:
- Results are bounded to the `_MAX_RESULTS` most-recent detections (compact output);
  `total_count` / `returned_count` / `truncated` report the full picture.
- Swing-based detectors (ob, structure, liquidity) classify a swing using future
  candles, so the trailing `swing_length` bars repaint; every such result carries a
  `repaint_note` telling the caller to treat those as unconfirmed.
- Session windows are fixed UTC hours (not DST-aware) per the pinned library.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Callable

import pandas as pd
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bars import choose_provider, load_bars, window
from ..cache import BarCache
from ..config import Settings
from ..symbols import resolve, resolve_timeframe
from . import detectors

_MAX_RESULTS = 100

_ALLOWED_SESSIONS = frozenset(
    {
        "Sydney", "Tokyo", "London", "New York",
        "Asian kill zone", "London open kill zone", "New York kill zone",
        "london close kill zone", "Custom",
    }
)


def _resolve_symbol_tf(symbol: str, timeframe: str):
    try:
        return resolve(symbol), resolve_timeframe(timeframe)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _default_load(settings: Settings, cache: BarCache, symbol: str, timeframe: str, count: int, provider: str):
    sym, tf = _resolve_symbol_tf(symbol, timeframe)
    count = min(count, settings.max_bars)
    end_ts = pd.Timestamp.now(tz="UTC")
    start_ts, _ = window(count, tf.minutes, end_ts)
    df = load_bars(settings, cache, sym, tf, start_ts, end_ts, count, provider)
    return sym, tf, df


def _validate_session(session: str, start_time: str, end_time: str) -> None:
    if session not in _ALLOWED_SESSIONS:
        raise ToolError(
            f"Unknown session {session!r}; use one of {sorted(_ALLOWED_SESSIONS)}"
        )
    if session == "Custom":
        if not start_time or not end_time:
            raise ToolError("Custom session requires start_time and end_time (HH:MM UTC)")
        for t in (start_time, end_time):
            try:
                datetime.strptime(t, "%H:%M")
            except ValueError:
                raise ToolError(f"Bad time {t!r}; use HH:MM (24h UTC)") from None


def register(mcp: Any, settings: Settings, loader: Callable | None = None) -> None:
    cache = BarCache(settings.cache_dir)
    load = loader or (lambda s, tf, c, p: _default_load(settings, cache, s, tf, c, p))

    def _base(sym, tf, df, provider, detector, params) -> dict:
        return {
            "symbol": sym.canonical,
            "tv_symbol": sym.tv,
            "provider": choose_provider(provider, settings),
            "timeframe": tf.canonical,
            "detector": detector,
            "bars_scanned": int(len(df)),
            "params": params,
        }

    def _finalize(out: dict, results: list) -> dict:
        total = len(results)
        shown = results[-_MAX_RESULTS:]
        out["total_count"] = total
        out["returned_count"] = len(shown)
        out["truncated"] = total > _MAX_RESULTS
        out["results"] = shown
        return out

    @mcp.tool(tags={"scan"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_scan_fvg(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Number of most-recent bars to scan", ge=50)] = 500,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        join_consecutive: Annotated[bool, Field(description="Merge consecutive same-direction FVGs into one")] = False,
    ) -> dict:
        """Scan for Fair Value Gaps. Bullish FVG = prior high below next low on an up candle."""
        sym, tf, df = load(symbol, timeframe, count, provider)
        out = _base(sym, tf, df, provider, "fvg", {"join_consecutive": join_consecutive})
        out["repaint_note"] = "fvg uses the next candle; the final bar is unconfirmed"
        return _finalize(out, detectors.scan_fvg(df, join_consecutive=join_consecutive))

    @mcp.tool(tags={"scan"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_scan_ob(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Number of most-recent bars to scan", ge=50)] = 500,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        swing_length: Annotated[int, Field(description="Candles each side of a swing; 5-15 for M5/M15", ge=2, le=50)] = 5,
        close_mitigation: Annotated[bool, Field(description="Count mitigation on close instead of low/high")] = False,
    ) -> dict:
        """Scan for Order Blocks: the last down-candle before a bullish breakout (and mirror)."""
        sym, tf, df = load(symbol, timeframe, count, provider)
        params = {"swing_length": swing_length, "close_mitigation": close_mitigation}
        out = _base(sym, tf, df, provider, "ob", params)
        out["repaint_note"] = f"last {swing_length} bars use future candles (swing look-ahead) - unconfirmed"
        return _finalize(out, detectors.scan_ob(df, swing_length=swing_length, close_mitigation=close_mitigation))

    @mcp.tool(tags={"scan"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_scan_structure(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Number of most-recent bars to scan", ge=50)] = 500,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        swing_length: Annotated[int, Field(description="Candles each side of a swing; 5-15 for M5/M15", ge=2, le=50)] = 5,
        close_break: Annotated[bool, Field(description="Require close through the level (vs high/low)")] = True,
    ) -> dict:
        """Scan for Break of Structure (BOS) and Change of Character (CHoCH)."""
        sym, tf, df = load(symbol, timeframe, count, provider)
        params = {"swing_length": swing_length, "close_break": close_break}
        out = _base(sym, tf, df, provider, "structure", params)
        out["repaint_note"] = f"last {swing_length} bars use future candles (swing look-ahead) - unconfirmed"
        return _finalize(out, detectors.scan_structure(df, swing_length=swing_length, close_break=close_break))

    @mcp.tool(tags={"scan"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_scan_liquidity(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Number of most-recent bars to scan", ge=50)] = 500,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        swing_length: Annotated[int, Field(description="Candles each side of a swing; 5-15 for M5/M15", ge=2, le=50)] = 5,
        range_percent: Annotated[float, Field(description="Range (%) of the data's high-low used to cluster swing levels", gt=0.0, le=0.2)] = 0.01,
    ) -> dict:
        """Scan for liquidity pools (clustered swing highs or lows) and their sweeps."""
        sym, tf, df = load(symbol, timeframe, count, provider)
        params = {"swing_length": swing_length, "range_percent": range_percent}
        out = _base(sym, tf, df, provider, "liquidity", params)
        out["repaint_note"] = f"last {swing_length} bars use future candles (swing look-ahead) - unconfirmed"
        return _finalize(out, detectors.scan_liquidity(df, swing_length=swing_length, range_percent=range_percent))

    @mcp.tool(tags={"scan"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_scan_sessions(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Number of most-recent bars to scan", ge=50)] = 500,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        session: Annotated[str, Field(description="Sydney, Tokyo, London, New York, Asian kill zone, London open kill zone, New York kill zone, london close kill zone, Custom")] = "London open kill zone",
        start_time: Annotated[str, Field(description="HH:MM UTC; required for Custom session")] = "",
        end_time: Annotated[str, Field(description="HH:MM UTC; required for Custom session")] = "",
    ) -> dict:
        """Find killzone/session blocks and each block's high/low (dealing-range liquidity).

        Session windows are fixed UTC hours (not DST-aware). Custom requires
        start_time/end_time as HH:MM UTC.
        """
        _validate_session(session, start_time, end_time)
        sym, tf, df = load(symbol, timeframe, count, provider)
        res = detectors.scan_sessions(df, session, start_time, end_time, "UTC")
        blocks = res["blocks"]
        total = len(blocks)
        shown = blocks[-_MAX_RESULTS:]
        out = _base(sym, tf, df, provider, "sessions", {
            "session": session, "start_time": start_time, "end_time": end_time, "time_zone": "UTC",
        })
        out["total_count"] = total
        out["returned_count"] = len(shown)
        out["truncated"] = total > _MAX_RESULTS
        out["session"] = res["session"]
        out["time_zone"] = res["time_zone"]
        out["blocks"] = shown
        return out

    @mcp.tool(tags={"scan"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_scan_prev_hl(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Number of most-recent bars to scan", ge=50)] = 500,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        time_frame: Annotated[str, Field(description="Levels timeframe: 15m, 1H, 4H, 1D, 1W, 1M")] = "1D",
    ) -> dict:
        """Previous high/low of `time_frame` and whether price has broken them."""
        sym, tf, df = load(symbol, timeframe, count, provider)
        out = _base(sym, tf, df, provider, "prev_hl", {"time_frame": time_frame})
        out.update(detectors.scan_prev_hl(df, time_frame=time_frame))
        return out
