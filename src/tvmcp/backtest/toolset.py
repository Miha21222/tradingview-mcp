"""`backtest` toolset: run a vetted built-in strategy through backtesting.py.

Strategies are selected by name from a fixed registry (never user/LLM code - the
sandboxed extension point is a later milestone). Read-only tool: no files are
written. Fill model is explicit: default fills at the next bar's open
(`trade_on_close=False`); setting `trade_on_close=True` fills at the current bar's
close. Currency assumptions are explicit: when the account currency differs from
the quote currency, `quote_to_account_rate` must be supplied.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Any, Callable

import pandas as pd
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bars import choose_provider, load_bars, window
from ..cache import BarCache
from ..config import Settings
from ..symbols import resolve, resolve_timeframe
from . import engine
from .forex import quote_currency
from .strategies import STRATEGIES


def _load(settings: Settings, cache: BarCache, symbol: str, timeframe: str, count: int, provider: str):
    sym = resolve(symbol)
    tf = resolve_timeframe(timeframe)
    end_ts = pd.Timestamp.now(tz="UTC")
    start_ts, _ = window(count, tf.minutes, end_ts)
    df = load_bars(settings, cache, sym, tf, start_ts, end_ts, count, provider)
    return sym, tf, df


def _validate_params(strategy: str, params: dict) -> None:
    if strategy == "sma_cross":
        fast = params.get("fast", 10)
        slow = params.get("slow", 30)
        if fast < 2 or slow < 2:
            raise ToolError("sma_cross params fast/slow must be >= 2")
        if fast >= slow:
            raise ToolError("sma_cross params require fast < slow")
    elif strategy == "breakout":
        if params.get("lookback", 20) < 2:
            raise ToolError("breakout param lookback must be >= 2")
        if params.get("risk_amount", 100.0) <= 0:
            raise ToolError("breakout param risk_amount must be > 0")
        if params.get("rr", 2.0) <= 0:
            raise ToolError("breakout param rr must be > 0")


def register(mcp: Any, settings: Settings, loader: Callable | None = None) -> None:
    cache = BarCache(settings.cache_dir)
    load = loader or (lambda s, tf, c, p: _load(settings, cache, s, tf, c, p))

    @mcp.tool(tags={"backtest"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_backtest_run(
        strategy: Annotated[str, Field(description="Built-in strategy name: sma_cross | breakout")] = "breakout",
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")] = "EURUSD",
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Most-recent bars to backtest", ge=50)] = 300,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        cash: Annotated[float, Field(description="Starting account balance (account currency)", gt=0)] = 10_000.0,
        spread_pips: Annotated[float, Field(description="Round-trip spread cost in pips", ge=0)] = 1.0,
        trade_on_close: Annotated[bool, Field(description="Fill at current bar close (True) vs next bar open (False)")] = False,
        margin: Annotated[float, Field(description="Fraction of notional held as margin (leverage assumption; 0.02 = 1:50)", gt=0.0, le=1.0)] = 0.02,
        account_currency: Annotated[str, Field(description="3-letter account currency, e.g. USD")] = "USD",
        quote_to_account_rate: Annotated[float | None, Field(description="Explicit quote->account FX rate when account currency != quote currency (e.g. 1/150 for USD account on USDJPY); defaults to 1.0 when they match")] = None,
        params_json: Annotated[str, Field(description="JSON object of strategy params, e.g. {\"lookback\":20,\"rr\":2,\"risk_amount\":100}. Symbol is always set from the symbol arg.")] = "{}",
    ) -> dict:
        """Backtest a vetted built-in strategy and return stats + capped recent trades.

        Read-only (no files written). Fill model: default next-bar-open;
        `trade_on_close=True` fills at the current bar close. Strategies are sized by
        risk from the actual stop distance, so P&L and R-multiples are in account
        currency for any quote currency; a next-open gap can alter realized risk.
        Unknown/invalid strategy params raise an actionable error.
        """
        if strategy not in STRATEGIES:
            raise ToolError(f"Unknown strategy {strategy!r}; available: {sorted(STRATEGIES)}")
        try:
            params = json.loads(params_json) if params_json.strip() else {}
        except json.JSONDecodeError as exc:
            raise ToolError(f"params_json is not valid JSON: {exc}") from exc
        if not isinstance(params, dict):
            raise ToolError("params_json must be a JSON object")
        _validate_params(strategy, params)

        sym, tf, df = load(symbol, timeframe, count, provider)
        count = int(len(df))

        # The resolved tool symbol is authoritative; params_json cannot override it.
        if "symbol" in params:
            raise ToolError("strategy param 'symbol' is managed by the tool; remove it from params_json")

        account_currency = account_currency.strip().upper()
        if len(account_currency) != 3 or not account_currency.isalpha():
            raise ToolError(f"account_currency must be a 3-letter code, got {account_currency!r}")
        if quote_to_account_rate is not None and (not math.isfinite(quote_to_account_rate) or quote_to_account_rate <= 0):
            raise ToolError("quote_to_account_rate must be a finite number > 0")

        quote = quote_currency(sym.canonical)
        if quote_to_account_rate is None and quote is not None and quote != account_currency:
            raise ToolError(
                f"quote currency {quote} != account currency {account_currency}; "
                "pass quote_to_account_rate explicitly"
            )
        try:
            summary, trades, meta = engine.run(
                df, strategy, params, sym.canonical,
                cash=cash, spread_pips=spread_pips, trade_on_close=trade_on_close,
                margin=margin, account_currency=account_currency,
                quote_to_account_rate=quote_to_account_rate,
            )
        except (TypeError, AttributeError, ValueError) as exc:
            raise ToolError(f"Backtest failed: {exc}") from exc

        return {
            "strategy": strategy,
            "symbol": sym.canonical,
            "tv_symbol": sym.tv,
            "provider": choose_provider(provider, settings),
            "timeframe": tf.canonical,
            "bars": count,
            "cash": cash,
            "spread_pips": spread_pips,
            "spread_relative": meta["spread_relative"],
            "spread_ref_price": meta["spread_ref_price"],
            "trade_on_close": trade_on_close,
            "fill_model": "current_close" if trade_on_close else "next_open",
            "margin": margin,
            "account_currency": account_currency,
            "quote_currency": meta["quote_currency"],
            "quote_to_account_rate": meta["quote_to_account_rate"],
            "pip_value_per_lot_account": meta["pip_value_per_lot_account"],
            "params": params,
            "total_trades": summary.pop("_trades_count", len(trades)),
            "summary": summary,
            "trades_returned": len(trades),
            "trades": trades,
        }