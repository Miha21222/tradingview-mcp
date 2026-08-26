"""Backtest engine + toolset tests. Engine tests are offline (synthetic bars);
tool tests use a mocked loader (no network)."""

import asyncio
import json

import numpy as np
import pandas as pd
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import backtest
from tvmcp.backtest import engine, forex
from tvmcp.config import Settings
from tvmcp.symbols import resolve, resolve_timeframe


def _make_df(n=200, seed=11) -> pd.DataFrame:
    times = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.RandomState(seed)
    trend = 1.10 + np.linspace(0, 0.05, n) + 0.008 * np.sin(np.linspace(0, 12 * np.pi, n)) + rng.normal(0, 0.0015, n)
    op = np.roll(trend, 1)
    cl = trend
    return pd.DataFrame(
        {
            "time": times,
            "open": op,
            "high": np.maximum(op, cl) + 0.003,
            "low": np.minimum(op, cl) - 0.003,
            "close": cl,
            "volume": 100 + rng.rand(n) * 50,
        }
    )


def _crafted_eurusd() -> pd.DataFrame:
    # single breakout long; warmup skips bars 0-2, breakout fires at bar 3
    # (close 1.1010 > prior high 1.1000), stop 20 pips => size 50000 exact, TP=1R=1.1030.
    # Post-TP close stays below prior high so no second trade.
    rows = [
        ["2026-01-01T00:00:00Z", 1.1000, 1.1000, 1.0990, 1.0995],
        ["2026-01-01T01:00:00Z", 1.0995, 1.1000, 1.0990, 1.0995],
        ["2026-01-01T02:00:00Z", 1.0995, 1.1000, 1.0990, 1.0995],
        ["2026-01-01T03:00:00Z", 1.0995, 1.1010, 1.0990, 1.1010],  # breakout
        ["2026-01-01T04:00:00Z", 1.1010, 1.1018, 1.1005, 1.1012],  # fill at open
        ["2026-01-01T05:00:00Z", 1.1012, 1.1035, 1.1010, 1.1016],  # TP 1.1030 hit
        ["2026-01-01T06:00:00Z", 1.1016, 1.1014, 1.1010, 1.1012],
        ["2026-01-01T07:00:00Z", 1.1012, 1.1013, 1.1010, 1.1012],
        ["2026-01-01T08:00:00Z", 1.1012, 1.1012, 1.1010, 1.1012],
    ]
    return _frame(rows)


def _crafted_usdjpy() -> pd.DataFrame:
    # same structure scaled to ~150; stop 20 pips => size 500 exact, TP=1R=150.20
    rows = [
        ["2026-01-01T00:00:00Z", 149.90, 149.90, 149.80, 149.85],
        ["2026-01-01T01:00:00Z", 149.85, 149.90, 149.80, 149.85],
        ["2026-01-01T02:00:00Z", 149.85, 149.90, 149.80, 149.85],
        ["2026-01-01T03:00:00Z", 149.85, 150.00, 149.80, 150.00],  # breakout
        ["2026-01-01T04:00:00Z", 150.00, 150.10, 149.95, 150.02],  # fill at open
        ["2026-01-01T05:00:00Z", 150.02, 150.35, 150.00, 150.08],  # TP 150.20 hit
        ["2026-01-01T06:00:00Z", 150.08, 150.06, 150.02, 150.04],
        ["2026-01-01T07:00:00Z", 150.04, 150.05, 150.02, 150.04],
        ["2026-01-01T08:00:00Z", 150.04, 150.04, 150.02, 150.04],
    ]
    return _frame(rows)


def _frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["volume"] = 100.0
    return df


def _breakout_params():
    return {"lookback": 2, "rr": 1.0, "risk_amount": 100.0}


# ---------------- engine-level ----------------

