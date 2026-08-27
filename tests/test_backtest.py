"""Backtest engine + toolset tests. Engine tests are offline (synthetic bars);
tool tests use a mocked loader (no network)."""

import asyncio
import json
from pathlib import Path

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


def _make_m15_df(n=2000, seed=7) -> pd.DataFrame:
    """Volatile M15 random-walk with drift: produces H4 structure breaks and M15 FVGs."""
    times = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    rng = np.random.RandomState(seed)
    steps = rng.normal(0.00002, 0.0011, n)
    cl = 1.10 + np.cumsum(steps) + 0.006 * np.sin(np.linspace(0, 8 * np.pi, n))
    op = np.roll(cl, 1)
    op[0] = cl[0]
    return pd.DataFrame(
        {
            "time": times,
            "open": op,
            "high": np.maximum(op, cl) + rng.uniform(0, 0.0008, n),
            "low": np.minimum(op, cl) - rng.uniform(0, 0.0008, n),
            "close": cl,
            "volume": 100 + rng.rand(n) * 50,
        }
    )


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
    assert t["tp"] is not None
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


def test_engine_runs_smc_h4_m15():
    df = _make_m15_df()
    summary, trades, meta = engine.run(
        df, "smc_h4_m15", {}, "EURUSD", cash=10000, spread_pips=1.0, margin=0.02
    )
    assert summary["# Trades"] >= 1, "synthetic walk should produce at least one SMC trade"
    for t in trades:
        assert t["sl"] is not None
        assert t["r"] is not None
        # risk-sized: a full stop-out is ~-1R (spread/gap tolerance)
        assert t["r"] > -1.6


def test_smc_h4_m15_no_lookahead_prefix_stability():
    """Trades in the common history must be identical when later bars are removed -
    any dependence on future data (FVG mitigation, later H4 breaks) would change them."""
    full = _make_m15_df(n=2000)
    short = full.iloc[:1500].reset_index(drop=True)
    _, trades_full, _ = engine.run(full, "smc_h4_m15", {}, "EURUSD", cash=10000, margin=0.02)
    _, trades_short, _ = engine.run(short, "smc_h4_m15", {}, "EURUSD", cash=10000, margin=0.02)
    # compare only trades fully closed well inside the short window
    cutoff = short["time"].iloc[-200].isoformat()
    key = lambda t: (t["entry_time"], t["direction"], t["entry_price"], t["sl"])
    a = [key(t) for t in trades_full if t["exit_time"] < cutoff]
    b = [key(t) for t in trades_short if t["exit_time"] < cutoff]
    assert a == b


def test_tool_invariant_bad_smc_params_raise(tmp_path):
    mcp = _build(tmp_path)
    for bad in ['{"swing_length":1}', '{"rr":0}', '{"risk_amount":-5}', '{"expiry_bars":0}']:
        text = _error(mcp, "tv_backtest_run", {
            "strategy": "smc_h4_m15", "symbol": "EURUSD", "params_json": bad,
        })
        assert "smc_h4_m15 param" in text


def test_tool_smc_rejects_h4_and_slower_timeframes(tmp_path):
    mcp = _build(tmp_path, df=_make_m15_df(n=300))
    text = _error(mcp, "tv_backtest_run", {
        "strategy": "smc_h4_m15", "symbol": "EURUSD", "timeframe": "H4", "count": 300,
    })
    assert "below H4" in text


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


# ---------------- trade rendering (fake renderer, no Chromium) ----------------

def _fake_renderer(specs: list):
    def draw(spec, out_path):
        specs.append(spec)
        Path(out_path).write_bytes(b"\x89PNG fake")

    return draw


def _build_with_renderer(tmp_path, df, specs):
    mcp = FastMCP(name="test")
    backtest.register(mcp, _settings(tmp_path), loader=_loader(df), renderer=_fake_renderer(specs))
    return mcp


def _flat_df(n=60) -> pd.DataFrame:
    times = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({
        "time": times, "open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 100.0,
    })


