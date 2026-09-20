"""M9: the `sentinel` toolset - polled session watcher over an injected bar loader.

No network: a synthetic M5 session is built once and served through the loader the
way the scan tests do it, so the four tools run end to end against a fake feed.
Covers the state machine (range forms and closes, break up, confirmation after the
configured closes, retest of the broken boundary, a sweep of a named level, level
tags, clocks), the polling contract (idempotent polls, exact `since_seq` filtering,
`next_poll_after_s`), feed health (stale feed, no data), replay determinism, run
files (atomic, spec re-validated, a second start refuses) and toolset gating.
"""

import asyncio
import json
from datetime import timezone

import pandas as pd
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import sentinel
from tvmcp.config import Settings
from tvmcp.sentinel import machine, store
from tvmcp.sentinel.spec import SentinelSpec, schedule_for
from tvmcp.symbols import resolve, resolve_timeframe

DAY = "2026-09-18"
SESSION_OPEN = pd.Timestamp(f"{DAY}T13:30:00Z")
TF_MIN = 5


def _settings(tmp_path, **kw) -> Settings:
    return Settings(
        toolsets=frozenset({"sentinel"}),
        extra_tools=frozenset(),
        read_only=kw.get("read_only", False),
        cache_dir=tmp_path / "cache",
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        sentinel_dir=tmp_path / "sentinel",
        max_bars=5000,
        oanda_api_key=None,
        oanda_env="practice",
        session_id=None,
    )


# ---------------------------------------------------------------- synthetic day

def _bars(rows, start=SESSION_OPEN, minutes=TF_MIN) -> pd.DataFrame:
    """rows = [(open, high, low, close), ...] on a regular grid from `start`."""
    times = [start + pd.Timedelta(minutes=minutes * i) for i in range(len(rows))]
    return pd.DataFrame({
        "time": times,
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
        "volume": [10.0] * len(rows),
    })


# A 30-minute range (6 x M5) from 13:30, then a break up, two closes beyond, a
# pullback into the boundary zone, and a sweep of the named level PDL at 99.0.
#            open,  high,  low,   close
SESSION_ROWS = [
    (100.0, 101.0, 99.5, 100.5),   # 0 13:30 range bar
    (100.5, 102.0, 100.0, 101.0),  # 1 13:35 range bar (range high 102)
    (101.0, 101.5, 98.8, 99.2),    # 2 13:40 range bar: sweeps PDL 99.0, closes back above
    (99.2, 100.5, 99.0, 100.2),    # 3 13:45 range bar
    (100.2, 101.0, 99.8, 100.8),   # 4 13:50 range bar
    (100.8, 101.2, 100.3, 101.0),  # 5 13:55 range bar (range 102.0 / 98.8)
    (101.0, 102.4, 100.9, 102.3),  # 6 14:00 BREAK up (high > 102), close beyond -> 1 close
    (102.3, 103.0, 102.1, 102.9),  # 7 14:05 second close beyond -> BREAK_CONFIRMED
    (102.9, 103.1, 101.8, 102.6),  # 8 14:10 dips into the retest zone around 102 -> RETEST
    (102.6, 103.4, 102.4, 103.2),  # 9 14:15
]
SESSION_DF = _bars(SESSION_ROWS)


def _spec(**over) -> dict:
    spec = {
        "symbol": "EURUSD",
        "timeframe": "M5",
        "provider": "dukascopy",
        "day": DAY,
        "session": {"start": "13:30", "end": "20:00", "tz": "UTC"},
        "range": {"source": "session_open", "minutes": 30, "adr": 8.0},
        "levels": [{"name": "PDL", "price": 99.0}, {"name": "PDH", "price": 103.0}],
        "watch": {
            "breaks": True, "retest": True, "retest_after": "confirm",
            "retest_zone": {"ticks": 4}, "tick_size": 0.25,
            "sweep": True, "sweep_levels": ["PDL"], "levels": True, "stale_bars": 2,
        },
        "clocks": [{"name": "review", "at": "14:00", "tz": "UTC"}],
        "confirm": {"mode": "closes", "closes": 2},
    }
    spec.update(over)
    return spec


