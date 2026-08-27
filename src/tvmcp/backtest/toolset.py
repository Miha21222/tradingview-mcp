"""`backtest` toolset: run a vetted built-in strategy through backtesting.py.

Strategies are selected by name from a fixed registry (never user/LLM code - the
sandboxed extension point is a later milestone). `tv_backtest_run` writes no files;
`tv_backtest_render_trades` writes PNGs only into the managed chart_dir
(collision-safe names, no caller-controlled paths). Fill model is explicit:
default fills at the next bar's open
(`trade_on_close=False`); setting `trade_on_close=True` fills at the current bar's
close. Currency assumptions are explicit: when the account currency differs from
the quote currency, `quote_to_account_rate` must be supplied.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from typing import Annotated, Any, Callable

import pandas as pd
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bars import choose_provider, load_bars, window
from ..cache import BarCache
from ..chart.markup import KillzoneMarkup, LineMarkup, Markup, parse_markup
from ..chart.renderer import build_spec, render_png
from ..config import Settings
from ..symbols import resolve, resolve_timeframe
from . import engine
from .forex import quote_currency
from .strategies import STRATEGIES

_MAX_RENDER_BARS = 500
_TAIL_BARS = 10  # bars kept after the exit so the outcome is visible


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
    elif strategy == "smc_h4_m15":
        if params.get("swing_length", 5) < 2:
            raise ToolError("smc_h4_m15 param swing_length must be >= 2")
        if params.get("risk_amount", 100.0) <= 0:
            raise ToolError("smc_h4_m15 param risk_amount must be > 0")
        if params.get("rr", 2.0) <= 0:
            raise ToolError("smc_h4_m15 param rr must be > 0")
        if params.get("expiry_bars", 96) < 1:
            raise ToolError("smc_h4_m15 param expiry_bars must be >= 1")


def _execute(
    load: Callable,
    strategy: str,
    symbol: str,
    timeframe: str,
    count: int,
    provider: str,
    cash: float,
    spread_pips: float,
    trade_on_close: bool,
    margin: float,
    account_currency: str,
    quote_to_account_rate: float | None,
    params_json: str,
):
    """Validate args, load bars, run the backtest. Shared by run + render tools."""
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

    # smc_h4_m15 derives its H4 bias by resampling the execution bars; running
    # it on H4-or-slower bars degenerates the bias to the bar itself.
    if strategy == "smc_h4_m15" and tf.minutes >= 240:
        raise ToolError("smc_h4_m15 needs an execution timeframe below H4 (M15 is the designed one)")

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
    return sym, tf, df, params, account_currency, summary, trades, meta


def _trade_markup(trade: dict, extra: Markup | None = None) -> Markup:
    """Entry/SL/TP/exit lines (SL red, TP green) + a band shading the trade's
    lifetime, plus any caller-supplied extra markup items."""
    items: list = [
        KillzoneMarkup(type="killzone", start=trade["entry_time"], end=trade["exit_time"], label=None),
        LineMarkup(type="line", time=trade["entry_time"], level=trade["entry_price"],
                   label=f"entry {trade['direction']} {trade['entry_price']}"),
    ]
    if trade.get("sl") is not None:
        items.append(LineMarkup(type="line", time=trade["entry_time"], level=trade["sl"],
                                label="SL", color="#e53935"))
    if trade.get("tp") is not None:
        items.append(LineMarkup(type="line", time=trade["entry_time"], level=trade["tp"],
                                label="TP", color="#43a047"))
    r = trade.get("r")
    r_txt = f" {r}R" if r is not None else ""
    items.append(LineMarkup(type="line", time=trade["exit_time"], level=trade["exit_price"],
                            label=f"exit{r_txt}"))
    if extra is not None:
        items.extend(extra.markup)
    return Markup(markup=items)


def register(mcp: Any, settings: Settings, loader: Callable | None = None, renderer: Callable | None = None) -> None:
    cache = BarCache(settings.cache_dir)
    load = loader or (lambda s, tf, c, p: _load(settings, cache, s, tf, c, p))
    draw = renderer or render_png

    @mcp.tool(tags={"backtest"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_backtest_run(
        strategy: Annotated[str, Field(description="Built-in strategy name: sma_cross | breakout | smc_h4_m15")] = "breakout",
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
        sym, tf, df, params, account_currency, summary, trades, meta = _execute(
            load, strategy, symbol, timeframe, count, provider, cash, spread_pips,
            trade_on_close, margin, account_currency, quote_to_account_rate, params_json,
        )
        count = int(len(df))

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

    @mcp.tool(tags={"backtest"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_backtest_render_trades(
        strategy: Annotated[str, Field(description="Built-in strategy name: sma_cross | breakout | smc_h4_m15")] = "breakout",
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")] = "EURUSD",
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Most-recent bars to backtest", ge=50)] = 300,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        cash: Annotated[float, Field(description="Starting account balance (account currency)", gt=0)] = 10_000.0,
        spread_pips: Annotated[float, Field(description="Round-trip spread cost in pips", ge=0)] = 1.0,
        trade_on_close: Annotated[bool, Field(description="Fill at current bar close (True) vs next bar open (False)")] = False,
        margin: Annotated[float, Field(description="Fraction of notional held as margin (leverage assumption; 0.02 = 1:50)", gt=0.0, le=1.0)] = 0.02,
        account_currency: Annotated[str, Field(description="3-letter account currency, e.g. USD")] = "USD",
        quote_to_account_rate: Annotated[float | None, Field(description="Explicit quote->account FX rate when account currency != quote currency; defaults to 1.0 when they match")] = None,
        params_json: Annotated[str, Field(description="JSON object of strategy params, same as tv_backtest_run")] = "{}",
        max_renders: Annotated[int, Field(description="Render the most recent N closed trades", ge=1, le=20)] = 6,
        context_bars: Annotated[int, Field(description="Bars of context shown before each entry", ge=10, le=200)] = 60,
        extra_markup_json: Annotated[str, Field(description="Optional markup_json (same schema as tv_chart_render) layered onto EVERY trade image - e.g. killzones, FVG boxes, text notes. Items outside a trade's window are simply not drawn.")] = "",
        width: Annotated[int, Field(description="Output width px", ge=200, le=2000)] = 1200,
        height: Annotated[int, Field(description="Output height px", ge=200, le=2000)] = 700,
    ) -> dict:
        """Re-run a backtest and render each recent trade to a PNG for visual sanity checks.

        Each image shows the actual bars around the trade with the entry price line,
        SL (red) and TP (green) lines, the exit line labeled with the R-multiple,
        and a shaded band over the trade's lifetime. `extra_markup_json` layers
        custom drawings (boxes, lines, killzones, text, markers - full
        tv_chart_render markup schema incl. hex colors) onto every image; for a
        fully custom per-trade look, use tv_backtest_run + tv_chart_render with
        `end_time` instead. Deterministic: same bars, same params, same trades as
        tv_backtest_run. PNGs land in the managed chart_dir (collision-safe names);
        paths are returned per trade. Needs headless Chromium
        (`uv run playwright install chromium`; tv_setup_doctor checks).
        """
        extra = parse_markup(extra_markup_json) if extra_markup_json.strip() else None
        sym, tf, df, params, account_currency, summary, trades, meta = _execute(
            load, strategy, symbol, timeframe, count, provider, cash, spread_pips,
            trade_on_close, margin, account_currency, quote_to_account_rate, params_json,
        )
        if not trades:
            return {
                "strategy": strategy, "symbol": sym.canonical, "timeframe": tf.canonical,
                "bars": int(len(df)), "total_trades": 0, "rendered": [],
                "note": "backtest produced no trades; nothing to render",
            }

        out_dir = settings.chart_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        times = df["time"]
        rendered = []
        for i, trade in enumerate(trades[-max_renders:]):
            entry_ts = pd.Timestamp(trade["entry_time"])
            exit_ts = pd.Timestamp(trade["exit_time"])
            lo = max(0, int(times.searchsorted(entry_ts)) - context_bars)
            hi = min(len(df), int(times.searchsorted(exit_ts)) + 1 + _TAIL_BARS)
            if hi - lo > _MAX_RENDER_BARS:
                lo = hi - _MAX_RENDER_BARS  # keep the exit visible; trim old context
            sub = df.iloc[lo:hi].reset_index(drop=True)
            spec = build_spec(sub, _trade_markup(trade, extra), width, height)
            r = trade.get("r")
            r_tag = f"{r:+.2f}R".replace("+", "p").replace("-", "m").replace(".", "_") if r is not None else "naR"
            fname = (
                f"bt_{strategy}_{sym.canonical}_{tf.canonical}_t{i}_"
                f"{trade['direction']}_{r_tag}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
            )
            path = out_dir / fname
            draw(spec, path)
            rendered.append({"path": str(path), "window_bars": int(len(sub)), **trade})

        return {
            "strategy": strategy,
            "symbol": sym.canonical,
            "tv_symbol": sym.tv,
            "provider": choose_provider(provider, settings),
            "timeframe": tf.canonical,
            "bars": int(len(df)),
            "params": params,
            "fill_model": "current_close" if trade_on_close else "next_open",
            "total_trades": summary.get("_trades_count", len(trades)),
            "rendered_count": len(rendered),
            "rendered": rendered,
        }