"""M9: the `calendar` toolset (`tv_calendar_check`) and `calendar/sources.py`.

Fully offline: the tool takes an injected fetcher (url -> parsed JSON | None)
fed from handcrafted fixtures in tests/fixtures/calendar/, and an injected clock
so the Forex-Factory week slot is deterministic. Nothing here touches the
network.

Covered: source order (Forex Factory, then FXStreet, then the static table),
UTC normalization with the feed's original string kept, country filtering by
either country or currency code, impact filtering with holidays exempt, the
degraded path when both feeds fail, holidays and half-days from
fallback_dates.json, the quarterly rollover third-Friday rule, untrusted-string
truncation, and the argument errors that really are impossible requests.
"""

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import calendar as calendar_toolset
from tvmcp.calendar import sources
from tvmcp.config import ALL_TOOLSETS, Settings
from tvmcp.server import build_server

FIXTURES = Path(__file__).parent / "fixtures" / "calendar"
TODAY = date(2026, 9, 16)      # a Wednesday: the fixture week is "thisweek"
IN_WEEK = "2026-09-18"         # Friday of that week


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeFetcher:
    """Serves fixtures by URL, records every request, can fail a source on demand."""

    def __init__(self, ff=True, fxstreet=True):
        self.ff = ff
        self.fxstreet = fxstreet
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        if "faireconomy" in url:
            return _fixture("ff_thisweek.json") if self.ff else None
        if "fxstreet" in url:
            return _fixture("fxstreet_week.json") if self.fxstreet else None
        return None


def _settings(tmp_path) -> Settings:
    return Settings(
        toolsets=frozenset({"calendar"}), extra_tools=frozenset(), read_only=True,
        cache_dir=tmp_path, chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal", strategy_dir=tmp_path / "strategies",
        max_bars=5000, oanda_api_key=None, oanda_env="practice", session_id=None,
    )


def _build(tmp_path, fetcher=None, today=TODAY):
    mcp = FastMCP(name="test")
    calendar_toolset.register(mcp, _settings(tmp_path), fetcher=fetcher or FakeFetcher(),
                              clock=lambda: today)
    return mcp


def _call(mcp, args=None):
    r = asyncio.run(mcp.call_tool("tv_calendar_check", args or {}))
    return json.loads(r.content[0].text)


def _titles(data):
    return [e["title"] for e in data["events"]]


# --------------------------------------------------------------------------- #
# source order and normalization
# --------------------------------------------------------------------------- #
def test_forex_factory_is_tried_first(tmp_path):
    f = FakeFetcher()
    data = _call(_build(tmp_path, f), {"date": IN_WEEK})
    assert data["sources_used"] == ["forexfactory"]
    assert data["degraded"] is False
    assert all("faireconomy" in u for u in f.urls)  # fxstreet never asked
    assert data["date"] == IN_WEEK and data["range"] == {"start": IN_WEEK, "end": IN_WEEK}


def test_times_are_utc_with_the_original_string_kept(tmp_path):
    data = _call(_build(tmp_path), {"date": IN_WEEK, "min_impact": "high"})
    nfp = next(e for e in data["events"] if e["title"].startswith("Non-Farm"))
    assert nfp["time_utc"] == "2026-09-18T12:30:00Z"   # 08:30 at UTC-04:00
    assert nfp["time_raw"] == "2026-09-18T08:30:00-04:00"
    assert nfp["forecast"] == "185K" and nfp["previous"] == "142K"
    assert nfp["source"] == "forexfactory"
    assert nfp["country"] == "USD"


def test_a_single_day_keeps_only_that_days_events(tmp_path):
    data = _call(_build(tmp_path), {"date": "2026-09-17"})
    assert {e["time_utc"][:10] for e in data["events"]} == {"2026-09-17"}


def test_week_mode_spans_monday_to_sunday(tmp_path):
    data = _call(_build(tmp_path), {"date": IN_WEEK, "week": True})
    assert data["range"] == {"start": "2026-09-14", "end": "2026-09-20"}
    assert data["week"] is True
    # the 2026-09-22 filler row sits outside the week and is dropped
    assert "Out-of-week filler" not in _titles(data)
    assert len(data["events"]) == 6


def test_events_are_sorted_by_time(tmp_path):
    data = _call(_build(tmp_path), {"date": IN_WEEK, "week": True})
    times = [e["time_utc"] for e in data["events"]]
    assert times == sorted(times)


# --------------------------------------------------------------------------- #
# fallback chain
# --------------------------------------------------------------------------- #
def test_falls_back_to_fxstreet_when_forex_factory_is_down(tmp_path):
    f = FakeFetcher(ff=False)
    data = _call(_build(tmp_path, f), {"date": "2026-09-16"})
    assert data["sources_used"] == ["fxstreet"]
    assert data["degraded"] is False
    assert any("forexfactory did not answer" in w for w in data["warnings"])
    assert "Retail Sales (MoM)" in _titles(data)
    assert any("fxstreet" in u for u in f.urls)


