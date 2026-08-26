"""Offline tests for the `strategy` toolset (declarative YAML, mocked loader)."""

import asyncio
import json

import numpy as np
import pandas as pd
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import strategy
from tvmcp.config import Settings
from tvmcp.symbols import resolve, resolve_timeframe


def _settings(tmp_path) -> Settings:
    return Settings(
        toolsets=frozenset({"strategy"}),
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


def _loader(df=None):
    data = df if df is not None else _make_df()

    def load(symbol, timeframe, count, provider):
        sym, tf = resolve(symbol), resolve_timeframe(timeframe)
        return sym, tf, data.tail(count).reset_index(drop=True)

    return load


def _write_spec(tmp_path, filename, body):
    (tmp_path / "strategies").mkdir(parents=True, exist_ok=True)
    (tmp_path / "strategies" / filename).write_text(body, encoding="utf-8")


def _data(mcp, name, args):
    r = asyncio.run(mcp.call_tool(name, args))
    return json.loads(r.content[0].text)


def _error(mcp, name, args):
    with pytest.raises(ToolError) as ei:
        asyncio.run(mcp.call_tool(name, args))
    return str(ei.value)


def _build(tmp_path, df=None):
    mcp = FastMCP(name="test")
    strategy.register(mcp, _settings(tmp_path), loader=_loader(df))
    return mcp


GOOD = """name: my_breakout
description: breakout with wider rr
strategy: breakout
params:
  lookback: 25
  rr: 3.0
  risk_amount: 150
"""


def test_strategy_gated_off_by_default(tmp_path):
    from tvmcp.server import build_server

    settings = _settings(tmp_path)
    settings = Settings(**{**settings.__dict__, "toolsets": frozenset({"public", "data"})})
    mcp = build_server(settings)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "tv_strategy_run" not in names


def test_registers_two_strategy_tools(tmp_path):
    mcp = _build(tmp_path)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"tv_strategy_list", "tv_strategy_run"} <= names


def test_list_empty_dir(tmp_path):
    (tmp_path / "strategies").mkdir(parents=True, exist_ok=True)
    data = _data(_build(tmp_path), "tv_strategy_list", {})
    assert data["count"] == 0


def test_list_reports_specs_and_errors(tmp_path):
    _write_spec(tmp_path, "good.yaml", GOOD)
    _write_spec(tmp_path, "bad.yaml", "name: bad\nstrategy: nonexistent\n")
    data = _data(_build(tmp_path), "tv_strategy_list", {})
    by_file = {s["file"]: s for s in data["strategies"]}
    assert by_file["good.yaml"]["strategy"] == "breakout"
    assert by_file["good.yaml"]["params"]["rr"] == 3.0
    assert "unknown strategy" in by_file["bad.yaml"]["error"]


def test_run_uses_spec_and_returns_result(tmp_path):
    _write_spec(tmp_path, "good.yaml", GOOD)
    mcp = _build(tmp_path)
    data = _data(mcp, "tv_strategy_run", {"name": "good.yaml", "symbol": "EURUSD", "count": 200})
    assert data["strategy_name"] == "my_breakout"
    assert data["strategy"] == "breakout"
    assert data["params"]["lookback"] == 25
    assert data["provider"] == "dukascopy"
    assert "summary" in data and "Equity Final [$]" in data["summary"]


def test_run_params_json_overrides_spec(tmp_path):
    _write_spec(tmp_path, "good.yaml", GOOD)
    mcp = _build(tmp_path)
    data = _data(mcp, "tv_strategy_run", {"name": "good.yaml", "count": 200, "params_json": '{"rr":1.0}'})
    assert data["params"]["rr"] == 1.0
    assert data["params"]["lookback"] == 25  # spec default preserved


def test_run_unknown_name_raises(tmp_path):
    (tmp_path / "strategies").mkdir(parents=True, exist_ok=True)
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_strategy_run", {"name": "missing.yaml"})
    assert "No strategy spec" in text


def test_run_path_traversal_raises(tmp_path):
    _write_spec(tmp_path, "good.yaml", GOOD)
    (tmp_path / "outside.yaml").write_text(GOOD, encoding="utf-8")
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_strategy_run", {"name": "..\\outside.yaml"})
    assert "directly inside" in text


def test_run_rejects_unknown_strategy_in_spec(tmp_path):
    _write_spec(tmp_path, "bad.yaml", "name: x\nstrategy: nope\n")
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_strategy_run", {"name": "bad.yaml"})
    assert "unknown strategy" in text


def test_run_rejects_symbol_in_params(tmp_path):
    _write_spec(tmp_path, "s.yaml", "name: s\nstrategy: sma_cross\nparams:\n  symbol: EURUSD\n")
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_strategy_run", {"name": "s.yaml"})
    assert "managed by the tool" in text


def test_run_invalid_params_json_raises(tmp_path):
    _write_spec(tmp_path, "good.yaml", GOOD)
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_strategy_run", {"name": "good.yaml", "params_json": "{bad"})
    assert "params_json" in text


def test_run_rejects_bad_invariant_via_spec(tmp_path):
    _write_spec(tmp_path, "badinv.yaml", "name: b\nstrategy: sma_cross\nparams:\n  fast: 30\n  slow: 10\n")
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_strategy_run", {"name": "badinv.yaml"})
    assert "fast < slow" in text