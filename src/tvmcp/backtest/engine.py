"""Run vetted strategies through backtesting.py and normalize the results.

Handles the dataframe conversion (lowercase bars -> capitalized OHLCV with a UTC
DatetimeIndex), the fixed-pip spread conversion (backtesting.py's `spread` is
relative, so we derive the rate exact at a reference price), fill-model semantics
(trade_on_close vs next-open), and compact trade normalization with R-multiples,
physical units and UTC session tags.

Currency model: backtesting.py prices P&L in a single account currency. Strategies
sized with `size = risk / stop_distance` (see strategies.Breakout) make P&L already
denominated in account currency for any quote currency, because `size` carries the
quote-to-account conversion. When the account currency differs from the quote
currency, `quote_to_account_rate` MUST be supplied for correct physical-unit and
pip-value reporting (risk-based P&L itself needs no rate).
"""

from __future__ import annotations

import math

import pandas as pd

from . import forex
from .strategies import STRATEGIES

MAX_TRADES = 100


def _to_backtest_df(df: pd.DataFrame) -> "pd.DataFrame":
    out = df.copy()
    out.index = pd.to_datetime(out["time"], utc=True)
    return out[["open", "high", "low", "close", "volume"]].rename(
        columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        }
    )


def run(
    df: "pd.DataFrame",
    strategy_name: str,
    strategy_params: dict,
    symbol: str,
    cash: float = 10_000.0,
    spread_pips: float = 0.0,
    trade_on_close: bool = False,
    exclusive_orders: bool = True,
    margin: float = 0.02,
    account_currency: str = "USD",
    quote_to_account_rate: float | None = None,
) -> tuple[dict, list[dict], dict]:
    from backtesting import Backtest

    strategy = STRATEGIES[strategy_name]

    quote = forex.quote_currency(symbol)
    if quote_to_account_rate is not None and not (math.isfinite(quote_to_account_rate) and quote_to_account_rate > 0):
        raise ValueError("quote_to_account_rate must be a finite number > 0")
    if quote_to_account_rate is None:
        if quote is not None and quote != account_currency:
            raise ValueError(
                f"quote currency {quote} != account currency {account_currency}; "
                "pass quote_to_account_rate explicitly"
            )
        quote_to_account_rate = 1.0

    ref_price = float(df["close"].mean())
    spread = forex.spread_relative(spread_pips, symbol, ref_price)
    data = _to_backtest_df(df)
    bt = Backtest(
        data,
        strategy,
        cash=cash,
        spread=spread,
        trade_on_close=trade_on_close,
        exclusive_orders=exclusive_orders,
        margin=margin,
        finalize_trades=True,
    )
    stats = bt.run(**strategy_params)
    trades = _normalize_trades(stats, symbol, quote_to_account_rate)
    summary = _summary(stats)
    meta = {
        "spread_relative": spread,
        "spread_ref_price": ref_price,
        "quote_currency": quote,
        "quote_to_account_rate": quote_to_account_rate,
        "pip_value_per_lot_account": forex.pip_value_per_lot(symbol, quote_to_account_rate),
        "finalize_trades": True,
    }
    return summary, trades, meta


def _norm(v):
    import numpy as np

    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return round(float(v), 6)
    return v


def _summary(stats: "pd.Series") -> dict:
    keys = ["Start", "End", "Equity Final [$]", "Equity Peak [$]", "Return [%]",
            "Max. Drawdown [%]", "Sharpe Ratio", "Sortino Ratio", "# Trades",
            "Win Rate [%]", "Profit Factor", "Expectancy [%]", "SQN"]
    out = {}
    for k in keys:
        if k in stats.index:
            out[k] = _norm(stats[k])
    out["_trades_count"] = int(len(stats.get("_trades", [])))
    return out


def _normalize_trades(stats: "pd.Series", symbol: str, quote_to_account_rate: float) -> list[dict]:
    import numpy as np

    trades = stats.get("_trades", [])
    out = []
    for _, t in trades.tail(MAX_TRADES).iterrows():
        size = float(t["Size"])
        entry = float(t["EntryPrice"])
        exit_ = float(t["ExitPrice"])
        pnl = float(t["PnL"])
        sl = t.get("SL")
        sl = float(sl) if sl is not None and not (isinstance(sl, float) and np.isnan(sl)) else None
        tp = t.get("TP")
        tp = float(tp) if tp is not None and not (isinstance(tp, float) and np.isnan(tp)) else None
        direction = "long" if size > 0 else "short"
        r = None
        if sl is not None and abs(entry - sl) > 0:
            r = round(pnl / (abs(size) * abs(entry - sl)), 3)
        et = t["EntryTime"]
        et = pd.Timestamp(et)
        rec = {
            "direction": direction,
            "size": abs(size),  # backtesting units (conversion-adjusted account units)
            "units": round(abs(size) / quote_to_account_rate, 4),  # physical instrument units
            "entry_price": round(entry, 6),
            "exit_price": round(exit_, 6),
            "pnl": round(pnl, 6),
            "r": r,
            "sl": round(sl, 6) if sl is not None else None,
            "tp": round(tp, 6) if tp is not None else None,
            "entry_time": et.isoformat(),
            "exit_time": pd.Timestamp(t["ExitTime"]).isoformat(),
            "session": forex.killzone(et.hour) or "off",
        }
        if "ReturnPct" in t.index:
            rec["return_pct"] = round(float(t["ReturnPct"]), 6)
        out.append(rec)
    return out