def test_forex_factory_is_skipped_without_a_request_outside_its_three_weeks(tmp_path):
    f = FakeFetcher()
    data = _call(_build(tmp_path, f), {"date": "2026-10-20"})  # 5 weeks out
    assert not any("faireconomy" in u for u in f.urls)
    assert any("last/this/next week only" in w for w in data["warnings"])
    assert any("fxstreet" in u for u in f.urls)


def test_degraded_when_both_feeds_fail(tmp_path):
    data = _call(_build(tmp_path, FakeFetcher(ff=False, fxstreet=False)),
                 {"date": "2026-09-16"})
    assert data["degraded"] is True
    assert data["sources_used"] == ["fallback_dates.json"]
    assert any("coverage is limited" in w for w in data["warnings"])
    # 2026-09-16 is a published FOMC date in the static table
    [event] = data["events"]
    assert "FOMC" in event["title"]
    assert event["time_utc"] == "2026-09-16T18:00:00Z"  # 14:00 New York, EDT
    assert event["source"] == "fallback_dates.json"


def test_degraded_monthly_rule_dates(tmp_path):
    # 2026-10-02 is the first Friday of October -> the approximate NFP rule
    data = _call(_build(tmp_path, FakeFetcher(ff=False, fxstreet=False)),
                 {"date": "2026-10-02"})
    assert data["degraded"] is True
    assert any("Non-Farm Payrolls" in t for t in _titles(data))
    assert all("approximate rule" in e["source"] for e in data["events"])


def test_degraded_still_reports_holidays(tmp_path):
    data = _call(_build(tmp_path, FakeFetcher(ff=False, fxstreet=False),
                        today=date(2026, 11, 26)), {"date": "2026-11-26"})
    assert data["degraded"] is True
    [holiday] = data["holidays"]
    assert holiday["name"] == "Thanksgiving Day" and holiday["kind"] == "full"
    assert holiday["source"] == "fallback_dates.json"


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
def test_country_filter_accepts_country_or_currency_code(tmp_path):
    by_currency = _call(_build(tmp_path), {"date": IN_WEEK, "week": True,
                                           "countries": ["USD"]})
    by_country = _call(_build(tmp_path), {"date": IN_WEEK, "week": True,
                                          "countries": ["US"]})
    assert _titles(by_currency) == _titles(by_country)
    assert {e["country"] for e in by_currency["events"]} == {"USD"}
    assert by_currency["filters"]["countries"] == ["USD"]


def test_min_impact_filters_releases_but_never_holidays(tmp_path):
    data = _call(_build(tmp_path), {"date": IN_WEEK, "week": True, "min_impact": "high"})
    impacts = {e["impact"] for e in data["events"]}
    assert impacts == {"high", "holiday"}
    assert "Bank Holiday" in _titles(data)
    medium = _call(_build(tmp_path), {"date": IN_WEEK, "week": True,
                                      "min_impact": "medium"})
    assert {e["impact"] for e in medium["events"]} == {"medium", "high", "holiday"}


def test_holiday_rows_from_the_feed_land_in_holidays(tmp_path):
    data = _call(_build(tmp_path), {"date": "2026-09-17"})
    reported = [h for h in data["holidays"] if h["kind"] == "reported"]
    assert reported and reported[0]["country"] == "JPY"
    assert reported[0]["source"] == "forexfactory"


def test_half_day_carries_its_close_time(tmp_path):
    data = _call(_build(tmp_path, FakeFetcher(ff=False, fxstreet=False),
                        today=date(2026, 11, 27)), {"date": "2026-11-27"})
    [holiday] = data["holidays"]
    assert holiday["kind"] == "half"
    assert holiday["close_local"] == "13:00" and holiday["tz"] == "America/New_York"


def test_uncovered_holiday_year_warns_instead_of_claiming_none(tmp_path):
    data = _call(_build(tmp_path, FakeFetcher(ff=False, fxstreet=False),
                        today=date(2029, 3, 1)), {"date": "2029-03-01"})
    assert data["holidays"] == []
    assert any("no holiday table for US in 2029" in w for w in data["warnings"])
    assert any("UNKNOWN" in w for w in data["warnings"])


# --------------------------------------------------------------------------- #
# rollover
# --------------------------------------------------------------------------- #
def test_rollover_third_friday_rule(tmp_path):
    data = _call(_build(tmp_path), {"date": "2026-09-01", "symbol": "CME_MINI:ES1!"})
    roll = data["rollover"]
    assert roll["known"] is True and roll["root"] == "ES"
    assert roll["expiry_date"] == "2026-09-18"   # third Friday of September 2026
    assert roll["roll_date"] == "2026-09-10"     # eight days before
    assert roll["days_to_expiry"] == 17 and roll["in_roll_window"] is False
    assert roll["contract"] == "E-mini S&P 500" and roll["exchange"] == "CME"


