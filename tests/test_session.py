"""Credential-free tests for the opt-in `session` toolset (mocked provider, no TV).

Verifies: gating (off by default), first-use ToS warning printed once, actionable
missing-TV_SESSIONID error, provider='session' output, cache-backed loader plumbing,
and the realtime snapshot path. The live tvdatafeed-enhanced client is never touched.
"""

import asyncio
import json

import numpy as np
import pandas as pd
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import session
from tvmcp.config import Settings
from tvmcp.symbols import resolve, resolve_timeframe


def _settings(tmp_path, session_id=None) -> Settings:
    return Settings(
        toolsets=frozenset({"session"}),
        extra_tools=frozenset(),
        read_only=False,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=None,
        oanda_env="practice",
        session_id=session_id,
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


@pytest.fixture(autouse=True)
def _reset_warning():
    from tvmcp.session import warnings as w

    w._printed = False
    yield
    w._printed = False


def _loader(df=None, capture=None):
    data = df if df is not None else _make_df()

    def load(symbol, timeframe, count):
        if capture is not None:
            capture.append(count)
        sym, tf = resolve(symbol), resolve_timeframe(timeframe)
        return sym, tf, data.tail(count).reset_index(drop=True)

    return load


def _realtime():
    def quote(symbol):
        return {"time": "2026-01-01T12:00:00Z", "price": 1.1150, "bid": 1.1149, "ask": 1.1151}

    return quote


def _data(mcp, name, args):
    r = asyncio.run(mcp.call_tool(name, args))
    return json.loads(r.content[0].text)


def _error(mcp, name, args):
    with pytest.raises(ToolError) as ei:
        asyncio.run(mcp.call_tool(name, args))
    return str(ei.value)


def _build(tmp_path, session_id="cookie", loader=None, realtime=None):
    mcp = FastMCP(name="test")
    session.register(mcp, _settings(tmp_path, session_id), loader=loader, realtime=realtime)
    return mcp


def test_gating_off_by_default(tmp_path):
    from tvmcp.server import build_server

    settings = _settings(tmp_path, session_id=None)
    settings = Settings(**{**settings.__dict__, "toolsets": frozenset({"public", "data"})})
    mcp = build_server(settings)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "tv_session_ohlcv" not in names


def test_registers_three_session_tools(tmp_path):
    mcp = _build(tmp_path, loader=_loader())
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"tv_session_status", "tv_session_ohlcv", "tv_session_realtime"} <= names


def test_missing_sessionid_raises_actionable(tmp_path, capsys):
    mcp = _build(tmp_path, session_id=None)  # default loader raises
    text = _error(mcp, "tv_session_ohlcv", {"symbol": "EURUSD"})
    assert "TV_SESSIONID" in text and "opt-in" in text
    assert "TradingView" in capsys.readouterr().err  # warning printed on first use


def test_ohlcv_returns_session_bars(tmp_path, capsys):
    df = _make_df(30)
    mcp = _build(tmp_path, loader=_loader(df))
    data = _data(mcp, "tv_session_ohlcv", {"symbol": "EURUSD", "timeframe": "H1", "count": 30})
    assert data["provider"] == "session"
    assert data["symbol"] == "EURUSD"
    assert data["count"] == 30
    assert len(data["bars"][0]) == 6
    err = capsys.readouterr().err
    assert "TradingView" in err and "opt-in" in err  # ToS warning on first use


def test_warning_printed_once(tmp_path, capsys):
    mcp = _build(tmp_path, loader=_loader())
    _data(mcp, "tv_session_ohlcv", {"symbol": "EURUSD"})
    _data(mcp, "tv_session_ohlcv", {"symbol": "GBPUSD"})
    _data(mcp, "tv_session_status", {})
    assert capsys.readouterr().err.count("WARNING") == 1


def test_realtime_quote(tmp_path):
    mcp = _build(tmp_path, realtime=_realtime())
    data = _data(mcp, "tv_session_realtime", {"symbol": "EURUSD"})
    assert data["provider"] == "session"
    assert data["price"] == pytest.approx(1.1150)
    assert "bid" in data and "ask" in data


def test_status_reports_configured_state(tmp_path):
    mcp = _build(tmp_path, session_id=None)
    data = _data(mcp, "tv_session_status", {})
    assert data["session_id_set"] is False
    assert data["usable"] is False
    mcp2 = _build(tmp_path, session_id="x")
    data2 = _data(mcp2, "tv_session_status", {})
    assert data2["session_id_set"] is True


def test_ohlcv_requires_session_even_with_injected_loader(tmp_path):
    # the gate lives in the tool, not the loader - injected loaders cannot bypass it
    mcp = _build(tmp_path, session_id=None, loader=_loader())
    text = _error(mcp, "tv_session_ohlcv", {"symbol": "EURUSD"})
    assert "TV_SESSIONID" in text


def test_realtime_requires_session_even_with_injected_provider(tmp_path):
    mcp = _build(tmp_path, session_id=None, realtime=_realtime())
    text = _error(mcp, "tv_session_realtime", {"symbol": "EURUSD"})
    assert "TV_SESSIONID" in text


def test_realtime_metadata_is_authoritative(tmp_path):
    # a provider payload must not be able to spoof symbol/provider
    def evil(symbol):
        return {"symbol": "EVIL", "tv_symbol": "X:EVIL", "provider": "evil", "price": 1.11}

    mcp = _build(tmp_path, realtime=evil)
    data = _data(mcp, "tv_session_realtime", {"symbol": "EURUSD"})
    assert data["symbol"] == "EURUSD"
    assert data["provider"] == "session"
    assert data["price"] == pytest.approx(1.11)


def test_ohlcv_honors_settings_max_bars(tmp_path):
    captured = []
    mcp = FastMCP(name="test")
    settings = _settings(tmp_path, session_id="cookie")
    settings = Settings(**{**settings.__dict__, "max_bars": 25})
    session.register(mcp, settings, loader=_loader(_make_df(60), capture=captured))
    data = _data(mcp, "tv_session_ohlcv", {"symbol": "EURUSD", "timeframe": "H1", "count": 500})
    assert captured[-1] == 25
    assert data["count"] == 25

def test_client_find_auth_token_nested():
    from tvmcp.session.client import _find_auth_token

    assert _find_auth_token({"user": {"auth_token": "jwt123"}}) == "jwt123"
    assert _find_auth_token({"authToken": "jwtX"}) == "jwtX"
    assert _find_auth_token({"user": {"name": "x"}}) is None


def test_client_interval_map_covers_all_timeframes():
    from tvmcp.session.client import _INTERVALS
    from tvmcp.symbols import _TIMEFRAMES as TIMEFRAMES

    assert set(_INTERVALS) == set(TIMEFRAMES)

