"""tv_risk_guard: hand-built journals with known answers, one per check type.

Every threshold lives in the test's own rules dict - the module under test must
hold none of its own. Dates: 2026-09-14 is a Monday, so 09-18 is a Friday and
09-21 the next Monday (the session-vs-calendar-day cases hang off that).
"""

import asyncio
import json

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import journal
from tvmcp.config import Settings
from tvmcp.journal import risk, schema

ACCOUNT = {"balance": 10_000.0, "starting_balance": 10_000.0, "currency": "USD"}


def rec(date, r=None, pnl=None, **kw):
    return schema.make_record(source="test", date=date, r_multiple=r, pnl=pnl, **kw)


def rules(**checks) -> dict:
    """Only the named checks are enabled; 1R = 1 % of the account."""
    return {
        "percent_base": "starting_balance",
        "risk_per_r": {"pct_of_account": 1.0},
        "session": {"trading_days": [0, 1, 2, 3, 4], "week_start": "monday"},
        "checks": {name: {"enabled": True, **cfg} for name, cfg in checks.items()},
    }


def check(result, id_):
    return next(c for c in result["checks"] if c["id"] == id_)


# ---------------------------------------------------------------- loss limits

def test_daily_loss_breaches_exactly_at_the_limit():
    r = risk.evaluate([rec("2026-09-16", r=-2.0)], rules(daily_loss={"limit": {"pct": 2.0}}),
                      today="2026-09-16", account=ACCOUNT)
    assert r["status"] == "stop"
    assert check(r, "daily_loss")["status"] == "stop"
    assert check(r, "daily_loss")["numbers"]["total"] == -2.0


def test_daily_loss_one_tick_under_the_limit_is_ok():
    r = risk.evaluate([rec("2026-09-16", r=-1.99)], rules(daily_loss={"limit": {"pct": 2.0}}),
                      today="2026-09-16", account=ACCOUNT)
    assert r["status"] == "ok"
    assert check(r, "daily_loss")["numbers"]["remaining"] == pytest.approx(0.01)


def test_daily_loss_limit_in_currency_over_a_money_journal():
    recs = [rec("2026-09-16", pnl=-120.0, pnl_currency="USD"), rec("2026-09-16", pnl=-80.0, pnl_currency="USD")]
    r = risk.evaluate(recs, rules(daily_loss={"limit": {"currency": 200.0}}), today="2026-09-16", account=ACCOUNT)
    assert check(r, "daily_loss")["status"] == "stop"
    assert check(r, "daily_loss")["numbers"]["unit"] == "currency"


def test_daily_loss_limit_in_r_over_an_r_only_journal():
    r = risk.evaluate([rec("2026-09-16", r=-2.5)], rules(daily_loss={"limit": {"r": 2.0}}),
                      today="2026-09-16", account=None)
    assert check(r, "daily_loss")["status"] == "stop"
    assert r["status"] == "stop"  # no account needed for an R limit over an R journal


def test_r_limit_over_a_currency_only_journal_converts_through_risk_per_r():
    r = risk.evaluate([rec("2026-09-16", pnl=-250.0, pnl_currency="USD")],
                      rules(daily_loss={"limit": {"r": 2.0}}), today="2026-09-16", account=ACCOUNT)
    c = check(r, "daily_loss")
    assert c["status"] == "stop"
    assert c["numbers"]["basis"] == "journal money / risk_per_r"
    assert c["numbers"]["total"] == pytest.approx(-2.5)


def test_pct_limit_without_an_account_is_unknown_not_ok():
    r = risk.evaluate([rec("2026-09-16", r=-5.0)], rules(daily_loss={"limit": {"pct": 2.0}}),
                      today="2026-09-16", account=None)
    assert check(r, "daily_loss")["status"] == "unknown"
    assert r["status"] == "flag"  # an unevaluated stop-rule is visible, never comfortable
    assert any("daily_loss" in w for w in r["warnings"])


def test_a_malformed_limit_is_unknown():
    r = risk.evaluate([rec("2026-09-16", r=-5.0)], rules(daily_loss={"limit": 2.0}),
                      today="2026-09-16", account=ACCOUNT)
    assert check(r, "daily_loss")["status"] == "unknown"


# ---------------------------------------------------------------- week windows