def test_engine_runs_both_strategies():
    df = _make_df()
    for strat, params in [("sma_cross", {"fast": 10, "slow": 30}),
                          ("breakout", _breakout_params())]:
        summary, trades, meta = engine.run(df, strat, params, "EURUSD", cash=10000, spread_pips=1.0, margin=0.02)
        assert summary["# Trades"] >= 0
        assert isinstance(trades, list)
        assert summary["Equity Final [$]"] is not None
        assert meta["finalize_trades"] is True


def test_engine_breakout_trades_have_r_units_and_session():
    df = _make_df()
    _, trades, _ = engine.run(df, "breakout", _breakout_params(), "EURUSD",
                              cash=10000, spread_pips=1.0, margin=0.02)
    assert trades, "expected at least one breakout trade on the trend series"
    t = trades[0]
    assert t["direction"] in ("long", "short")
    assert t["size"] > 0 and t["units"] > 0
    assert "entry_time" in t and "exit_time" in t
    assert t["session"] in {"off", "Asian kill zone", "London open kill zone",
                            "New York kill zone", "London close kill zone"}
    assert t["sl"] is not None
    assert t["r"] is not None


@pytest.mark.parametrize("symbol,df,account", [
    ("EURUSD", _crafted_eurusd(), "USD"),
    ("USDJPY", _crafted_usdjpy(), "JPY"),
])
def test_single_trade_pnl_is_currency_correct(symbol, df, account):
    # Same risk (100) + same pip spread (2) must give the same account-currency PnL
    # (~90 at TP=1R) for USD- and JPY-quoted pairs - the spread + sizing model must
    # not distort JPY prices.
    for spread_pips, expect in [(0.0, 100.0), (2.0, 90.0)]:
        summary, trades, meta = engine.run(
            df, "breakout", _breakout_params(), symbol,
            cash=10000, spread_pips=spread_pips, margin=0.02,
            account_currency=account,
        )
        assert len(trades) == 1, f"{symbol} spread={spread_pips} expected 1 trade"
        assert trades[0]["pnl"] == pytest.approx(expect, abs=1.0)
        assert meta["quote_to_account_rate"] == 1.0  # account == quote


def test_engine_requires_explicit_rate_when_account_mismatches():
    df = _crafted_usdjpy()
    with pytest.raises(ValueError):
        engine.run(df, "breakout", _breakout_params(), "USDJPY", account_currency="USD", quote_to_account_rate=None)


def test_engine_rejects_nonpositive_rate():
    df = _crafted_usdjpy()
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            engine.run(df, "breakout", _breakout_params(), "USDJPY",
                       account_currency="USD", quote_to_account_rate=bad)


def test_spread_relative_roundtrip_cost_matches_pips():
    for symbol, ref, size in [("EURUSD", 1.10, 50000.0), ("USDJPY", 150.0, 500.0)]:
        s = forex.spread_relative(2.0, symbol, ref)
        # backtesting charges spread once at entry: round-trip cost == s * ref * size
        # == pips * pip_size * size
        assert s * ref * size == pytest.approx(2.0 * forex.pip_size(symbol) * size, rel=1e-12)


# ---------------- tool-level (mocked loader) ----------------

def _settings(tmp_path) -> Settings:
    return Settings(
        toolsets=frozenset({"backtest"}),
        extra_tools=frozenset(),
        read_only=False,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=None,
        oanda_env="practice",
        session_id=None,
    )


def _loader(df: pd.DataFrame):
    def load(symbol, timeframe, count, provider):
        sym, tf = resolve(symbol), resolve_timeframe(timeframe)
        return sym, tf, df.tail(count).reset_index(drop=True)

    return load


def _build(tmp_path, n=200, df=None):
    mcp = FastMCP(name="test")
    settings = _settings(tmp_path)
    backtest.register(mcp, settings, loader=_loader(df if df is not None else _make_df(n)))
    return mcp


def _data(mcp, name, args):
    r = asyncio.run(mcp.call_tool(name, args))
    return json.loads(r.content[0].text)