class _Feed:
    """Injected loader: serves bars up to `self.upto` (an index into SESSION_DF)."""

    def __init__(self, df=SESSION_DF, upto=None, empty=False):
        self.df = df
        self.upto = len(df) if upto is None else upto
        self.empty = empty
        self.calls = []

    def __call__(self, symbol, timeframe, count, provider, end):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "count": count,
                           "provider": provider, "end": end})
        sym, tf = resolve(symbol), resolve_timeframe(timeframe)
        if self.empty:
            return sym, tf, self.df.iloc[0:0]
        d = self.df.iloc[: self.upto]
        if end is not None:
            d = d[d["time"] <= end]
        return sym, tf, d.tail(count).reset_index(drop=True)


def _build(tmp_path, feed=None, **kw):
    mcp = FastMCP(name="test")
    settings = _settings(tmp_path, **kw)
    sentinel.register(mcp, settings, loader=feed or _Feed())
    return mcp, settings


def _call(mcp, name, args):
    res = asyncio.run(mcp.call_tool(name, args))
    blocks = res.content if hasattr(res, "content") else res
    return json.loads(blocks[0].text)


def _types(events):
    return [e["type"] for e in events]


# ---------------------------------------------------------------- spec model

def test_spec_rejects_unknown_fields_and_bad_references():
    with pytest.raises(Exception):
        SentinelSpec.model_validate({**_spec(), "bogus": 1})
    bad = _spec()
    bad["watch"] = {**bad["watch"], "break_levels": ["NOPE"]}
    with pytest.raises(Exception):
        SentinelSpec.model_validate(bad)
    dupes = _spec(levels=[{"name": "L", "price": 1.0}, {"name": "L", "price": 2.0}])
    with pytest.raises(Exception):
        SentinelSpec.model_validate(dupes)


def test_spec_session_name_uses_fixed_utc_table_and_overnight_rolls():
    spec = SentinelSpec.model_validate(_spec(session={"name": "new york"}))
    sched = schedule_for(spec, pd.Timestamp(DAY).date())
    assert sched["session_open"] == f"{DAY}T13:00:00Z"
    assert sched["session_close"] == f"{DAY}T22:00:00Z"
    overnight = SentinelSpec.model_validate(_spec(session={"name": "Sydney"}))
    s2 = schedule_for(overnight, pd.Timestamp(DAY).date())
    assert s2["session_open"] == f"{DAY}T21:00:00Z"
    assert s2["session_close"] == "2026-09-19T06:00:00Z"


def test_spec_dst_aware_for_named_timezone():
    spec = SentinelSpec.model_validate(
        _spec(session={"start": "09:30", "end": "16:00", "tz": "America/New_York"})
    )
    summer = schedule_for(spec, pd.Timestamp("2026-07-01").date())
    winter = schedule_for(spec, pd.Timestamp("2026-01-05").date())
    assert summer["session_open"] == "2026-07-01T13:30:00Z"  # EDT
    assert winter["session_open"] == "2026-01-05T14:30:00Z"  # EST


def test_spec_confirm_and_zone_validation():
    with pytest.raises(Exception):
        SentinelSpec.model_validate(_spec(confirm={"mode": "distance"}))
    with pytest.raises(Exception):
        SentinelSpec.model_validate(_spec(confirm={"mode": "distance", "ticks": 2, "percent": 1}))
    bad_zone = _spec()
    bad_zone["watch"] = {**bad_zone["watch"], "retest_zone": {"ticks": 1, "percent": 1}}
    with pytest.raises(Exception):
        SentinelSpec.model_validate(bad_zone)


# ---------------------------------------------------------------- the machine