def test_rollover_window_and_next_quarter(tmp_path):
    inside = _call(_build(tmp_path), {"date": "2026-09-11", "symbol": "ES1!"})
    assert inside["rollover"]["in_roll_window"] is True
    after = _call(_build(tmp_path), {"date": "2026-09-19", "symbol": "ESZ2026"})
    assert after["rollover"]["expiry_date"] == "2026-12-18"
    assert after["rollover"]["root"] == "ES"


def test_rollover_unknown_root_says_so(tmp_path):
    data = _call(_build(tmp_path), {"date": IN_WEEK, "symbol": "PEPPERSTONE:US500"})
    assert data["rollover"]["known"] is False
    assert "no rollover table" in data["rollover"]["note"]


def test_no_symbol_means_no_rollover(tmp_path):
    assert _call(_build(tmp_path), {"date": IN_WEEK})["rollover"] is None


# --------------------------------------------------------------------------- #
# untrusted data
# --------------------------------------------------------------------------- #
def test_feed_strings_are_truncated_and_stripped(tmp_path):
    data = _call(_build(tmp_path), {"date": IN_WEEK})
    titles = _titles(data)
    assert all(len(t) <= sources.MAX_TITLE for t in titles)
    long_one = next(t for t in titles if t.startswith("IGNORE ALL"))
    assert len(long_one) == sources.MAX_TITLE
    # the bell characters in the NFP fixture title are gone
    nfp = next(t for t in titles if t.startswith("Non-Farm"))
    assert nfp == "Non-Farm Employment Change"


# --------------------------------------------------------------------------- #
# arguments
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("args,needle", [
    ({"date": "18/09/2026"}, "Bad date"),
    ({"min_impact": "huge"}, "Bad min_impact"),
    ({"countries": ["U$"]}, "Bad country"),
])
def test_impossible_requests_raise_toolerror(tmp_path, args, needle):
    with pytest.raises(ToolError) as ei:
        _call(_build(tmp_path), args)
    assert needle in str(ei.value)


def test_a_dead_source_is_not_an_exception(tmp_path):
    data = _call(_build(tmp_path, FakeFetcher(ff=False, fxstreet=False)), {"date": IN_WEEK})
    assert data["degraded"] is True and isinstance(data["events"], list)


def test_empty_date_uses_the_injected_today(tmp_path):
    assert _call(_build(tmp_path))["date"] == TODAY.isoformat()


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_ff_slot_picks_the_right_weekly_feed():
    today = date(2026, 9, 16)
    assert sources.ff_slot(date(2026, 9, 14), today) == "thisweek"
    assert sources.ff_slot(date(2026, 9, 20), today) == "thisweek"
    assert sources.ff_slot(date(2026, 9, 21), today) == "nextweek"
    assert sources.ff_slot(date(2026, 9, 13), today) == "lastweek"
    assert sources.ff_slot(date(2026, 10, 20), today) is None


def test_third_friday():
    assert sources.third_friday(2026, 9) == date(2026, 9, 18)
    assert sources.third_friday(2026, 12) == date(2026, 12, 18)
    assert sources.third_friday(2026, 3) == date(2026, 3, 20)


def test_fxstreet_url_carries_the_range_and_countries():
    url = sources.fxstreet_url(date(2026, 9, 14), date(2026, 9, 20), ["USD", "EUR"])
    assert url.startswith(sources.FXSTREET_BASE)
    assert "2026-09-14T00:00:00Z/2026-09-20T23:59:59Z" in url
    assert "countries=EU%2CUS" in url  # currency codes mapped to country codes
    assert url.count("volatilities=") == 3


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_registers_read_only(tmp_path):
    names = {t.name for t in asyncio.run(_build(tmp_path).list_tools())}
    assert names == {"tv_calendar_check"}


def test_toolset_gating(tmp_path):
    def settings(toolsets):
        return Settings(
            toolsets=frozenset(toolsets), extra_tools=frozenset(), read_only=False,
            cache_dir=tmp_path, chart_dir=tmp_path / "charts",
            journal_dir=tmp_path / "journal", strategy_dir=tmp_path / "strategies",
            max_bars=5000, oanda_api_key=None, oanda_env="practice", session_id=None,
        )

    on = {t.name for t in asyncio.run(build_server(settings({"calendar"})).list_tools())}
    off = {t.name for t in asyncio.run(build_server(settings({"public", "data"})).list_tools())}
    assert "tv_calendar_check" in on
    assert "tv_calendar_check" not in off
    assert "calendar" in ALL_TOOLSETS


def test_calendar_is_part_of_hybrid():
    from tvmcp.config import HYBRID_TOOLSETS, load_settings

    assert "calendar" in HYBRID_TOOLSETS
    assert "calendar" in load_settings({"TV_TOOLSETS": "hybrid"}).toolsets