WEEK_RECORDS = [rec("2026-09-11", r=-2.0), rec("2026-09-16", r=-1.5)]  # Fri last week, Wed this week


def test_calendar_week_excludes_last_friday():
    r = risk.evaluate(WEEK_RECORDS, rules(weekly_loss={"limit": {"pct": 3.0}, "window": "calendar"}),
                      today="2026-09-16", account=ACCOUNT)
    c = check(r, "weekly_loss")
    assert c["status"] == "ok"
    assert c["numbers"]["window"] == ["2026-09-14", "2026-09-16"]
    assert c["numbers"]["total"] == pytest.approx(-1.5)


def test_rolling_week_includes_last_friday_and_breaches():
    r = risk.evaluate(WEEK_RECORDS,
                      rules(weekly_loss={"limit": {"pct": 3.0}, "window": "rolling", "rolling_days": 7}),
                      today="2026-09-16", account=ACCOUNT)
    c = check(r, "weekly_loss")
    assert c["status"] == "stop"
    assert c["numbers"]["window"] == ["2026-09-10", "2026-09-16"]
    assert c["numbers"]["total"] == pytest.approx(-3.5)


def test_monthly_loss_calendar_window():
    recs = [rec("2026-08-31", r=-4.0), rec("2026-09-02", r=-3.0), rec("2026-09-16", r=-3.5)]
    r = risk.evaluate(recs, rules(monthly_loss={"limit": {"pct": 6.0}}), today="2026-09-16", account=ACCOUNT)
    c = check(r, "monthly_loss")
    assert c["status"] == "stop"
    assert c["numbers"]["total"] == pytest.approx(-6.5)  # August is not in the window


# ---------------------------------------------------------------- counts

def test_max_losses_per_day_breaches_on_the_second_loss():
    one = risk.evaluate([rec("2026-09-16", r=-1.0)], rules(max_losses_per_day={"max": 1}), today="2026-09-16")
    two = risk.evaluate([rec("2026-09-16", r=-1.0), rec("2026-09-16", r=-1.0)],
                        rules(max_losses_per_day={"max": 1}), today="2026-09-16")
    assert check(one, "max_losses_per_day")["status"] == "ok"
    assert check(two, "max_losses_per_day")["status"] == "stop"


def test_max_trades_per_day_counts_winners_too():
    recs = [rec("2026-09-16", r=1.0), rec("2026-09-16", r=2.0), rec("2026-09-16", r=-1.0)]
    r = risk.evaluate(recs, rules(max_trades_per_day={"max": 2, "status": "flag"}), today="2026-09-16")
    assert check(r, "max_trades_per_day")["status"] == "flag"
    assert r["status"] == "flag"


def test_max_trades_per_week_is_configurable_as_a_flag():
    recs = [rec("2026-09-14", r=1.0), rec("2026-09-15", r=-1.0), rec("2026-09-16", r=1.0)]
    r = risk.evaluate(recs, rules(max_trades_per_week={"max": 2, "status": "flag"}), today="2026-09-16")
    assert check(r, "max_trades_per_week")["numbers"] == {
        "count": 3, "max": 2, "window": ["2026-09-14", "2026-09-16"]}


# ---------------------------------------------------------------- cooldown

COOLDOWN = rules(consecutive_losses={"losses": 2, "cooldown_sessions": 1})
TWO_LOSSES = [rec("2026-09-17", r=-1.0), rec("2026-09-18", r=-1.0)]  # Thu + Fri


def test_cooldown_counts_sessions_not_calendar_days():
    monday = risk.evaluate(TWO_LOSSES, COOLDOWN, today="2026-09-21")  # 3 calendar days, 1 session
    c = check(monday, "consecutive_losses")
    assert c["status"] == "stop"
    assert c["numbers"]["sessions_since_last_loss"] == 1
    assert c["numbers"]["streak"] == 2


def test_cooldown_clears_after_the_configured_sessions():
    tuesday = risk.evaluate(TWO_LOSSES, COOLDOWN, today="2026-09-22")
    assert check(tuesday, "consecutive_losses")["status"] == "ok"
    assert check(tuesday, "consecutive_losses")["numbers"]["sessions_since_last_loss"] == 2