def _run_machine(spec_dict, df, now=None, replay=False):
    spec = SentinelSpec.model_validate(spec_dict)
    state = machine.initial_state(schedule_for(spec, pd.Timestamp(DAY).date()))
    now = now or (df["time"].iloc[-1] + pd.Timedelta(minutes=TF_MIN))
    events = machine.advance(spec, state, df, now=now, seq_start=0,
                             timeframe_minutes=TF_MIN, replay=replay)
    return spec, state, events


def test_machine_walks_the_whole_session():
    _, state, events = _run_machine(_spec(), SESSION_DF)
    kinds = _types(events)
    assert kinds[0] == "SESSION_OPEN"
    assert kinds[1] == "RANGE_OPEN"
    assert "SWEEP" in kinds and "RANGE_CLOSED" in kinds
    assert kinds.index("RANGE_CLOSED") < kinds.index("BREAK") < kinds.index("BREAK_CONFIRMED")
    assert kinds.index("BREAK_CONFIRMED") < kinds.index("RETEST")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))

    closed = next(e for e in events if e["type"] == "RANGE_CLOSED")
    assert closed["payload"]["high"] == 102.0 and closed["payload"]["low"] == 98.8
    assert closed["payload"]["size"] == pytest.approx(3.2)
    assert closed["payload"]["size_vs_adr"] == pytest.approx(0.4)
    assert closed["payload"]["bars"] == 6
    assert closed["ts_utc"] == "2026-09-18T14:00:00Z"

    brk = next(e for e in events if e["type"] == "BREAK")
    assert brk["payload"] == {"side": "up", "level_name": "range_high", "boundary": 102.0,
                              "price": 102.4, "close": 102.3,
                              "bar_time": "2026-09-18T14:00:00Z", "confirmed": False}
    conf = next(e for e in events if e["type"] == "BREAK_CONFIRMED")
    assert conf["payload"]["closes_beyond"] == 2 and conf["payload"]["mode"] == "closes"
    assert conf["ts_utc"] == "2026-09-18T14:05:00Z"

    retest = next(e for e in events if e["type"] == "RETEST")
    assert retest["ts_utc"] == "2026-09-18T14:10:00Z"
    assert retest["payload"]["reaction"] == "closed_beyond"
    assert retest["payload"]["zone"] == [101.0, 103.0]  # 4 ticks x 0.25

    sweep = next(e for e in events if e["type"] == "SWEEP")
    assert sweep["payload"] == {"level": "PDL", "side": "down", "level_price": 99.0,
                                "wick_beyond": pytest.approx(0.2), "closed_back": True,
                                "close": 99.2, "bar_time": "2026-09-18T13:40:00Z"}

    tags = [e for e in events if e["type"] == "LEVEL_TAGGED"]
    assert [t["payload"]["level"] for t in tags] == ["PDL", "PDH"]
    clock = next(e for e in events if e["type"] == "CLOCK")
    assert clock["payload"]["name"] == "review" and clock["ts_utc"] == "2026-09-18T14:00:00Z"
    assert state["phase"] == "range_closed"


def test_machine_is_idempotent_over_the_same_bars():
    spec, state, first = _run_machine(_spec(), SESSION_DF)
    again = machine.advance(spec, state, SESSION_DF,
                            now=SESSION_DF["time"].iloc[-1] + pd.Timedelta(minutes=5),
                            seq_start=len(first), timeframe_minutes=TF_MIN)
    assert again == []


