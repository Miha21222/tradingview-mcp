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


def _loader(df: pd.DataFrame, seen_ends: list | None = None):
    def load(symbol, timeframe, count, provider, end_ts=None):
        if seen_ends is not None:
            seen_ends.append(end_ts)
        sym, tf = resolve(symbol), resolve_timeframe(timeframe)
        sliced = df if end_ts is None else df[df["time"] <= end_ts]
        return sym, tf, sliced.tail(count).reset_index(drop=True)

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


def test_end_time_windows_history(tmp_path):
    df = _make_df(60)
    seen_ends: list = []
    calls: list = []
    mcp = FastMCP(name="test")
    chart.register(mcp, _settings(tmp_path), loader=_loader(df, seen_ends), renderer=_fake_renderer(calls))
    anchor = df["time"].iloc[39].isoformat()
    data = _data(mcp, "tv_chart_render", {
        "symbol": "EURUSD", "timeframe": "H1", "count": 30, "end_time": anchor,
    })
    assert seen_ends[0] == df["time"].iloc[39]
    assert data["end_time"] == anchor  # chart really ends at the anchor, not now
    assert data["bars"] == 30


def test_end_time_naive_treated_as_utc(tmp_path):
    df = _make_df(60)
    seen_ends: list = []
    calls: list = []
    mcp = FastMCP(name="test")
    chart.register(mcp, _settings(tmp_path), loader=_loader(df, seen_ends), renderer=_fake_renderer(calls))
    _data(mcp, "tv_chart_render", {
        "symbol": "EURUSD", "timeframe": "H1", "count": 30, "end_time": "2026-01-02 12:00:00",
    })
    assert seen_ends[0] == pd.Timestamp("2026-01-02T12:00:00Z")


def test_bad_end_time_raises(tmp_path):
    mcp, _ = _build(tmp_path)
    text = _error(mcp, "tv_chart_render", {
        "symbol": "EURUSD", "timeframe": "H1", "count": 60, "end_time": "not-a-time",
    })
    assert "end_time" in text


def test_future_end_time_raises(tmp_path):
    mcp, _ = _build(tmp_path)
    text = _error(mcp, "tv_chart_render", {
        "symbol": "EURUSD", "timeframe": "H1", "count": 60, "end_time": "2999-01-01T00:00:00Z",
    })
    assert "future" in text


def test_new_markup_types_reach_renderer(tmp_path):
    mcp, calls = _build(tmp_path)
    markup = json.dumps({"version": 1, "markup": [
        {"type": "text", "time": "2026-01-02T00:00:00Z", "price": 1.105, "text": "note", "color": "#112233"},
        {"type": "marker", "time": "2026-01-02T01:00:00Z", "price": 1.106, "direction": "down", "label": "exit"},
        {"type": "fvg", "time": "2026-01-02T02:00:00Z", "direction": "bullish",
         "top": 1.107, "bottom": 1.106, "color": "#ff9800", "label": "FVG"},
    ]})
    data = _data(mcp, "tv_chart_render", {
        "symbol": "EURUSD", "timeframe": "H1", "count": 60, "markup_json": markup,
    })
    assert data["markup_count"] == 3
    by_type = {m["type"]: m for m in calls[0]["markup"]}
    assert by_type["text"]["text"] == "note" and by_type["text"]["color"] == "#112233"
    assert by_type["marker"]["direction"] == "down" and by_type["marker"]["label"] == "exit"
    assert by_type["fvg"]["color"] == "#ff9800" and by_type["fvg"]["label"] == "FVG"