def test_cooldown_needs_the_full_streak():
    mixed = [rec("2026-09-17", r=-1.0), rec("2026-09-18", r=0.5)]
    r = risk.evaluate(mixed, COOLDOWN, today="2026-09-21")
    assert check(r, "consecutive_losses")["status"] == "ok"
    assert check(r, "consecutive_losses")["numbers"]["streak"] == 0


def test_cooldown_ends_on_a_named_reset():
    cfg = rules(consecutive_losses={"losses": 2, "cooldown_sessions": 5, "reset_tag": "reviewed"})
    recs = TWO_LOSSES[:1] + [rec("2026-09-18", r=-1.0, tags=["reviewed"])]
    r = risk.evaluate(recs, cfg, today="2026-09-21")
    assert check(r, "consecutive_losses")["status"] == "ok"
    assert check(r, "consecutive_losses")["numbers"]["reset_seen"] is True


def test_cooldown_over_a_journal_session_calendar():
    cfg = rules(consecutive_losses={"losses": 2, "cooldown_sessions": 1})
    cfg["session"]["session_calendar"] = "journal"
    # only dates present in the journal count as sessions: nothing traded since Friday
    r = risk.evaluate(TWO_LOSSES, cfg, today="2026-09-21")
    assert check(r, "consecutive_losses")["numbers"]["sessions_since_last_loss"] == 0
    assert check(r, "consecutive_losses")["status"] == "stop"


# ---------------------------------------------------------------- the floor

FLOOR_RULES = rules(floor_buffer={"buffer": {"currency": 1000.0}})
WINNER = [rec("2026-09-15", pnl=1000.0, pnl_currency="USD")]


def test_static_floor_leaves_room_after_a_win():
    account = {"starting_balance": 10_000.0, "max_loss_pct": 5.0, "floor_type": "static", "currency": "USD"}
    r = risk.evaluate(WINNER, FLOOR_RULES, today="2026-09-16", account=account)
    c = check(r, "floor_buffer")
    assert c["status"] == "ok"
    assert c["numbers"]["floor"] == 9_500.0
    assert c["numbers"]["room_to_floor"] == 1_500.0
    assert c["numbers"]["room_to_floor_r"] == 15.0  # 1R = 1 % of 10k


def test_trailing_floor_follows_a_new_high_and_stops():
    account = {"starting_balance": 10_000.0, "max_loss_pct": 5.0, "floor_type": "trailing", "currency": "USD"}
    r = risk.evaluate(WINNER, FLOOR_RULES, today="2026-09-16", account=account)
    c = check(r, "floor_buffer")
    assert r["account"]["high_water_mark"] == 11_000.0
    assert c["numbers"]["floor"] == 10_500.0      # trailed up with the equity high
    assert c["numbers"]["room_to_floor"] == 500.0
    assert c["status"] == "stop"                   # less than the 1000 buffer left


def test_trailing_floor_does_not_fall_back_after_a_drawdown():
    account = {"starting_balance": 10_000.0, "max_loss_pct": 5.0, "floor_type": "trailing", "currency": "USD"}
    recs = WINNER + [rec("2026-09-16", pnl=-400.0, pnl_currency="USD")]
    r = risk.evaluate(recs, FLOOR_RULES, today="2026-09-16", account=account)
    assert r["account"]["high_water_mark"] == 11_000.0
    assert r["account"]["equity"] == 10_600.0
    assert check(r, "floor_buffer")["numbers"]["floor"] == 10_500.0


def test_floor_without_numbers_is_unknown():
    r = risk.evaluate(WINNER, FLOOR_RULES, today="2026-09-16", account={"currency": "USD"})
    assert check(r, "floor_buffer")["status"] == "unknown"


def test_balance_asof_adds_only_later_trades():
    account = {"balance": 10_500.0, "balance_asof": "2026-09-14", "starting_balance": 10_000.0,
               "max_loss_pct": 10.0, "currency": "USD"}
    recs = [rec("2026-09-12", pnl=500.0, pnl_currency="USD"), rec("2026-09-15", pnl=-200.0, pnl_currency="USD")]
    r = risk.evaluate(recs, FLOOR_RULES, today="2026-09-16", account=account)
    assert r["account"]["equity"] == 10_300.0  # 10 500 snapshot minus the trade after it


# ---------------------------------------------------------------- rest / idle