def test_machine_confirms_by_distance_and_by_pullback():
    # 2 ticks x 0.25 = 0.5: the break bar's close is only 0.3 beyond, the next one is 0.9
    _, _, events = _run_machine(_spec(confirm={"mode": "distance", "ticks": 2}), SESSION_DF)
    conf = next(e for e in events if e["type"] == "BREAK_CONFIRMED")
    assert conf["ts_utc"] == "2026-09-18T14:05:00Z"
    assert conf["payload"]["distance"] == pytest.approx(0.9)
    assert conf["payload"]["distance_required"] == pytest.approx(0.5)
    # 1 tick = 0.25: the break bar itself confirms
    _, _, events = _run_machine(_spec(confirm={"mode": "distance", "ticks": 1}), SESSION_DF)
    conf = next(e for e in events if e["type"] == "BREAK_CONFIRMED")
    assert conf["ts_utc"] == "2026-09-18T14:00:00Z"
    # percent works the same way
    _, _, events = _run_machine(_spec(confirm={"mode": "distance", "percent": 0.5}), SESSION_DF)
    conf = next(e for e in events if e["type"] == "BREAK_CONFIRMED")
    assert conf["payload"]["distance_required"] == pytest.approx(0.51)

    # pullback: bar 8 (14:10) dips back to 101.8, inside the range, and closes beyond again
    _, _, events = _run_machine(_spec(confirm={"mode": "pullback"}), SESSION_DF)
    conf = next(e for e in events if e["type"] == "BREAK_CONFIRMED")
    assert conf["ts_utc"] == "2026-09-18T14:10:00Z"
    assert conf["payload"]["pulled_back"] is True
    # a break bar alone never confirms in pullback mode
    early = [e for e in events if e["type"] == "BREAK_CONFIRMED"
             and e["ts_utc"] <= "2026-09-18T14:05:00Z"]
    assert early == []


def test_machine_needs_three_closes_when_asked():
    _, _, events = _run_machine(_spec(confirm={"mode": "closes", "closes": 3}), SESSION_DF)
    conf = [e for e in events if e["type"] == "BREAK_CONFIRMED"]
    assert conf and conf[0]["ts_utc"] == "2026-09-18T14:10:00Z"


def test_machine_reports_no_data_and_stale_once_per_episode():
    spec = SentinelSpec.model_validate(_spec())
    state = machine.initial_state(schedule_for(spec, pd.Timestamp(DAY).date()))
    now = SESSION_OPEN
    empty = SESSION_DF.iloc[0:0]
    first = machine.advance(spec, state, empty, now=now, seq_start=0, timeframe_minutes=TF_MIN)
    assert _types(first) == ["NO_DATA"]
    second = machine.advance(spec, state, empty, now=now, seq_start=1, timeframe_minutes=TF_MIN)
    assert second == []  # latched: no duplicate NO_DATA

    late = SESSION_DF["time"].iloc[-1] + pd.Timedelta(minutes=60)
    events = machine.advance(spec, state, SESSION_DF, now=late, seq_start=1,
                             timeframe_minutes=TF_MIN)
    stale = [e for e in events if e["type"] == "STALE_DATA"]
    assert len(stale) == 1
    assert stale[0]["payload"]["age_seconds"] == 60 * 60  # last bar 14:15 -> now 15:15
    assert stale[0]["payload"]["threshold_seconds"] == 600
    more = machine.advance(spec, state, SESSION_DF, now=late, seq_start=99,
                           timeframe_minutes=TF_MIN)
    assert more == []


def test_machine_marks_an_empty_range_window_incomplete():
    late_only = SESSION_DF.iloc[6:].reset_index(drop=True)  # nothing inside 13:30-14:00
    _, state, events = _run_machine(_spec(), late_only)
    closed = next(e for e in events if e["type"] == "RANGE_CLOSED")
    assert closed["payload"]["incomplete"] is True and closed["payload"]["bars"] == 0
    assert "BREAK" not in _types(events)  # no range -> no range boundary to break
    assert state["range"]["complete"] is False


def test_machine_emits_session_close():
    tail = _bars([(103.0, 103.2, 102.8, 103.0)], start=pd.Timestamp(f"{DAY}T20:00:00Z"))
    df = pd.concat([SESSION_DF, tail], ignore_index=True)
    _, state, events = _run_machine(_spec(), df)
    close = next(e for e in events if e["type"] == "SESSION_CLOSE")
    assert close["ts_utc"] == f"{DAY}T20:00:00Z"
    assert close["payload"]["session_high"] == 103.4
    assert state["phase"] == "session_closed"


