"""M8: `scan/levels.py` (pure pandas) and the `tv_scan_check_levels` tool.

Covers: tagged from below / from above / zone, untagged with closest approach
(side + distance + time), coverage warnings (history starts after `since`, no
bars after `since`, stale last bar), `parse_since` for ISO and `session:` forms
(fixed-UTC table, date suffix, most-recent-start rule, flooring to timeframe),
level validation, and the loader-injected scan tool naming its provider.
"""

import asyncio
import json
from datetime import datetime, timezone

import pandas as pd
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import scan
from tvmcp.config import Settings
from tvmcp.scan import levels as L
from tvmcp.scan.detectors import SESSION_WINDOWS_UTC
from tvmcp.symbols import resolve, resolve_timeframe

T0 = pd.Timestamp("2026-09-18T13:00:00Z")


def _df(rows, start=T0, minutes=15):
    """rows = [(open, high, low, close), ...] on a regular grid from `start`."""
    times = [start + pd.Timedelta(minutes=minutes * i) for i in range(len(rows))]
    return pd.DataFrame({
        "time": times,
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
        "volume": [1] * len(rows),
    })


NOW = T0 + pd.Timedelta(minutes=15 * 6)  # right after the 6th bar closes


def test_tagged_from_below():
    df = _df([(100, 101, 99, 100.5), (100.5, 102, 100, 101.5), (101.5, 104, 101, 103.5),
              (103.5, 105, 103, 104), (104, 104.5, 103, 103.5), (103.5, 104, 103, 103.8)])
    [r] = L.check_levels(df, [{"name": "IBH", "price": 103.0}], T0, 15, now=NOW)
    assert r["tagged"] is True
    assert r["first_tag"] == {"time": "2026-09-18T13:30:00Z", "from": "below",
                              "high": 104.0, "low": 101.0}
    assert r["closest_approach"] == {"distance": 0.0, "time": "2026-09-18T13:30:00Z", "side": "below"}
    assert r["coverage_warning"] is None and r["price"] == 103.0


def test_tagged_from_above_and_zone():
    df = _df([(110, 111, 109, 110), (110, 110.5, 108, 108.5), (108.5, 109, 105.5, 106),
              (106, 107, 105, 106.5), (106.5, 107, 106, 106.8), (106.8, 107, 106, 106.5)])
    lvl, zone = L.check_levels(
        df, [{"name": "PDL", "price": 106.0}, {"name": "FVG", "low": 107.2, "high": 107.8}],
        T0, 15, now=NOW)
    assert lvl["tagged"] and lvl["first_tag"]["from"] == "above"
    assert lvl["first_tag"]["time"] == "2026-09-18T13:30:00Z"
    assert zone["kind"] == "zone" and zone["tagged"] is True
    assert zone["first_tag"]["time"] == "2026-09-18T13:30:00Z" and zone["first_tag"]["from"] == "above"


def test_first_bar_tag_uses_open_side():
    df = _df([(99, 101, 98, 100), (100, 100.5, 99.5, 100)])
    [r] = L.check_levels(df, [{"name": "x", "price": 100.5}], T0, 15, now=T0 + pd.Timedelta(minutes=30))
    assert r["first_tag"]["from"] == "below"


def test_untagged_closest_approach_below_and_above():
    df = _df([(100, 101, 99, 100.5), (100.5, 102.4, 100, 101.5), (101.5, 102, 101, 101.8),
              (101.8, 102.2, 101.2, 101.5), (101.5, 101.9, 100.8, 101), (101, 101.5, 100.5, 101.2)])
    hi, lo = L.check_levels(
        df, [{"name": "res", "price": 103.0}, {"name": "sup", "price": 98.0}], T0, 15, now=NOW)
    assert hi["tagged"] is False and hi["first_tag"] is None
    assert hi["closest_approach"] == {"distance": pytest.approx(0.6), "time": "2026-09-18T13:15:00Z",
                                      "side": "below"}
    assert lo["closest_approach"]["side"] == "above"
    assert lo["closest_approach"]["distance"] == pytest.approx(1.0)  # low 99 vs 98 on bar 0