def test_min_rest_in_minutes_blocks_and_clears():
    loss = rec("2026-09-16", r=-1.0, closed_at="2026-09-16T14:00:00")
    cfg = rules(min_rest={"minutes": 60, "status": "stop"})
    early = risk.evaluate([loss], cfg, today="2026-09-16T14:30:00")
    late = risk.evaluate([loss], cfg, today="2026-09-16T15:30:00")
    assert check(early, "min_rest")["status"] == "stop"
    assert check(early, "min_rest")["numbers"]["minutes_elapsed"] == 30.0
    assert check(late, "min_rest")["status"] == "ok"


def test_min_rest_in_sessions():
    cfg = rules(min_rest={"sessions": 1, "status": "stop"})
    same = risk.evaluate([rec("2026-09-18", r=-1.0)], cfg, today="2026-09-18")
    next_session = risk.evaluate([rec("2026-09-18", r=-1.0)], cfg, today="2026-09-21")
    assert check(same, "min_rest")["status"] == "stop"
    assert check(next_session, "min_rest")["status"] == "ok"


def test_idle_flags_but_never_stops():
    cfg = rules(idle={"sessions": 5, "status": "stop"})  # a configured stop is downgraded on purpose
    r = risk.evaluate([rec("2026-09-07", r=1.0)], cfg, today="2026-09-21")
    assert check(r, "idle")["status"] == "flag"
    assert r["status"] == "flag"
    assert any("downgraded" in w for w in r["warnings"])


def test_empty_journal_is_ok_not_idle():
    r = risk.evaluate([], rules(idle={"sessions": 5}, daily_loss={"limit": {"pct": 2.0}}),
                      today="2026-09-16", account=ACCOUNT)
    assert r["status"] == "ok"
    assert check(r, "idle")["status"] == "ok"


# ---------------------------------------------------------------- profit side

def test_profit_target_flags_when_the_day_is_made():
    cfg = rules(profit_target_day={"target": {"r": 3.0}})
    r = risk.evaluate([rec("2026-09-16", r=3.0)], cfg, today="2026-09-16")
    assert check(r, "profit_target_day")["status"] == "flag"
    assert r["status"] == "flag"


def test_profit_target_can_be_configured_as_a_stop():
    cfg = rules(profit_target_week={"target": {"pct": 4.0}, "status": "stop"})
    recs = [rec("2026-09-14", r=2.0), rec("2026-09-16", r=2.5)]
    r = risk.evaluate(recs, cfg, today="2026-09-16", account=ACCOUNT)
    assert check(r, "profit_target_week")["status"] == "stop"


# ---------------------------------------------------------------- fail closed

def test_fail_closed_forces_stop_even_when_every_limit_is_fine():
    r = risk.evaluate([rec("2026-09-16", r=0.5)], rules(daily_loss={"limit": {"pct": 2.0}}),
                      today="2026-09-16", account=ACCOUNT, fail_closed=True,
                      problems=[{"row": 4, "reason": "result_r='abc' is not a number"}])
    assert r["status"] == "stop"
    assert r["fail_closed"] is True
    integrity = check(r, "journal_integrity")
    assert integrity["status"] == "stop"
    assert "abc" in integrity["detail"]        # the broken row is named, not swallowed
    assert check(r, "daily_loss")["status"] == "ok"  # the limit really is fine; the journal is not


def test_clean_journal_reports_integrity_ok():
    r = risk.evaluate([rec("2026-09-16", r=0.5)], rules(), today="2026-09-16")
    assert check(r, "journal_integrity")["status"] == "ok"
    assert r["fail_closed"] is False


def test_undated_records_are_reported_not_counted():
    recs = [rec("2026-09-16", r=-1.0), schema.make_record(source="test", r_multiple=-5.0)]
    r = risk.evaluate(recs, rules(daily_loss={"limit": {"r": 2.0}}), today="2026-09-16")
    assert check(r, "daily_loss")["status"] == "ok"
    assert any("no session date" in w for w in r["warnings"])


# ---------------------------------------------------------------- rules I/O

def test_shipped_example_rules_load_and_run():
    rules_cfg, warnings = risk.load_rules()
    assert warnings and "placeholders" in warnings[0]
    r = risk.evaluate([rec("2026-09-16", r=-1.0)], rules_cfg, today="2026-09-16", account=ACCOUNT)
    ids = {c["id"] for c in r["checks"]}
    assert {"daily_loss", "weekly_loss", "monthly_loss", "max_losses_per_day",
            "consecutive_losses", "floor_buffer", "idle"} <= ids


