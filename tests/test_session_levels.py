"""M9: `scan/session_levels.py` (pure pandas) and the `tv_scan_levels` tool.

Every expectation below is hand-checked against synthetic bars built in this
file: flat 100.00 candles with a handful of explicit overrides, so each high,
low, range, average and extension can be read off by eye. No network.

Covered: previous day high/low/close, previous week / month high-low, session
high/low/open/close, ADR over a known series (including the dropped oldest
period), the opening range with its size, its share of ADR and its extensions,
gap size between consecutive session occurrences and between calendar days,
midnight-open anchors, the empty-session warning, DST-correct explicit windows
vs the fixed-UTC name table, `include` filtering, and the tool naming its feed.
"""

import asyncio
import json

import pandas as pd
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import scan
from tvmcp.config import Settings
from tvmcp.scan import session_levels as SL
from tvmcp.symbols import resolve, resolve_timeframe

BASE = 100.0


# --------------------------------------------------------------------------- #
# synthetic bars
# --------------------------------------------------------------------------- #
def _day(day: str, overrides: dict | None = None, start_min: int = 0,
         end_min: int = 1440, step: int = 15) -> list[dict]:
    """One day of `step`-minute bars, flat at BASE except for `overrides`.

    `overrides` maps "HH:MM" -> (open, high, low, close).
    """
    rows = []
    for m in range(start_min, end_min, step):
        hh, mm = divmod(m, 60)
        key = f"{hh:02d}:{mm:02d}"
        o, h, low, c = (overrides or {}).get(key, (BASE, BASE, BASE, BASE))
        rows.append({"time": pd.Timestamp(f"{day}T{key}:00Z"), "open": o, "high": h,
                     "low": low, "close": c, "volume": 1})
    return rows


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).sort_values("time").reset_index(drop=True)


def _m15_frame() -> pd.DataFrame:
    """Five days of M15 bars with hand-set daily ranges.

    2026-09-14 range 200 (high 300) - the oldest loaded day, dropped from ADR(3)
    2026-09-15 range  10 (high 110)
    2026-09-16 range  20 (high 120)
    2026-09-17 range  30 (high 130), closes at 102
    2026-09-18 the day under test: bars start at 06:00 (00:00-05:45 missing),
               13:30-14:15 carries the opening range (high 105, low 100).
    """
    rows = []
    rows += _day("2026-09-14", {"10:00": (BASE, 300.0, BASE, BASE)})
    rows += _day("2026-09-15", {"10:00": (BASE, 110.0, BASE, BASE)})
    rows += _day("2026-09-16", {"10:00": (BASE, 120.0, BASE, BASE)})
    rows += _day("2026-09-17", {"10:00": (BASE, 130.0, BASE, BASE),
                                "23:45": (BASE, 102.0, BASE, 102.0)})
    rows += _day("2026-09-18", {
        "13:30": (101.0, 105.0, 101.0, 104.0),
        "13:45": (104.0, 104.0, 100.0, 101.0),
        "14:00": (102.0, 102.0, 102.0, 102.0),
        "14:15": (102.0, 102.0, 102.0, 102.0),
    }, start_min=360)
    return _frame(rows)


RTH = {"name": "RTH", "start": "13:30", "end": "20:00", "tz": "UTC"}
DAY = "2026-09-18"


def _levels(out: dict) -> dict:
    return {lv["name"]: lv for lv in out["levels"]}


# --------------------------------------------------------------------------- #
# previous calendar periods
# --------------------------------------------------------------------------- #
def test_previous_day_high_low_close():
    out = SL.compute_levels(_m15_frame(), day=DAY, include=["prev_day"], timeframe_minutes=15)
    lv = _levels(out)
    assert lv["Previous day high"]["price"] == 130.0
    assert lv["Previous day low"]["price"] == 100.0
    assert lv["Previous day close"]["price"] == 102.0
    # every level says how it was derived
    assert "previous day 2026-09-17" in lv["Previous day high"]["source"]
    assert lv["Previous day high"]["kind"] == "price"