def test_only_bars_after_since_count():
    df = _df([(100, 105, 99, 104), (104, 104.5, 103, 103.5), (103.5, 104, 103, 103.8)])
    since = T0 + pd.Timedelta(minutes=15)
    [r] = L.check_levels(df, [{"name": "x", "price": 104.8}], since, 15, now=T0 + pd.Timedelta(minutes=45))
    assert r["tagged"] is False  # bar 0 tagged it, but bar 0 is before since
    assert r["closest_approach"]["side"] == "below"


def test_coverage_history_starts_after_since():
    df = _df([(100, 101, 99, 100.5), (100.5, 101, 100, 100.8)])
    since = T0 - pd.Timedelta(hours=3)
    [r] = L.check_levels(df, [{"name": "x", "price": 100.7}], since, 15, now=T0 + pd.Timedelta(minutes=30))
    assert r["tagged"] is True
    assert "history starts at 2026-09-18T13:00:00Z" in r["coverage_warning"]


def test_coverage_no_bars_after_since():
    df = _df([(100, 101, 99, 100.5), (100.5, 101, 100, 100.8)])
    since = T0 + pd.Timedelta(hours=5)
    [r] = L.check_levels(df, [{"name": "x", "price": 100.7}], since, 15, now=since + pd.Timedelta(minutes=15))
    assert r["tagged"] is False and r["closest_approach"] is None
    assert "no bars at or after" in r["coverage_warning"]


def test_coverage_stale_last_bar():
    df = _df([(100, 101, 99, 100.5), (100.5, 101, 100, 100.8)])
    now = T0 + pd.Timedelta(minutes=15 + 31)  # last bar opened 31 min > 2x15m ago
    [r] = L.check_levels(df, [{"name": "x", "price": 200}], T0, 15, now=now)
    assert "stale" in r["coverage_warning"]
    now_ok = T0 + pd.Timedelta(minutes=15 + 29)
    [r] = L.check_levels(df, [{"name": "x", "price": 200}], T0, 15, now=now_ok)
    assert r["coverage_warning"] is None


def test_empty_df_and_inferred_timeframe():
    assert L.check_levels(_df([]), [{"name": "x", "price": 1}], T0)[0]["coverage_warning"] == "no bars loaded"
    # bars WERE loaded but every one is older than since (daily chart on a Saturday)
    [r] = L.check_levels(_df([]), [{"name": "x", "price": 1}], T0,
                         last_loaded=int((T0 - pd.Timedelta(days=1)).timestamp()))
    assert r["coverage_warning"] == ("no bars at or after since=2026-09-18T13:00:00Z "
                                     "(last loaded bar 2026-09-17T13:00:00Z)")
    df = _df([(1, 2, 0, 1)] * 3, minutes=60)
    [r] = L.check_levels(df, [{"name": "x", "price": 1}], T0, now=T0 + pd.Timedelta(hours=2, minutes=100))
    assert r["coverage_warning"] is None  # inferred 60m: 100 min < 2x60


def test_level_validation():
    with pytest.raises(ToolError, match="non-empty"):
        L.normalize_levels([])
    with pytest.raises(ToolError, match="price or both high and low"):
        L.normalize_levels([{"name": "x"}])
    with pytest.raises(ToolError, match="must be a number"):
        L.normalize_levels([{"name": "x", "price": "abc"}])
    with pytest.raises(ToolError, match="At most 50"):
        L.normalize_levels([{"name": "x", "price": 1}] * 51)
    [z] = L.normalize_levels([{"name": "z", "high": 1.0, "low": 2.0}])  # swapped edges tolerated
    assert (z["lo"], z["hi"]) == (1.0, 2.0)
    [a] = L.normalize_levels([{"price": 5}])
    assert a["name"] == "level_1"


# --- parse_since ---------------------------------------------------------------------

def test_parse_since_iso_forms_and_flooring():
    assert L.parse_since("2026-09-19T13:07:00Z") == pd.Timestamp("2026-09-19T13:07:00Z")
    assert L.parse_since("2026-09-19 13:07") == pd.Timestamp("2026-09-19T13:07:00Z")  # naive = UTC
    assert L.parse_since("2026-09-19T13:07:00Z", 15) == pd.Timestamp("2026-09-19T13:00:00Z")
    assert L.parse_since("2026-09-19T15:07:00+02:00", 60) == pd.Timestamp("2026-09-19T13:00:00Z")
    with pytest.raises(ToolError, match="Cannot parse"):
        L.parse_since("yesterday-ish")
    with pytest.raises(ToolError, match="since must be"):
        L.parse_since("")