# ---------------------------------------------------------------- the tools

def test_start_poll_stop_round_trip(tmp_path):
    feed = _Feed()
    mcp, settings = _build(tmp_path, feed)
    started = _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "day1"})
    assert started["run_id"] == "day1"
    assert started["state"]["phase"] == "pending"
    assert "SESSION_OPEN" in started["event_types"]
    assert started["spec_echo"]["session"]["start"] == "13:30"
    assert (settings.sentinel_dir / "day1.json").exists()

    polled = _call(mcp, "tv_sentinel_poll", {"run_id": "day1"})
    kinds = _types(polled["events"])
    assert kinds[0] == "SESSION_OPEN" and "BREAK_CONFIRMED" in kinds
    assert polled["last_seq"] == len(polled["events"])
    assert polled["state"]["range"]["high"] == 102.0
    assert polled["warnings"] == []
    assert polled["next_poll_after_s"] > 0

    stopped = _call(mcp, "tv_sentinel_stop", {"run_id": "day1"})
    assert stopped["stopped"] is True and stopped["already_stopped"] is False
    again = _call(mcp, "tv_sentinel_stop", {"run_id": "day1"})
    assert again["already_stopped"] is True
    after = _call(mcp, "tv_sentinel_poll", {"run_id": "day1"})
    assert after["events"] == polled["events"]
    assert any("stopped" in w for w in after["warnings"])
    assert after["next_poll_after_s"] == 0


def test_poll_is_idempotent_and_since_seq_filters_exactly(tmp_path):
    mcp, _ = _build(tmp_path, _Feed())
    _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "idem"})
    first = _call(mcp, "tv_sentinel_poll", {"run_id": "idem"})
    second = _call(mcp, "tv_sentinel_poll", {"run_id": "idem"})
    assert first["events"] == second["events"]
    assert first["last_seq"] == second["last_seq"]

    cut = first["events"][2]["seq"]
    tail = _call(mcp, "tv_sentinel_poll", {"run_id": "idem", "since_seq": cut})
    assert tail["events"] == [e for e in first["events"] if e["seq"] > cut]
    assert tail["events"][0]["seq"] == cut + 1

    capped = _call(mcp, "tv_sentinel_poll", {"run_id": "idem", "max_events": 2})
    assert capped["returned_count"] == 2 and capped["truncated"] is True
    assert capped["pending_count"] == len(first["events"])
    assert capped["events"] == first["events"][:2]


def test_poll_advances_incrementally_as_bars_arrive(tmp_path):
    feed = _Feed(upto=6)  # only the range bars so far
    mcp, _ = _build(tmp_path, feed)
    _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "live"})
    first = _call(mcp, "tv_sentinel_poll", {"run_id": "live"})
    assert "BREAK" not in _types(first["events"])
    assert first["state"]["phase"] == "range_forming"

    feed.upto = 8  # two more bars close
    second = _call(mcp, "tv_sentinel_poll", {"run_id": "live", "since_seq": first["last_seq"]})
    # the 14:00 bar carries the clock mark first, then the range closes on it; the
    # 14:05 bar tags PDH before it confirms the break (per-bar order is fixed)
    assert _types(second["events"])[:5] == [
        "CLOCK", "RANGE_CLOSED", "BREAK", "LEVEL_TAGGED", "BREAK_CONFIRMED"]
    assert all(e["seq"] > first["last_seq"] for e in second["events"])
    # everything already reported keeps its seq and payload
    replayed = _call(mcp, "tv_sentinel_poll", {"run_id": "live"})
    assert replayed["events"][: len(first["events"])] == first["events"]


def test_poll_reports_no_data_when_the_feed_is_empty(tmp_path):
    mcp, _ = _build(tmp_path, _Feed(empty=True))
    _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "dark"})
    out = _call(mcp, "tv_sentinel_poll", {"run_id": "dark"})
    assert _types(out["events"]) == ["NO_DATA"]
    assert out["state"]["flags"]["no_data"] is True