def test_previous_week_and_month():
    # one bar a day, 2026-08-25 .. 2026-09-18; high = 100 + day-of-month,
    # low = 50 + day-of-month. Week of 2026-09-18 is ISO W38, so the previous
    # week is W37 = Sep 7..13 and the previous month is August (25th..31st here).
    rows = []
    for ts in pd.date_range("2026-08-25", "2026-09-18", freq="D"):
        rows.append({"time": pd.Timestamp(f"{ts.date()}T12:00:00Z"),
                     "open": BASE, "high": 100.0 + ts.day, "low": 50.0 + ts.day,
                     "close": 75.0 + ts.day, "volume": 1})
    out = SL.compute_levels(_frame(rows), day=DAY, include=["prev_week", "prev_month"],
                            timeframe_minutes=1440)
    lv = _levels(out)
    assert lv["Previous week high"]["price"] == 113.0  # Sep 13
    assert lv["Previous week low"]["price"] == 57.0    # Sep 7
    assert lv["Previous month high"]["price"] == 131.0  # Aug 31
    assert lv["Previous month low"]["price"] == 75.0    # Aug 25


def test_missing_previous_period_warns_instead_of_inventing():
    out = SL.compute_levels(_m15_frame(), day=DAY, include=["prev_week", "prev_month"],
                            timeframe_minutes=15)
    assert out["levels"] == []
    assert any("previous week" in w for w in out["warnings"])
    assert any("previous month" in w for w in out["warnings"])


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def test_session_high_low_open_close():
    out = SL.compute_levels(_m15_frame(), day=DAY, sessions=[RTH],
                            include=["sessions"], timeframe_minutes=15)
    lv = _levels(out)
    assert lv["RTH high"]["price"] == 105.0   # the 13:30 bar
    assert lv["RTH low"]["price"] == 100.0
    assert lv["RTH open"]["price"] == 101.0   # open of the 13:30 bar
    assert lv["RTH close"]["price"] == 100.0  # close of the 19:45 bar
    [block] = out["sessions"]
    assert block["window_utc"] == ["2026-09-18T13:30:00Z", "2026-09-18T20:00:00Z"]
    assert block["bars"] == 26 and block["complete"] is True


def test_empty_session_warns_and_emits_no_levels():
    # the test day starts at 06:00, so the 00:00-04:00 window holds no bars
    out = SL.compute_levels(_m15_frame(), day=DAY, sessions=["Asian kill zone"],
                            include=["sessions"], timeframe_minutes=15)
    assert out["levels"] == []
    [block] = out["sessions"]
    assert block["bars"] == 0 and block["high"] is None and block["complete"] is False
    assert any("has no bars" in w and "Asian kill zone" in w for w in out["warnings"])


def test_named_sessions_come_from_the_fixed_utc_table():
    [spec] = SL.normalize_sessions(["london"])  # case-insensitive
    assert spec["name"] == "London" and spec["start"] == "07:00" and spec["tz"] == "UTC"
    assert "not DST-aware" in spec["source"]
    with pytest.raises(ToolError) as ei:
        SL.normalize_sessions(["Frankfurt"])
    assert "Unknown session" in str(ei.value)


def test_explicit_window_is_dst_correct_and_wraps_midnight():
    ny = {"name": "RTH", "start": "09:30", "end": "16:00", "tz": "America/New_York"}
    summer = SL.session_bounds(ny, pd.Timestamp("2026-07-15").date())
    winter = SL.session_bounds(ny, pd.Timestamp("2026-01-15").date())
    assert summer[0].isoformat() == "2026-07-15T13:30:00+00:00"  # EDT = UTC-4
    assert winter[0].isoformat() == "2026-01-15T14:30:00+00:00"  # EST = UTC-5
    # a window whose end is at or before its start runs into the next day
    syd = SL.normalize_sessions(["Sydney"])[0]
    start, end = SL.session_bounds(syd, pd.Timestamp(DAY).date())
    assert start.isoformat() == "2026-09-18T21:00:00+00:00"
    assert end.isoformat() == "2026-09-19T06:00:00+00:00"