def test_parse_since_session_with_date():
    ts = L.parse_since("session:New York@2026-09-18", 15)
    assert ts == pd.Timestamp("2026-09-18T13:00:00Z")
    ts = L.parse_since("session:london open kill zone@2026-09-18")  # case-insensitive
    assert ts == pd.Timestamp("2026-09-18T06:00:00Z")
    with pytest.raises(ToolError, match="Unknown session"):
        L.parse_since("session:Mars@2026-09-18")
    with pytest.raises(ToolError, match="Bad date"):
        L.parse_since("session:London@18.09.2026")


def test_parse_since_session_most_recent_start():
    now = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    assert L.parse_since("session:London", now=now) == pd.Timestamp("2026-09-19T07:00:00Z")
    # New York (13:00) has not started yet at 10:00 -> yesterday's start
    assert L.parse_since("session:New York", now=now) == pd.Timestamp("2026-09-18T13:00:00Z")


def test_session_table_is_fixed_utc_and_complete():
    assert set(SESSION_WINDOWS_UTC) == {
        "Sydney", "Tokyo", "London", "New York", "Asian kill zone",
        "London open kill zone", "New York kill zone", "london close kill zone"}
    assert SESSION_WINDOWS_UTC["London"] == ("07:00", "16:00")
    assert SESSION_WINDOWS_UTC["New York"] == ("13:00", "22:00")


# --- tv_scan_check_levels (loader-injected) --------------------------------------------

def _settings(tmp_path) -> Settings:
    return Settings(
        toolsets=frozenset({"scan"}), extra_tools=frozenset(), read_only=False,
        cache_dir=tmp_path, chart_dir=tmp_path / "c", journal_dir=tmp_path / "j",
        strategy_dir=tmp_path / "s", max_bars=5000, oanda_api_key=None,
        oanda_env="practice", session_id=None,
    )


def _build(tmp_path, df):
    seen = {}

    def load(symbol, timeframe, count, provider):
        seen.update(symbol=symbol, timeframe=timeframe, count=count, provider=provider)
        return resolve(symbol), resolve_timeframe(timeframe), df

    mcp = FastMCP(name="test")
    scan.register(mcp, _settings(tmp_path), loader=load)
    return mcp, seen


def test_scan_check_levels_tool(tmp_path):
    df = _df([(100, 101, 99, 100.5), (100.5, 102, 100, 101.5), (101.5, 104, 101, 103.5)])
    mcp, seen = _build(tmp_path, df)
    r = asyncio.run(mcp.call_tool("tv_scan_check_levels", {
        "symbol": "eurusd", "timeframe": "M15",
        "levels": [{"name": "IBH", "price": 103.0}, {"name": "far", "price": 90}],
        "since": "2026-09-18T13:05:00Z", "count": 300,
    }))
    data = json.loads(r.content[0].text)
    assert seen == {"symbol": "eurusd", "timeframe": "M15", "count": 300, "provider": "auto"}
    assert data["provider"] == "dukascopy" and data["detector"] == "check_levels"
    assert data["symbol"] == "EURUSD" and data["timeframe"] == "M15"
    assert data["since"] == "2026-09-18T13:00:00Z"  # floored to M15
    assert data["bars_scanned"] == 3
    ibh, far = data["levels"]
    assert ibh["tagged"] is True and ibh["first_tag"]["from"] == "below"
    assert far["tagged"] is False and far["closest_approach"]["side"] == "above"
    assert "stale" in ibh["coverage_warning"]  # 2026-09-18 bars vs real now


def test_scan_check_levels_bad_since_is_tool_error(tmp_path):
    mcp, _ = _build(tmp_path, _df([(1, 2, 0, 1)]))
    with pytest.raises(ToolError, match="Unknown session"):
        asyncio.run(mcp.call_tool("tv_scan_check_levels", {
            "symbol": "EURUSD", "timeframe": "M15",
            "levels": [{"name": "x", "price": 1}], "since": "session:Nowhere"}))
