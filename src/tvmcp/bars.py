"""Shared bar-loading service used by the `data` and `scan` toolsets.

Single fetch + Parquet-cache path so providers/cache are not duplicated across
toolsets. Returns DataFrames with BAR_COLUMNS and a clean RangeIndex.
"""

from __future__ import annotations

from datetime import timedelta, timezone

import pandas as pd
from fastmcp.exceptions import ToolError

from .cache import BarCache
from .config import Settings
from .data_providers import DataProviderError, DukascopyProvider, OandaProvider
from .symbols import Symbol, Timeframe


def make_providers(settings: Settings) -> tuple[DukascopyProvider, OandaProvider]:
    """Construct the two OHLCV providers from settings (the only place they're built)."""
    return DukascopyProvider(), OandaProvider(settings.oanda_api_key, settings.oanda_env)


def choose_provider(provider: str, settings: Settings) -> str:
    if provider == "auto":
        return "oanda" if settings.oanda_api_key else "dukascopy"
    if provider not in ("dukascopy", "oanda"):
        raise ToolError(f"Unknown provider {provider!r}; use auto, dukascopy or oanda")
    return provider


def window(count: int, timeframe_minutes: int, end_ts: pd.Timestamp | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Calendar window that should comfortably contain `count` bars of `timeframe_minutes`."""
    end_ts = end_ts or pd.Timestamp.now(tz="UTC")
    # generous pad: weekends/holidays mean calendar time > bar time
    start_ts = end_ts - timedelta(minutes=timeframe_minutes * count * 2 + 4 * 1440)
    return start_ts, end_ts


def load_bars(
    settings: Settings,
    cache: BarCache,
    symbol: Symbol,
    timeframe: Timeframe,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    count: int,
    provider: str = "auto",
) -> pd.DataFrame:
    """Return up to `count` bars for the window, fetching + caching as needed.

    Raises ToolError when the provider cannot deliver bars or the window is empty.
    """
    chosen = choose_provider(provider, settings)
    dukascopy, oanda = make_providers(settings)

    cached = cache.slice(chosen, symbol.canonical, timeframe.canonical, start_ts, end_ts)
    need_fetch = cached.empty or (len(cached) < count and _range_uncovered(
        cached, start_ts, end_ts, timeframe.minutes
    ))
    if need_fetch:
        try:
            if chosen == "dukascopy":
                fetched = dukascopy.fetch(symbol, timeframe, start_ts.date(), end_ts.date())
            else:
                fetched = oanda.fetch(
                    symbol, timeframe,
                    start=start_ts.to_pydatetime().astimezone(timezone.utc),
                    end=end_ts.to_pydatetime().astimezone(timezone.utc),
                )
        except DataProviderError as exc:
            raise ToolError(str(exc)) from exc
        cache.store(chosen, symbol.canonical, timeframe.canonical, fetched)
        cached = cache.slice(chosen, symbol.canonical, timeframe.canonical, start_ts, end_ts)

    if cached.empty:
        raise ToolError(
            f"No bars for {symbol.canonical} {timeframe.canonical} in "
            f"{start_ts.date()}..{end_ts.date()} from {chosen}. Try another range or provider."
        )
    return cached.tail(count).reset_index(drop=True)


def _range_uncovered(
    cached: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, tf_minutes: int
) -> bool:
    """True when the cache clearly does not span the requested window."""
    if cached.empty:
        return True
    first = cached["time"].iloc[0]
    last = cached["time"].iloc[-1]
    slack = pd.Timedelta(minutes=tf_minutes * 3 + 3 * 1440)  # weekend tolerance
    return first > start + slack or last < end - slack


# --------------------------------------------------------------- any provider

SESSION_PROVIDER = "session"


def load_bars_any(
    settings: Settings,
    cache: BarCache,
    symbol,
    timeframe,
    count: int,
    provider: str,
    end_ts: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Load bars from the free feeds OR the opt-in account feed.

    The free providers cover forex and the instruments Dukascopy/OANDA carry;
    broker CFDs like `PEPPERSTONE:US500` exist only on the user's own
    TradingView feed, and that is exactly the instrument a session workflow
    tends to run on. Tools that must reach it accept `provider="session"` and
    come through here, so the cookie tier keeps one gate and one warning
    instead of a copy per tool.

    The account feed has no end anchor - it always returns the LAST `count`
    bars - so a caller asking for an older window must size `count` to reach
    back from now. The caller owns that; this function just refuses to pretend.
    """
    count = min(count, settings.max_bars)
    if provider != SESSION_PROVIDER:
        end = end_ts or pd.Timestamp.now(tz="UTC")
        start_ts, _ = window(count, timeframe.minutes, end)
        return load_bars(settings, cache, symbol, timeframe, start_ts, end, count, provider)

    if not settings.toolset_enabled("session"):
        raise ToolError(
            "provider='session' needs the opt-in `session` toolset "
            "(TV_TOOLSETS=...,session) and TV_SESSIONID - it reads through YOUR "
            "TradingView account cookie (ToS risk). Use auto/dukascopy/oanda for "
            "the free feeds."
        )
    if not settings.session_id:
        raise ToolError("TV_SESSIONID not set; the `session` provider needs it")
    from .session.client import SessionClient
    from .session.warnings import warn_once

    warn_once()
    return SessionClient(settings.session_id).get_bars(symbol, timeframe, count)