def test_poll_survives_a_loader_error_as_no_data(tmp_path):
    def boom(symbol, timeframe, count, provider, end):
        raise ToolError("provider exploded")

    mcp, _ = _build(tmp_path, boom)
    _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "boom"})
    out = _call(mcp, "tv_sentinel_poll", {"run_id": "boom"})
    assert _types(out["events"]) == ["NO_DATA"]
    assert "provider exploded" in out["events"][0]["payload"]["reason"]
    assert any("provider exploded" in w for w in out["warnings"])


def test_second_start_with_the_same_run_id_refuses(tmp_path):
    mcp, settings = _build(tmp_path, _Feed())
    _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "dup"})
    polled = _call(mcp, "tv_sentinel_poll", {"run_id": "dup"})
    with pytest.raises(ToolError) as exc:
        _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "dup"})
    assert "already exists" in str(exc.value)
    # the live run is untouched
    doc, _ = store.load(settings.sentinel_dir, "dup")
    assert len(doc["events"]) == len(polled["events"])


def test_replay_reproduces_the_live_sequence(tmp_path):
    feed = _Feed()
    mcp, _ = _build(tmp_path, feed)
    # live: one bar at a time
    _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "live2"})
    for upto in range(1, len(SESSION_ROWS) + 1):
        feed.upto = upto
        live = _call(mcp, "tv_sentinel_poll", {"run_id": "live2"})

    replay = _call(mcp, "tv_sentinel_start", {
        "spec": _spec(), "run_id": "rep",
        "replay": {"from": f"{DAY}T13:30:00Z", "to": f"{DAY}T14:20:00Z"},
    })
    assert replay["state"]["replay"] == {"from": f"{DAY}T13:30:00Z", "to": f"{DAY}T14:20:00Z"}
    out = _call(mcp, "tv_sentinel_poll", {"run_id": "rep"})
    # seq numbering differs (a live run of a past day also reports the stale feed);
    # the event sequence itself must be identical
    live_events = [(e["type"], e["ts_utc"], e["payload"]) for e in live["events"]
                   if e["type"] != "STALE_DATA"]
    replay_events = [(e["type"], e["ts_utc"], e["payload"]) for e in out["events"]]
    assert replay_events == live_events
    assert [e["seq"] for e in out["events"]] == list(range(1, len(out["events"]) + 1))
    assert out["next_poll_after_s"] == 0
    # a replay is done after one pass
    second = _call(mcp, "tv_sentinel_poll", {"run_id": "rep"})
    assert second["events"] == out["events"]
    assert any("replay" in w for w in second["warnings"])


def test_replay_clamps_the_window_it_feeds(tmp_path):
    feed = _Feed()
    mcp, _ = _build(tmp_path, feed)
    _call(mcp, "tv_sentinel_start", {
        "spec": _spec(), "run_id": "short",
        "replay": {"from": f"{DAY}T13:30:00Z", "to": f"{DAY}T14:00:00Z"},
    })
    out = _call(mcp, "tv_sentinel_poll", {"run_id": "short"})
    assert out["state"]["last_bar"] == f"{DAY}T14:00:00Z"
    assert "BREAK_CONFIRMED" not in _types(out["events"])
    assert feed.calls[-1]["end"] == pd.Timestamp(f"{DAY}T14:00:00Z")


def test_status_lists_runs_and_reads_one(tmp_path):
    mcp, _ = _build(tmp_path, _Feed())
    _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "a1"})
    _call(mcp, "tv_sentinel_start", {"spec": _spec(symbol="GBPUSD"), "run_id": "b2"})
    listing = _call(mcp, "tv_sentinel_status", {})
    assert listing["count"] == 2
    assert {r["run_id"] for r in listing["runs"]} == {"a1", "b2"}
    one = _call(mcp, "tv_sentinel_status", {"run_id": "a1"})
    assert one["state"]["phase"] == "pending" and one["events_stored"] == 0
    assert one["spec_echo"]["symbol"] == "EURUSD"
    with pytest.raises(ToolError):
        _call(mcp, "tv_sentinel_status", {"run_id": "nope"})