def test_day_offset_moves_the_session_back():
    spec = SL.normalize_sessions([{**RTH, "name": "yesterday RTH", "day_offset": -1}])[0]
    start, _ = SL.session_bounds(spec, pd.Timestamp(DAY).date())
    assert start.isoformat() == "2026-09-17T13:30:00+00:00"


# --------------------------------------------------------------------------- #
# ADR
# --------------------------------------------------------------------------- #
def test_adr_over_a_known_series_drops_the_oldest_loaded_day():
    # ranges before 2026-09-18 are 200 / 10 / 20 / 30; with n=3 the oldest
    # loaded day (200, truncated by the load window) is dropped -> mean 20
    out = SL.compute_levels(_m15_frame(), day=DAY, include=["adr"], adr_days=3,
                            adr_anchor="none", timeframe_minutes=15)
    adr = out["adr"]
    assert adr["value"] == 20.0
    assert adr["n_used"] == 3
    assert [p["label"] for p in adr["periods"]] == ["2026-09-15", "2026-09-16", "2026-09-17"]
    assert [p["range"] for p in adr["periods"]] == [10.0, 20.0, 30.0]
    assert adr["basis"] == "calendar day (UTC)"


def test_adr_keeps_the_oldest_day_when_it_is_needed():
    out = SL.compute_levels(_m15_frame(), day=DAY, include=["adr"], adr_days=4,
                            adr_anchor="none", timeframe_minutes=15)
    assert out["adr"]["n_used"] == 4
    assert out["adr"]["value"] == pytest.approx((200 + 10 + 20 + 30) / 4)


def test_adr_projections_from_the_session_open():
    out = SL.compute_levels(_m15_frame(), day=DAY, sessions=[RTH], include=["adr", "sessions"],
                            adr_days=3, adr_anchor="session_open", adr_multiples=[0.5, 1],
                            timeframe_minutes=15)
    adr = out["adr"]
    assert adr["anchor"]["price"] == 101.0  # RTH open
    assert adr["projections"] == {"up_0.5": 111.0, "down_0.5": 91.0,
                                  "up_1": 121.0, "down_1": 81.0}
    lv = _levels(out)
    assert lv["ADR +1"]["price"] == 121.0 and lv["ADR -1"]["price"] == 81.0
    assert "ADR(3) 20.0 x1 from RTH open 101.0" in lv["ADR +1"]["source"]


def test_adr_over_a_named_session_instead_of_the_calendar_day():
    out = SL.compute_levels(_m15_frame(), day=DAY, sessions=[RTH], include=["adr"],
                            adr_days=2, adr_session="RTH", adr_anchor="none",
                            timeframe_minutes=15)
    adr = out["adr"]
    # every prior RTH window is flat at 100 -> range 0
    assert adr["basis"].startswith("session:RTH")
    assert adr["n_used"] == 2 and adr["value"] == 0.0
    assert [p["label"] for p in adr["periods"]] == ["2026-09-16", "2026-09-17"]


def test_adr_short_history_warns():
    out = SL.compute_levels(_m15_frame(), day=DAY, include=["adr"], adr_days=30,
                            adr_anchor="none", timeframe_minutes=15)
    assert out["adr"]["n_used"] == 4
    assert any("only 4 of 30" in w for w in out["warnings"])


# --------------------------------------------------------------------------- #
# opening range / initial balance
# --------------------------------------------------------------------------- #
def test_opening_range_size_share_of_adr_and_extensions():
    out = SL.compute_levels(_m15_frame(), day=DAY, sessions=[RTH],
                            include=["adr", "opening_range"], adr_days=3,
                            opening_range_minutes=60, adr_anchor="none",
                            timeframe_minutes=15)
    orb = out["opening_range"]
    assert orb["window_utc"] == ["2026-09-18T13:30:00Z", "2026-09-18T14:30:00Z"]
    assert orb["bars"] == 4 and orb["complete"] is True
    assert (orb["high"], orb["low"], orb["size"], orb["mid"]) == (105.0, 100.0, 5.0, 102.5)
    assert (orb["open"], orb["close"]) == (101.0, 102.0)
    assert orb["size_vs_adr"] == 0.25  # 5 / ADR(3)=20
    assert orb["extensions"] == {
        "up_0.5": 107.5, "down_0.5": 97.5,
        "up_1": 110.0, "down_1": 95.0,
        "up_1.5": 112.5, "down_1.5": 92.5,
        "up_2": 115.0, "down_2": 90.0,
    }
    lv = _levels(out)
    assert lv["RTH OR high"]["price"] == 105.0 and lv["RTH OR mid"]["price"] == 102.5
    assert lv["RTH OR up_2"]["price"] == 115.0
    assert lv["RTH opening range"]["kind"] == "zone"
    assert (lv["RTH opening range"]["high"], lv["RTH opening range"]["low"]) == (105.0, 100.0)