def test_rules_path_is_read(tmp_path):
    p = tmp_path / "my_rules.json"
    p.write_text(json.dumps(rules(daily_loss={"limit": {"r": 1.0}})), encoding="utf-8")
    cfg, warnings = risk.load_rules(rules_path=str(p))
    assert warnings == []
    r = risk.evaluate([rec("2026-09-16", r=-1.0)], cfg, today="2026-09-16")
    assert r["status"] == "stop"


def test_disabled_checks_do_not_appear():
    cfg = rules(daily_loss={"limit": {"pct": 2.0}})
    cfg["checks"]["weekly_loss"] = {"enabled": False, "limit": {"pct": 1.0}}
    r = risk.evaluate([rec("2026-09-16", r=-5.0)], cfg, today="2026-09-16", account=ACCOUNT)
    assert "weekly_loss" not in {c["id"] for c in r["checks"]}


def test_rules_and_rules_path_together_is_refused(tmp_path):
    with pytest.raises(ValueError):
        risk.load_rules({"checks": {}}, str(tmp_path / "x.json"))


# ---------------------------------------------------------------- the tool

def _settings(tmp_path, read_only=False) -> Settings:
    return Settings(
        toolsets=frozenset({"journal"}),
        extra_tools=frozenset(),
        read_only=read_only,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=None,
        oanda_env="practice",
        session_id=None,
    )


def _build(tmp_path, read_only=False):
    mcp = FastMCP(name="test")
    journal.register(mcp, _settings(tmp_path, read_only))
    return mcp


def _data(mcp, name, args):
    return json.loads(asyncio.run(mcp.call_tool(name, args)).content[0].text)


def _error(mcp, name, args):
    with pytest.raises(ToolError) as ei:
        asyncio.run(mcp.call_tool(name, args))
    return str(ei.value)


def test_tool_runs_over_inline_records(tmp_path):
    data = _data(_build(tmp_path), "tv_risk_guard", {
        "records": [{"date": "2026-09-16", "r_multiple": -2.0}],
        "rules": rules(daily_loss={"limit": {"r": 2.0}}),
        "today": "2026-09-16",
    })
    assert data["status"] == "stop"
    assert data["origin"]["source"] == "inline"


def test_tool_registers_under_read_only(tmp_path):
    names = {t.name for t in asyncio.run(_build(tmp_path, read_only=True).list_tools())}
    assert {"tv_risk_guard", "tv_journal_load"} <= names


def test_tool_requires_exactly_one_input(tmp_path):
    mcp = _build(tmp_path)
    assert "exactly one" in _error(mcp, "tv_risk_guard", {"today": "2026-09-16"})
    assert "exactly one" in _error(mcp, "tv_risk_guard", {
        "today": "2026-09-16", "records": [], "journal": "x.csv"})


def test_tool_requires_today(tmp_path):
    assert "today is required" in _error(_build(tmp_path), "tv_risk_guard", {"records": []})


def test_tool_flags_unusable_inline_records(tmp_path):
    data = _data(_build(tmp_path), "tv_risk_guard", {
        "records": [{"date": "2026-09-16", "r_multiple": "not a number"}],
        "rules": rules(daily_loss={"limit": {"r": 2.0}}),
        "today": "2026-09-16",
    })
    assert data["fail_closed"] is True
    assert data["status"] == "stop"
    assert data["problems"][0]["row"] == 1


def test_tool_reads_a_journal_file_and_propagates_fail_closed(tmp_path):
    jdir = tmp_path / "journal"
    jdir.mkdir(parents=True)
    (jdir / "log.csv").write_text(
        "date,entered,result_r\n"
        "2026-09-15,yes,-1.0\n"
        "2026-09-16,yes,oops\n",
        encoding="utf-8",
    )
    data = _data(_build(tmp_path), "tv_risk_guard", {
        "journal": "log.csv",
        "rules": rules(daily_loss={"limit": {"r": 2.0}}),
        "today": "2026-09-16",
    })
    assert data["fail_closed"] is True
    assert data["status"] == "stop"
    assert data["origin"]["mapping_used"]["r_multiple"] == "result_r"
    assert check(data, "journal_integrity")["status"] == "stop"