def test_generated_run_id_and_bad_ids(tmp_path):
    mcp, _ = _build(tmp_path, _Feed())
    started = _call(mcp, "tv_sentinel_start", {"spec": _spec()})
    assert started["run_id"].startswith(f"EURUSD-M5-{DAY}-")
    for bad in ("../escape", "a/b", "", "x" * 80):
        with pytest.raises(ToolError):
            _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": bad})


def test_invalid_spec_is_an_actionable_error(tmp_path):
    mcp, _ = _build(tmp_path, _Feed())
    with pytest.raises(ToolError) as exc:
        _call(mcp, "tv_sentinel_start", {"spec": {"symbol": "EURUSD"}})
    assert "session" in str(exc.value).lower()
    with pytest.raises(ToolError):
        _call(mcp, "tv_sentinel_start", {"spec": _spec(session={"name": "Narnia"})})


def test_session_provider_needs_the_opt_in_toolset(tmp_path):
    mcp = FastMCP(name="test")
    settings = _settings(tmp_path)
    sentinel.register(mcp, settings)  # real loader
    _call(mcp, "tv_sentinel_start", {"spec": _spec(provider="session"), "run_id": "sess"})
    out = _call(mcp, "tv_sentinel_poll", {"run_id": "sess"})
    assert _types(out["events"]) == ["NO_DATA"]
    assert "session" in out["events"][0]["payload"]["reason"]


# ---------------------------------------------------------------- run files

def test_run_file_is_reparsed_defensively(tmp_path):
    mcp, settings = _build(tmp_path, _Feed())
    _call(mcp, "tv_sentinel_start", {"spec": _spec(), "run_id": "tamper"})
    path = store.run_path(settings.sentinel_dir, "tamper")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["spec"]["session"] = {"name": "Not A Session"}
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ToolError) as exc:
        _call(mcp, "tv_sentinel_poll", {"run_id": "tamper"})
    assert "invalid spec" in str(exc.value).lower()

    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ToolError):
        _call(mcp, "tv_sentinel_poll", {"run_id": "tamper"})


def test_store_writes_atomically_and_lists(tmp_path):
    directory = tmp_path / "runs"
    doc = {"version": 1, "run_id": "r", "spec": _spec(), "state": {"phase": "pending"},
           "events": [], "cursor": {"last_seq": 0}, "created": store.now_iso()}
    store.save(directory, "r", doc)
    assert not list(directory.glob("*.tmp"))
    assert [r["run_id"] for r in store.list_runs(directory)] == ["r"]
    (directory / "junk.json").write_text("nope", encoding="utf-8")
    assert [r["run_id"] for r in store.list_runs(directory)] == ["r"]  # unreadable skipped
    with pytest.raises(store.StoreError):
        store.load(directory, "missing")


# ---------------------------------------------------------------- registration

def test_read_only_registers_status_only(tmp_path):
    mcp, _ = _build(tmp_path, _Feed(), read_only=True)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"tv_sentinel_status"}


def test_toolset_gating(tmp_path):
    from tvmcp.config import ALL_TOOLSETS, HYBRID_TOOLSETS, load_settings
    from tvmcp.server import build_server

    assert "sentinel" in ALL_TOOLSETS and "sentinel" in HYBRID_TOOLSETS
    assert load_settings({"TV_SENTINEL_DIR": str(tmp_path)}).sentinel_dir == tmp_path
    default = build_server(_settings(tmp_path)._replace_toolsets({"public", "data"})
                           if hasattr(Settings, "_replace_toolsets") else
                           Settings(**{**_settings(tmp_path).__dict__,
                                       "toolsets": frozenset({"public", "data"})}))
    names = {t.name for t in asyncio.run(default.list_tools())}
    assert not any(n.startswith("tv_sentinel_") for n in names)

    on = build_server(Settings(**{**_settings(tmp_path).__dict__,
                                  "toolsets": frozenset({"sentinel"})}))
    names = {t.name for t in asyncio.run(on.list_tools())}
    assert {"tv_sentinel_start", "tv_sentinel_poll", "tv_sentinel_status",
            "tv_sentinel_stop"} <= names