def test_opening_range_custom_extension_multiples():
    out = SL.compute_levels(_m15_frame(), day=DAY, sessions=[RTH],
                            include=["opening_range"], extensions=[1, 3],
                            timeframe_minutes=15)
    assert out["opening_range"]["extensions"] == {
        "up_1": 110.0, "down_1": 95.0, "up_3": 120.0, "down_3": 85.0,
    }


def test_incomplete_opening_range_is_flagged():
    rows = _day("2026-09-18", {"13:30": (101.0, 105.0, 101.0, 104.0)},
                start_min=360, end_min=13 * 60 + 45)  # last bar opens 13:30
    out = SL.compute_levels(_frame(rows), day=DAY, sessions=[RTH],
                            include=["opening_range"], timeframe_minutes=15)
    assert out["opening_range"]["complete"] is False
    assert out["opening_range"]["bars"] == 1
    assert any("not closed yet" in w for w in out["warnings"])
    assert _levels(out)["RTH OR high"]["note"] == "opening range still forming"


# --------------------------------------------------------------------------- #
# gaps
# --------------------------------------------------------------------------- #
def test_gap_between_consecutive_sessions_and_calendar_days():
    out = SL.compute_levels(_m15_frame(), day=DAY, sessions=[RTH], include=["gaps"],
                            timeframe_minutes=15)
    gaps = {g["name"]: g for g in out["gaps"]}
    rth = gaps["RTH open gap"]
    assert rth["from"]["price"] == 100.0 and rth["from"]["label"] == "2026-09-17"
    assert rth["to"]["price"] == 101.0
    assert rth["size"] == 1.0 and rth["direction"] == "up"
    day_gap = gaps["day open gap"]
    # 2026-09-17 closed at 102, 2026-09-18 opened at 100
    assert day_gap["size"] == -2.0 and day_gap["direction"] == "down"
    lv = _levels(out)
    assert lv["RTH previous close"]["price"] == 100.0
    assert lv["RTH open gap"]["kind"] == "zone"
    assert (lv["RTH open gap"]["high"], lv["RTH open gap"]["low"]) == (101.0, 100.0)


# --------------------------------------------------------------------------- #
# anchors
# --------------------------------------------------------------------------- #
def test_midnight_open_anchor_takes_the_open_of_the_first_bar_at_or_after():
    out = SL.compute_levels(
        _m15_frame(), day=DAY, include=["anchors"],
        anchors=[{"name": "NY midnight open", "time": "00:00", "tz": "America/New_York"}],
        timeframe_minutes=15,
    )
    lv = _levels(out)
    anchor = lv["NY midnight open"]
    # 2026-09-18 00:00 New York = 04:00 UTC; bars start at 06:00 that day
    assert anchor["time"] == "2026-09-18T06:00:00Z"
    assert anchor["price"] == 100.0
    assert "120m after the anchor" in anchor["note"]


def test_anchor_beyond_the_history_warns():
    out = SL.compute_levels(
        _m15_frame(), day=DAY, include=["anchors"],
        anchors=[{"name": "tomorrow", "time": "12:00", "tz": "UTC", "day_offset": 3}],
        timeframe_minutes=15,
    )
    assert out["levels"] == []
    assert any("no bar at or after it" in w for w in out["warnings"])


# --------------------------------------------------------------------------- #
# argument handling
# --------------------------------------------------------------------------- #
def test_include_filters_the_blocks():
    out = SL.compute_levels(_m15_frame(), day=DAY, sessions=[RTH], include=["prev_day"],
                            timeframe_minutes=15)
    assert out["include"] == ["prev_day"]
    assert out["adr"] is None and out["opening_range"] is None and out["gaps"] == []
    assert {lv["name"] for lv in out["levels"]} == {
        "Previous day high", "Previous day low", "Previous day close"}


