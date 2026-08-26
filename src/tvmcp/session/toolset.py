"""`session` toolset: opt-in TradingView-account data (OHLCV + realtime quote).

Gate: enabled only via `TV_TOOLSETS=...,session` (never default). Requires
`TV_SESSIONID` (the owner's TradingView `sessionid` cookie) and authenticates as that
account - may violate TradingView's ToS (a warning is printed to stderr on first use,
see `warnings.py`). Live fetch goes through `client.SessionClient`
(tvdatafeed-enhanced authenticated via the cookie-derived JWT). All tools are
read-only. Not affiliated with TradingView, Inc.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import Settings
from ..symbols import resolve, resolve_timeframe
from .warnings import warn_once

_SESSION_PROVIDER = "session"
_MAX_COUNT = 5000  # tvdatafeed-enhanced wire cap


def _require_session(settings: Settings) -> None:
    if not settings.session_id:
        raise ToolError(
            "TV_SESSIONID not set. The `session` toolset reads data through YOUR "
            "TradingView account cookie - opt-in at your own risk (may violate "
            "TradingView's ToS; see the warning printed on first use). Set "
            "TV_SESSIONID, or use the default `data` toolset instead."
        )


def register(mcp: Any, settings: Settings, loader: Callable | None = None, realtime: Callable | None = None) -> None:
    _client = None

    def _session_client():
        nonlocal _client
        if _client is None:
            from .client import SessionClient

            _client = SessionClient(settings.session_id)
        return _client

    def _default_load(symbol: str, timeframe: str, count: int):
        _require_session(settings)
        sym = resolve(symbol)
        tf = resolve_timeframe(timeframe)
        return sym, tf, _session_client().get_bars(sym, tf, count)

    def _default_realtime(symbol: str):
        _require_session(settings)
        return _session_client().get_quote(resolve(symbol))

    load = loader or _default_load
    quote = realtime or _default_realtime

    @mcp.tool(tags={"session"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_session_status() -> dict:
        """Whether the opt-in `session` toolset is usable and why not if not.

        Reads data through YOUR TradingView account cookie - opt-in, may violate
        TradingView's ToS. Not affiliated with TradingView, Inc.
        """
        warn_once()
        sid = bool(settings.session_id)
        return {
            "session_id_set": sid,
            "usable": sid,
            "detail": (
                "TV_SESSIONID is set; live fetch goes through tvdatafeed-enhanced "
                "(cookie-derived JWT). An expired cookie surfaces as an actionable "
                "error on first fetch."
                if sid else
                "TV_SESSIONID not set - set it to enable the session toolset (opt-in, ToS risk)"
            ),
            "tos_warning": "see stderr / README - not affiliated with TradingView, Inc.",
        }

    @mcp.tool(tags={"session"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_session_ohlcv(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Most-recent bars (TV wire cap 5000/request)", ge=1, le=5000)] = 500,
    ) -> dict:
        """TradingView-account OHLCV bars (tvdatafeed-enhanced). Requires TV_SESSIONID.

        Opt-in and read-only; uses YOUR TradingView account cookie, which may violate
        TradingView's ToS. Bars are UTC arrays [time_iso, open, high, low, close,
        volume]; provider is always `session`. Not affiliated with TradingView, Inc.
        """
        warn_once()
        _require_session(settings)
        count = min(count, settings.max_bars, _MAX_COUNT)
        sym, tf, df = load(symbol, timeframe, count)
        bars = [
            [
                t.isoformat().replace("+00:00", "Z"),
                round(float(o), 6), round(float(h), 6),
                round(float(lo), 6), round(float(c), 6), float(v),
            ]
            for t, o, h, lo, c, v in df[["time", "open", "high", "low", "close", "volume"]].itertuples(index=False)
        ]
        return {
            "symbol": sym.canonical,
            "tv_symbol": sym.tv,
            "provider": _SESSION_PROVIDER,
            "timeframe": tf.canonical,
            "count": len(bars),
            "columns": ["time", "open", "high", "low", "close", "volume"],
            "bars": bars,
        }

    @mcp.tool(tags={"session"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_session_realtime(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
    ) -> dict:
        """Latest TradingView-account quote for a symbol (tvdatafeed-enhanced).

        Opt-in and read-only; requires TV_SESSIONID. May violate TradingView's ToS.
        The returned `symbol`/`tv_symbol`/`provider` are authoritative (never
        overrideable by the quote payload). Not affiliated with TradingView, Inc.
        """
        warn_once()
        _require_session(settings)
        sym = resolve(symbol)
        payload = dict(quote(symbol) or {})
        # authoritative fields cannot be overridden by provider-supplied data
        for key in ("symbol", "tv_symbol", "provider"):
            payload.pop(key, None)
        out = {"symbol": sym.canonical, "tv_symbol": sym.tv, "provider": _SESSION_PROVIDER}
        out.update(payload)
        return out