def test_tool_render_trades_writes_png_with_trade_markup(tmp_path):
    specs: list = []
    mcp = _build_with_renderer(tmp_path, _crafted_eurusd(), specs)
    data = _data(mcp, "tv_backtest_render_trades", {
        "strategy": "breakout", "symbol": "EURUSD", "count": 200,
        "params_json": json.dumps(_breakout_params()),
    })
    assert data["total_trades"] == 1 and data["rendered_count"] == 1
    rec = data["rendered"][0]
    assert rec["path"].endswith(".png") and Path(rec["path"]).exists()
    assert str(Path(rec["path"]).parent) == str(tmp_path / "charts")
    assert rec["sl"] is not None and rec["tp"] is not None

    spec = specs[0]
    types = sorted(m["type"] for m in spec["markup"])
    # trade lifetime band + entry line + SL line + TP line + exit line
    assert types == ["killzone", "line", "line", "line", "line"]
    levels = {m["level"] for m in spec["markup"] if "level" in m}
    assert rec["entry_price"] in levels and rec["sl"] in levels and rec["tp"] in levels
    colors = {m.get("label"): m.get("color") for m in spec["markup"] if m.get("label") in ("SL", "TP")}
    assert colors == {"SL": "#e53935", "TP": "#43a047"}
    assert spec["bars"], "render window must contain bars"


def test_tool_render_trades_layers_extra_markup(tmp_path):
    specs: list = []
    mcp = _build_with_renderer(tmp_path, _crafted_eurusd(), specs)
    extra = json.dumps({"version": 1, "markup": [
        {"type": "text", "time": "2026-01-01T03:00:00Z", "price": 1.1005, "text": "breakout bar", "color": "#ff9800"},
        {"type": "marker", "time": "2026-01-01T04:00:00Z", "price": 1.1010, "direction": "up"},
    ]})
    data = _data(mcp, "tv_backtest_render_trades", {
        "strategy": "breakout", "symbol": "EURUSD", "count": 200,
        "params_json": json.dumps(_breakout_params()), "extra_markup_json": extra,
    })
    assert data["rendered_count"] == 1
    types = [m["type"] for m in specs[0]["markup"]]
    assert "text" in types and "marker" in types
    txt = next(m for m in specs[0]["markup"] if m["type"] == "text")
    assert txt["text"] == "breakout bar" and txt["color"] == "#ff9800"


def test_tool_render_trades_bad_extra_markup_raises(tmp_path):
    specs: list = []
    mcp = _build_with_renderer(tmp_path, _crafted_eurusd(), specs)
    text = _error(mcp, "tv_backtest_render_trades", {
        "strategy": "breakout", "symbol": "EURUSD", "count": 200,
        "params_json": json.dumps(_breakout_params()),
        "extra_markup_json": '{"version":1,"markup":[{"type":"wibble"}]}',
    })
    assert "invalid markup_json" in text


def test_tool_render_trades_no_trades_notes_and_writes_nothing(tmp_path):
    specs: list = []
    mcp = _build_with_renderer(tmp_path, _flat_df(), specs)
    data = _data(mcp, "tv_backtest_render_trades", {
        "strategy": "breakout", "symbol": "EURUSD", "count": 60,
        "params_json": json.dumps(_breakout_params()),
    })
    assert data["total_trades"] == 0 and data["rendered"] == []
    assert "nothing to render" in data["note"]
    assert specs == []


def test_tool_render_trades_respects_max_renders(tmp_path):
    specs: list = []
    mcp = _build_with_renderer(tmp_path, _make_df(400), specs)
    data = _data(mcp, "tv_backtest_render_trades", {
        "strategy": "breakout", "symbol": "EURUSD", "count": 400,
        "params_json": json.dumps(_breakout_params()), "max_renders": 2,
    })
    assert data["total_trades"] >= 3, "trend series should produce several trades"
    assert data["rendered_count"] == 2 == len(specs)
    # rendered trades are the most recent ones (list is oldest-first)
    times = [r["entry_time"] for r in data["rendered"]]
    assert times == sorted(times)