def test_date_defaults_to_the_last_bars_day():
    out = SL.compute_levels(_m15_frame(), include=["prev_day"], timeframe_minutes=15)
    assert out["date"] == DAY


def test_bad_arguments_raise_toolerror():
    df = _m15_frame()
    for kwargs, needle in (
        ({"day": "18/09/2026"}, "Bad date"),
        ({"tz": "Mars/Olympus"}, "Unknown timezone"),
        ({"include": ["nope"]}, "Unknown include"),
        ({"sessions": [{"name": "x", "start": "25:00", "end": "16:00"}]}, "HH:MM"),
        ({"sessions": [{"name": "x", "end": "16:00"}]}, "needs start and end"),
        ({"sessions": [RTH], "adr_session": "Tokyo"}, "not one of the requested sessions"),
        ({"sessions": [RTH], "adr_anchor": "vibes"}, "Unknown adr_anchor"),
        ({"extensions": [0]}, "must be > 0"),
        ({"adr_days": 0}, "adr_days"),
    ):
        with pytest.raises(ToolError) as ei:
            SL.compute_levels(df, timeframe_minutes=15, **kwargs)
        assert needle in str(ei.value), kwargs


def test_history_starting_after_the_day_warns():
    rows = _day("2026-09-18", start_min=360)
    out = SL.compute_levels(_frame(rows), day="2026-09-14", include=["prev_day"],
                            timeframe_minutes=15)
    assert any("history starts at" in w for w in out["warnings"])


# --------------------------------------------------------------------------- #
# the MCP tool
# --------------------------------------------------------------------------- #
def _settings(tmp_path) -> Settings:
    return Settings(
        toolsets=frozenset({"scan"}), extra_tools=frozenset(), read_only=True,
        cache_dir=tmp_path, chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal", strategy_dir=tmp_path / "strategies",
        max_bars=5000, oanda_api_key=None, oanda_env="practice", session_id=None,
    )


def _build(tmp_path, df):
    mcp = FastMCP(name="test")

    def load(symbol, timeframe, count, provider):
        return resolve(symbol), resolve_timeframe(timeframe), df.tail(count).reset_index(drop=True)

    scan.register(mcp, _settings(tmp_path), loader=load)
    return mcp


def _data(mcp, name, args):
    r = asyncio.run(mcp.call_tool(name, args))
    return json.loads(r.content[0].text)


def test_tool_names_its_feed_and_returns_the_blocks(tmp_path):
    data = _data(_build(tmp_path, _m15_frame()), "tv_scan_levels", {
        "symbol": "EURUSD", "timeframe": "M15", "date": DAY,
        "sessions": [RTH], "adr_days": 3, "opening_range_minutes": 60,
    })
    assert data["symbol"] == "EURUSD"
    assert data["provider"] == "dukascopy"  # no OANDA key -> auto picks dukascopy
    assert data["detector"] == "levels"
    assert data["timeframe"] == "M15" and data["timeframe_minutes"] == 15
    assert data["date"] == DAY
    assert data["adr"]["value"] == 20.0
    assert data["opening_range"]["size"] == 5.0
    names = {lv["name"] for lv in data["levels"]}
    assert {"Previous day high", "RTH high", "RTH OR high", "ADR +1"} <= names
    assert all({"name", "kind", "source", "time", "note"} <= set(lv) for lv in data["levels"])


def test_tool_registers_under_read_only(tmp_path):
    names = {t.name for t in asyncio.run(_build(tmp_path, _m15_frame()).list_tools())}
    assert "tv_scan_levels" in names


def test_tool_rejects_an_unknown_session_name(tmp_path):
    with pytest.raises(ToolError) as ei:
        _data(_build(tmp_path, _m15_frame()), "tv_scan_levels",
              {"symbol": "EURUSD", "sessions": ["Frankfurt"]})
    assert "Unknown session" in str(ei.value)