def test_utc_only_timestamps():
    _, _, events = _run_machine(_spec(), SESSION_DF)
    for e in events:
        assert e["ts_utc"].endswith("Z")
        assert pd.Timestamp(e["ts_utc"]).tzinfo == timezone.utc


class _AnchorlessFeed(_Feed):
    """A feed with no end anchor: it always returns the LAST `count` bars.

    This is the account-cookie path (SessionClient.get_bars(symbol, tf, count)),
    which is what a replay of an older day actually talks to.
    """

    def __call__(self, symbol, timeframe, count, provider, end):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "count": count,
                           "provider": provider, "end": end})
        sym, tf = resolve(symbol), resolve_timeframe(timeframe)
        return sym, tf, self.df.tail(count).reset_index(drop=True)


def test_replay_of_an_older_window_reaches_back_on_an_anchorless_feed(tmp_path):
    """Sizing the request by the replay window returns today's bars, not the day asked for.

    Live on the owner's account feed this showed up as NO_DATA for a replay of a
    session ten days back: the window is three hours wide, so the run asked for
    ~56 bars, got the newest 56, and the window filter dropped every one. The
    count must span from the replay start to now whenever the feed cannot be
    anchored to an end.
    """
    # The session under test sits at the START of the frame; "now" is far later,
    # with a long tail of newer bars in front of it.
    tail = SESSION_DF.copy()
    tail["time"] = tail["time"] + pd.Timedelta(days=7)
    df = pd.concat([SESSION_DF, tail], ignore_index=True)

    feed = _AnchorlessFeed(df=df)
    mcp, _ = _build(tmp_path, feed=feed)
    spec = _spec()
    spec["provider"] = "session"
    replay = {"from": str(SESSION_DF["time"].iloc[0]), "to": str(SESSION_DF["time"].iloc[-1])}
    _call(mcp, "tv_sentinel_start", {"spec": spec, "run_id": "older", "replay": replay})
    out = _call(mcp, "tv_sentinel_poll", {"run_id": "older", "since_seq": 0, "max_events": 200})

    assert feed.calls, "the loader was never called"
    # Asked for enough bars to reach back over the week-long gap, not just the window.
    assert feed.calls[-1]["count"] > len(SESSION_DF)
    assert "NO_DATA" not in _types(out["events"])
    assert "RANGE_CLOSED" in _types(out["events"])


def test_history_too_short_says_so_instead_of_a_bare_no_data(tmp_path):
    """A feed whose history starts after the window is a different problem from a dead feed."""
    late = SESSION_DF.copy()
    late["time"] = late["time"] + pd.Timedelta(days=30)
    feed = _AnchorlessFeed(df=late)
    mcp, _ = _build(tmp_path, feed=feed)
    spec = _spec()
    spec["provider"] = "session"
    replay = {"from": str(SESSION_DF["time"].iloc[0]), "to": str(SESSION_DF["time"].iloc[-1])}
    _call(mcp, "tv_sentinel_start", {"spec": spec, "run_id": "short", "replay": replay})
    out = _call(mcp, "tv_sentinel_poll", {"run_id": "short", "since_seq": 0, "max_events": 50})

    assert any("history starts at" in w for w in out["warnings"]), out["warnings"]
    no_data = [e for e in out["events"] if e["type"] == "NO_DATA"]
    assert no_data and "history starts at" in no_data[0]["payload"]["reason"]
