"""Offline tool-level tests for the `chart` toolset: mocked bar loader + renderer.

No browser/network. Verifies result shape, managed output dir with collision-safe
filenames, renderer invocation, provider reporting, and ToolError paths (markup
time outside the loaded range, too few bars, invalid markup_json).
"""

import asyncio
import json

import numpy as np
import pandas as pd
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import chart
from tvmcp.config import Settings
from tvmcp.symbols import resolve, resolve_timeframe


def _settings(tmp_path) -> Settings:
    return Settings(
        toolsets=frozenset({"chart"}),
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
        sym, tf = resolve(symbol), resolve_timeframe(timeframe)
        return sym, tf, df.tail(count).reset_index(drop=True)

    return load


def _fake_renderer(calls):
    def render(spec, out_path):
        calls.append(spec)
        out_path.write_bytes(b"\x89PNG-fake")

    return render


def _build(tmp_path, df=None, n=60):
    calls = []
    mcp = FastMCP(name="test")
    settings = _settings(tmp_path)
    chart.register(
        mcp, settings,
        loader=_loader(df if df is not None else _make_df(n)),
        renderer=_fake_renderer(calls),
    )
    return mcp, calls


def _data(mcp, name, args):
    r = asyncio.run(mcp.call_tool(name, args))
    return json.loads(r.content[0].text)


def _error(mcp, name, args):
    with pytest.raises(ToolError) as ei:
        asyncio.run(mcp.call_tool(name, args))
    return str(ei.value)


GOOD = (
    '{"version":1,"grid":true,"markup":['
    '{"type":"fvg","time":"2026-01-01T06:00:00Z","direction":"bullish","top":1.105,"bottom":1.098},'
    '{"type":"bos","time":"2026-01-02T08:00:00Z","level":1.11,"label":"BOS"}]}'
)


def test_render_returns_path_and_metadata(tmp_path):
    mcp, calls = _build(tmp_path)
    data = _data(mcp, "tv_chart_render", {
        "symbol": "EURUSD", "timeframe": "H1", "count": 60, "markup_json": GOOD,
    })
    assert data["symbol"] == "EURUSD"
    assert data["provider"] == "dukascopy"
    assert data["timeframe"] == "H1"
    assert data["markup_count"] == 2
    assert data["path"].endswith(".png")
    assert data["path"].startswith(str(tmp_path / "charts"))
    assert len(calls) == 1
    assert calls[0]["markup"][0]["type"] == "fvg"
    assert calls[0]["bars"]


def test_collision_safe_unique_filenames(tmp_path):
    mcp, _ = _build(tmp_path)
    a = _data(mcp, "tv_chart_render", {"symbol": "EURUSD", "timeframe": "H1", "count": 60})
    b = _data(mcp, "tv_chart_render", {"symbol": "EURUSD", "timeframe": "H1", "count": 60})
    assert a["path"] != b["path"]


def test_no_caller_controlled_path(tmp_path):
    # markup / args cannot set the output path; output is always under chart_dir
    mcp, calls = _build(tmp_path)
    data = _data(mcp, "tv_chart_render", {"symbol": "EURUSD", "timeframe": "H1", "count": 60})
    assert data["path"].startswith(str(tmp_path / "charts"))


def test_markup_out_of_range_raises_toolerror(tmp_path):
    mcp, _ = _build(tmp_path)
    bad = '{"version":1,"markup":[{"type":"fvg","time":"2000-01-01T00:00:00Z","direction":"bullish","top":1.1,"bottom":1.09}]}'
    text = _error(mcp, "tv_chart_render", {"symbol": "EURUSD", "timeframe": "H1", "count": 60, "markup_json": bad})
    assert "outside the loaded bar range" in text


def test_too_few_bars_raises_toolerror(tmp_path):
    mcp, _ = _build(tmp_path, _make_df(10), n=10)
    text = _error(mcp, "tv_chart_render", {"symbol": "EURUSD", "timeframe": "H1", "count": 60})
    assert "need >= 20" in text


def test_invalid_markup_json_raises_toolerror(tmp_path):
    mcp, _ = _build(tmp_path)
    text = _error(mcp, "tv_chart_render", {"symbol": "EURUSD", "timeframe": "H1", "count": 60, "markup_json": "{bad"})
    assert "markup_json" in text


def test_empty_markup_renders_candles_only(tmp_path):
    mcp, calls = _build(tmp_path)
    data = _data(mcp, "tv_chart_render", {"symbol": "EURUSD", "timeframe": "H1", "count": 60, "markup_json": ""})
    assert data["markup_count"] == 0
    assert calls[0]["markup"] == []