def _error(mcp, name, args):
    with pytest.raises(ToolError) as ei:
        asyncio.run(mcp.call_tool(name, args))
    return str(ei.value)


def test_tool_runs_and_reports_metadata(tmp_path):
    mcp = _build(tmp_path)
    data = _data(mcp, "tv_backtest_run", {
        "strategy": "sma_cross", "symbol": "EURUSD", "timeframe": "H1", "count": 200,
        "params_json": '{"fast":10,"slow":30}',
    })
    assert data["strategy"] == "sma_cross"
    assert data["provider"] == "dukascopy"
    assert data["fill_model"] in ("next_open", "current_close")
    assert data["margin"] == pytest.approx(0.02)
    assert data["quote_currency"] == "USD"
    assert data["account_currency"] == "USD"
    assert "spread_relative" in data and "pip_value_per_lot_account" in data
    assert data["total_trades"] == len(data["trades"])


def test_tool_rejects_symbol_in_params(tmp_path):
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_backtest_run", {
        "strategy": "breakout", "symbol": "USDJPY", "count": 200,
        "params_json": '{"symbol":"EURUSD","lookback":2,"rr":1.0,"risk_amount":100}',
    })
    assert "managed by the tool" in text


def test_tool_bad_strategy_raises(tmp_path):
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_backtest_run", {"strategy": "nope", "symbol": "EURUSD"})
    assert "Unknown strategy" in text


def test_tool_bad_params_raise_toolerror(tmp_path):
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_backtest_run", {
        "strategy": "sma_cross", "symbol": "EURUSD", "params_json": '{"nonexistent":1}',
    })
    assert "Backtest failed" in text


def test_tool_invariant_fast_lt_slow_raises(tmp_path):
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_backtest_run", {
        "strategy": "sma_cross", "symbol": "EURUSD", "params_json": '{"fast":30,"slow":10}',
    })
    assert "fast < slow" in text


def test_tool_invariant_bad_breakout_params_raise(tmp_path):
    mcp = _build(tmp_path)
    for bad in ['{"rr":-1}', '{"risk_amount":0}', '{"lookback":0}']:
        text = _error(mcp, "tv_backtest_run", {"strategy": "breakout", "symbol": "EURUSD", "params_json": bad})
        assert "lookback" in text or "risk_amount" in text or "rr" in text


def test_tool_currency_mismatch_requires_rate(tmp_path):
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_backtest_run", {
        "strategy": "breakout", "symbol": "USDJPY", "count": 200,
        "params_json": '{"lookback":2,"rr":1.0,"risk_amount":100}', "account_currency": "USD",
    })
    assert "quote_to_account_rate" in text


def test_tool_currency_mismatch_ok_with_rate(tmp_path):
    mcp = _build(tmp_path, df=_crafted_usdjpy())
    data = _data(mcp, "tv_backtest_run", {
        "strategy": "breakout", "symbol": "USDJPY", "count": 200,
        "params_json": '{"lookback":2,"rr":1.0,"risk_amount":100}',
        "account_currency": "USD", "quote_to_account_rate": 1 / 150.0,
    })
    assert data["quote_currency"] == "JPY"
    assert data["quote_to_account_rate"] == pytest.approx(1 / 150.0)


def test_tool_bad_account_currency_raises(tmp_path):
    mcp = _build(tmp_path)
    for bad in ("US", "USDX", "12D"):
        text = _error(mcp, "tv_backtest_run", {"strategy": "sma_cross", "account_currency": bad})
        assert "3-letter" in text


def test_tool_nonpositive_rate_raises(tmp_path):
    mcp = _build(tmp_path, df=_crafted_usdjpy())
    for bad in (0.0, -1.0):
        text = _error(mcp, "tv_backtest_run", {
            "strategy": "breakout", "symbol": "USDJPY",
            "account_currency": "USD", "quote_to_account_rate": bad,
        })
        assert "finite number > 0" in text

