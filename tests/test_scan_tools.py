"""Offline tool-level tests for the `scan` toolset: mocked bar loader, no network.

Uses an injected loader so each MCP tool can be exercised end-to-end without
touching Dukascopy/OANDA. Covers result shape, bars_scanned == len(df), provider
reporting, result truncation, and ToolError paths (bad session, missing Custom
times, malformed timeframe).
"""

import asyncio
import json

import numpy as np
import pandas as pd
import pytest
from fastmcp import FastMCP

from tvmcp import scan
from tvmcp.config import Settings
from tvmcp.symbols import resolve, resolve_timeframe


def _settings(tmp_path) -> Settings:
    return Settings(
        toolsets=frozenset({"scan"}),
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


def _make_df(n=60) -> pd.DataFrame:
    times = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.RandomState(1)
    trend = 1.10 + np.linspace(0, 0.02, n) + 0.005 * np.sin(np.linspace(0, 10 * np.pi, n))
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


def _loader(df: pd.DataFrame):
    def load(symbol, timeframe, count, provider):
        try:
            sym, tf = resolve(symbol), resolve_timeframe(timeframe)
        except ValueError as exc:
            from fastmcp.exceptions import ToolError

            raise ToolError(str(exc)) from exc
        return sym, tf, df.tail(count).reset_index(drop=True)

    return load


def _build(tmp_path, df=None, n=60):
    mcp = FastMCP(name="test")
    settings = _settings(tmp_path)
    scan.register(mcp, settings, loader=_loader(df if df is not None else _make_df(n)))
    return mcp


def _call(mcp, name, args):
    return asyncio.run(mcp.call_tool(name, args))


def _data(mcp, name, args):
    r = _call(mcp, name, args)
    return json.loads(r.content[0].text)


def _error(mcp, name, args):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as ei:
        _call(mcp, name, args)
    return str(ei.value)


def test_fvg_bars_scanned_matches_len(tmp_path):
    df = _make_df(60)
    data = _data(_build(tmp_path, df), "tv_scan_fvg", {"symbol": "EURUSD", "timeframe": "H1", "count": 500})
    assert data["bars_scanned"] == len(df) == 60
    assert data["symbol"] == "EURUSD"
    assert data["provider"] == "dukascopy"  # no OANDA key -> auto picks dukascopy
    assert data["detector"] == "fvg"
    assert "total_count" in data and "returned_count" in data and "truncated" in data


@pytest.mark.parametrize(
    "tool", ["tv_scan_fvg", "tv_scan_ob", "tv_scan_structure", "tv_scan_liquidity"]
)
def test_list_tools_report_provider_and_counts(tmp_path, tool):
    data = _data(_build(tmp_path), tool, {"symbol": "EURUSD", "timeframe": "H1", "count": 500})
    assert data["provider"] in {"dukascopy", "oanda"}
    assert data["timeframe"] == "H1"
    assert data["bars_scanned"] > 0
    assert isinstance(data["results"], list)
    assert data["total_count"] == len(data["results"])  # few results -> no truncation


def test_truncation_bounds_results(tmp_path):
    # fvg on a long trending series yields many gaps; ensure only _MAX_RESULTS returned
    data = _data(_build(tmp_path, _make_df(400), n=400), "tv_scan_fvg", {"symbol": "EURUSD", "timeframe": "H1", "count": 400})
    assert data["returned_count"] <= 100
    assert data["returned_count"] <= data["total_count"]
    assert data["truncated"] == (data["total_count"] > 100)


def test_sessions_blocks_reported(tmp_path):
    data = _data(_build(tmp_path), "tv_scan_sessions", {"symbol": "EURUSD", "timeframe": "H1", "count": 500})
    assert data["detector"] == "sessions"
    assert "blocks" in data
    assert data["total_count"] == len(data["blocks"])


def test_prev_hl_returns_levels(tmp_path):
    data = _data(_build(tmp_path), "tv_scan_prev_hl", {"symbol": "EURUSD", "timeframe": "H1", "count": 500})
    assert "previous_high" in data and "previous_low" in data
    assert data["provider"] == "dukascopy"


def test_sessions_unknown_name_raises_toolerror(tmp_path):
    text = _error(_build(tmp_path), "tv_scan_sessions", {"symbol": "EURUSD", "session": "Nope"})
    assert "Unknown session" in text


def test_sessions_custom_missing_times_raises_toolerror(tmp_path):
    text = _error(_build(tmp_path), "tv_scan_sessions", {"symbol": "EURUSD", "session": "Custom"})
    assert "requires start_time and end_time" in text


def test_sessions_custom_bad_time_raises_toolerror(tmp_path):
    text = _error(_build(tmp_path), "tv_scan_sessions", {
        "symbol": "EURUSD", "session": "Custom", "start_time": "25:99", "end_time": "10:00",
    })
    assert "HH:MM" in text


def test_malformed_timeframe_raises_toolerror(tmp_path):
    text = _error(_build(tmp_path), "tv_scan_fvg", {"symbol": "EURUSD", "timeframe": "7m"})
    assert "Unknown timeframe" in text






