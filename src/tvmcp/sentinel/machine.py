"""The sentinel state machine: bars in, events out. Pure, deterministic, strategy-free.

One session occurrence per run. The machine walks CLOSED bars in time order and
reports mechanics only - a range forming and closing, a boundary being traded
through and (by the spec's own definition) confirmed, price coming back to a broken
boundary, a named level being taken and given back, a level being touched, a clock
mark passing, a feed going quiet. Nothing here scores a setup, sizes a trade or
names a direction as good: the payloads are observations, the caller's workflow
decides what they mean.

Determinism and idempotency:
- bars are processed strictly in time order, each at most once (`state.last_bar_ts`
  is the cursor); re-feeding the same bars produces no new events;
- every event gets the next `seq` and is never rewritten;
- time-based events (CLOCK when no bars arrive, STALE_DATA, NO_DATA) are latched by
  a flag so a repeated poll in the same condition does not append a duplicate.
"""

from __future__ import annotations

import pandas as pd

from ..scan.levels import check_levels
from .spec import SentinelSpec, iso

# The whole vocabulary. Generic on purpose: no setup numbers, no bias, no targets.
EVENT_TYPES = (
    "SESSION_OPEN",
    "SESSION_CLOSE",
    "RANGE_OPEN",
    "RANGE_CLOSED",
    "BREAK",
    "BREAK_CONFIRMED",
    "RETEST",
    "SWEEP",
    "LEVEL_TAGGED",
    "CLOCK",
    "STALE_DATA",
    "NO_DATA",
)

PHASES = (
    "pending",
    "session_open",
    "range_forming",
    "range_closed",
    "session_closed",
    "stopped",
)


def initial_state(schedule: dict) -> dict:
    return {
        "phase": "pending",
        "schedule": schedule,
        "session": {"opened": False, "closed": False, "high": None, "low": None},
        "range": {"opened": False, "closed": False, "complete": False,
                  "high": None, "low": None, "size": None, "bars": 0},
        "breaks": {},
        "levels_tagged": {},
        "sweeps": {},
        "clocks_done": [],
        "flags": {"stale": False, "no_data": False},
        "last_bar_ts": None,
        "bars_seen": 0,
    }


def _f(x) -> float:
    return round(float(x), 8)


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], utc=True)
    d = d.sort_values("time").drop_duplicates(subset="time", keep="last")
    return d.reset_index(drop=True)


def advance(
    spec: SentinelSpec,
    state: dict,
    df: pd.DataFrame | None,
    *,
    now: pd.Timestamp,
    seq_start: int,
    timeframe_minutes: int,
    replay: bool = False,
    no_data_reason: str | None = None,
) -> list[dict]:
    """Feed bars to the machine; returns the NEW events and mutates `state` in place."""
    events: list[dict] = []
    seq = seq_start

    def emit(type_: str, ts, payload: dict) -> None:
        nonlocal seq
        seq += 1
        events.append({"seq": seq, "ts_utc": iso(ts), "type": type_, "payload": payload})

    sched = state["schedule"]
    session_open = pd.Timestamp(sched["session_open"])
    session_close = pd.Timestamp(sched["session_close"])
    range_open = pd.Timestamp(sched["range_open"])
    range_close = pd.Timestamp(sched["range_close"])

    if df is None or len(df) == 0:
        if not state["flags"]["no_data"]:
            emit("NO_DATA", now, {
                "reason": no_data_reason or "the loader returned no bars for the window",
                "last_bar": state["last_bar_ts"],
            })
            state["flags"]["no_data"] = True
        _fire_due_clocks(spec, state, emit, now, replay)
        return events
    state["flags"]["no_data"] = False

    d = _frame(df)
    cursor = pd.Timestamp(state["last_bar_ts"]) if state["last_bar_ts"] else None
    fresh = d[d["time"] > cursor] if cursor is not None else d
    if len(fresh):
        state["flags"]["stale"] = False

    tag_at = _level_tags(spec, state, fresh, timeframe_minutes, now)

    for row in fresh.itertuples(index=False):
        t = row.time
        o, h, low, c = float(row.open), float(row.high), float(row.low), float(row.close)

        # 1. clock marks that this bar has passed
        for clock in sched["clocks"]:
            at = pd.Timestamp(clock["at"])
            if clock["name"] not in state["clocks_done"] and t >= at:
                state["clocks_done"].append(clock["name"])
                emit("CLOCK", at, {"name": clock["name"], "at": clock["at"],
                                   "bar_time": iso(t), "close": _f(c)})

        # 2. session boundaries
        if not state["session"]["opened"] and t >= session_open:
            state["session"]["opened"] = True
            state["phase"] = "session_open"
            emit("SESSION_OPEN", t, {"session": sched["session"], "open": _f(o),
                                     "window": [sched["session_open"], sched["session_close"]]})
        if state["session"]["opened"] and not state["session"]["closed"] and t < session_close:
            s = state["session"]
            s["high"] = _f(h) if s["high"] is None else _f(max(s["high"], h))
            s["low"] = _f(low) if s["low"] is None else _f(min(s["low"], low))

        # 3. range window
        if not state["range"]["opened"] and t >= range_open:
            state["range"]["opened"] = True
            state["phase"] = "range_forming"
            emit("RANGE_OPEN", t, {"source": spec.range.source, "minutes": spec.range.minutes,
                                   "window": [sched["range_open"], sched["range_close"]],
                                   "open": _f(o)})
        if range_open <= t < range_close:
            r = state["range"]
            r["high"] = _f(h) if r["high"] is None else _f(max(r["high"], h))
            r["low"] = _f(low) if r["low"] is None else _f(min(r["low"], low))
            r["bars"] += 1
        if not state["range"]["closed"] and t >= range_close:
            r = state["range"]
            r["closed"] = True
            r["complete"] = r["bars"] > 0
            state["phase"] = "range_closed"
            payload = {"window": [sched["range_open"], sched["range_close"]],
                       "bars": r["bars"], "high": r["high"], "low": r["low"], "size": None}
            if r["complete"]:
                r["size"] = _f(r["high"] - r["low"])
                payload["size"] = r["size"]
                if spec.range.adr:
                    payload["size_vs_adr"] = round(r["size"] / float(spec.range.adr), 4)
                    payload["adr"] = float(spec.range.adr)
            else:
                payload["incomplete"] = True
                payload["note"] = "no bars inside the range window - the feed had a gap"
            emit("RANGE_CLOSED", t, payload)

        # 4. level tags (touch test comes from scan/levels.check_levels)
        for name in tag_at.get(iso(t), []):
            lv = spec.level(name)
            state["levels_tagged"][name] = iso(t)
            entry = {"level": name, "bar_time": iso(t), "high": _f(h), "low": _f(low)}
            entry.update(lv.as_check_level() if lv else {})
            entry["from"] = tag_at["_from"].get(name)
            emit("LEVEL_TAGGED", t, entry)

        # 5. sweeps of named levels
        for name in spec.sweep_names():
            lv = spec.level(name)
            if lv is None:
                continue
            lo_b, hi_b = lv.bounds()
            for side, taken, wick in (
                ("down", low < lo_b and c > lo_b, lo_b - low),
                ("up", h > hi_b and c < hi_b, h - hi_b),
            ):
                key = f"{name}:{side}"
                if not taken or (spec.watch.sweep_once and key in state["sweeps"]):
                    continue
                state["sweeps"][key] = iso(t)
                emit("SWEEP", t, {"level": name, "side": side,
                                  "level_price": _f(lo_b if side == "down" else hi_b),
                                  "wick_beyond": _f(wick), "closed_back": True,
                                  "close": _f(c), "bar_time": iso(t)})

        # 6. breaks / confirmations / retests
        if spec.watch.breaks:
            for key, name, side, boundary in _boundaries(spec, state):
                _step_break(spec, state, emit, key, name, side, boundary,
                            t=t, high=h, low=low, close=c)

        # 7. session close
        if not state["session"]["closed"] and t >= session_close:
            state["session"]["closed"] = True
            state["phase"] = "session_closed"
            emit("SESSION_CLOSE", t, {"session": sched["session"], "close": _f(c),
                                      "session_high": state["session"]["high"],
                                      "session_low": state["session"]["low"]})

        state["last_bar_ts"] = iso(t)
        state["bars_seen"] += 1

    _fire_due_clocks(spec, state, emit, now, replay)

    # feed health: only meaningful live - a replay's "now" is the window's end
    if not replay and state["last_bar_ts"]:
        last = pd.Timestamp(state["last_bar_ts"])
        threshold = pd.Timedelta(minutes=spec.watch.stale_bars * timeframe_minutes)
        age = now - last
        if age > threshold and not state["flags"]["stale"]:
            state["flags"]["stale"] = True
            emit("STALE_DATA", now, {
                "last_bar": state["last_bar_ts"],
                "age_seconds": int(age.total_seconds()),
                "threshold_seconds": int(threshold.total_seconds()),
                "timeframe_minutes": timeframe_minutes,
            })
    return events


def _fire_due_clocks(spec: SentinelSpec, state: dict, emit, now: pd.Timestamp, replay: bool) -> None:
    """Live runs announce a clock mark even when no bar has arrived since it passed.

    Only while the session is still running: once it has closed (or in a replay),
    the bars decide, so a run started over a past day does not fire every mark at
    once from the wall clock.
    """
    if replay or now >= pd.Timestamp(state["schedule"]["session_close"]):
        return
    for clock in state["schedule"]["clocks"]:
        at = pd.Timestamp(clock["at"])
        if clock["name"] not in state["clocks_done"] and now >= at:
            state["clocks_done"].append(clock["name"])
            emit("CLOCK", at, {"name": clock["name"], "at": clock["at"],
                               "bar_time": None, "close": None})


def _level_tags(spec: SentinelSpec, state: dict, fresh: pd.DataFrame,
                timeframe_minutes: int, now: pd.Timestamp) -> dict:
    """{bar_iso: [level names first tagged there]} + `_from` side, via check_levels."""
    out: dict = {"_from": {}}
    pending = [lv for lv in spec.levels if lv.name not in state["levels_tagged"]]
    if not (spec.watch.levels and pending and len(fresh)):
        return out
    since = fresh["time"].iloc[0]
    results = check_levels(
        fresh, [lv.as_check_level() for lv in pending], since,
        timeframe_minutes, now=now,
    )
    for r in results:
        if r.get("tagged") and r.get("first_tag"):
            out.setdefault(r["first_tag"]["time"], []).append(r["name"])
            out["_from"][r["name"]] = r["first_tag"].get("from")
    return out


def _boundaries(spec: SentinelSpec, state: dict) -> list[tuple[str, str, str, float]]:
    """Armed break boundaries: (key, level_name, side, price)."""
    out: list[tuple[str, str, str, float]] = []
    r = state["range"]
    if spec.watch.break_range and r["closed"] and r["complete"]:
        out.append(("range_high", "range_high", "up", float(r["high"])))
        out.append(("range_low", "range_low", "down", float(r["low"])))
    if state["session"]["opened"]:
        for name in spec.watch.break_levels:
            lv = spec.level(name)
            if lv is None:
                continue
            lo_b, hi_b = lv.bounds()
            out.append((f"{name}:up", name, "up", hi_b))
            out.append((f"{name}:down", name, "down", lo_b))
    return out


def _beyond(side: str, value: float, boundary: float) -> bool:
    return value > boundary if side == "up" else value < boundary


def _step_break(spec: SentinelSpec, state: dict, emit, key: str, name: str, side: str,
                boundary: float, *, t, high: float, low: float, close: float) -> None:
    rec = state["breaks"].get(key)
    traded_beyond = _beyond(side, high if side == "up" else low, boundary)
    closed_beyond = _beyond(side, close, boundary)

    if rec is None:
        if not traded_beyond:
            return
        rec = {
            "key": key, "level": name, "side": side, "boundary": _f(boundary),
            "break_ts": iso(t), "confirmed": False, "confirmed_ts": None,
            "closes_beyond": 0, "pulled_back": False, "retest_done": False,
        }
        state["breaks"][key] = rec
        emit("BREAK", t, {"side": side, "level_name": name, "boundary": _f(boundary),
                          "price": _f(high if side == "up" else low),
                          "close": _f(close), "bar_time": iso(t), "confirmed": False})

    if not rec["confirmed"]:
        _confirm(spec, rec, emit, side=side, boundary=boundary, t=t, close=close,
                 low=low, high=high, name=name)
        return

    if spec.watch.retest and not rec["retest_done"]:
        started = pd.Timestamp(rec["confirmed_ts"] if spec.watch.retest_after == "confirm"
                               else rec["break_ts"])
        if t <= started:
            return
        width = spec.watch.retest_zone.width(  # type: ignore[union-attr]
            boundary, state["range"].get("size"), spec.watch.tick_size
        )
        zone_lo, zone_hi = boundary - width, boundary + width
        if high >= zone_lo and low <= zone_hi:
            rec["retest_done"] = True
            if closed_beyond:
                reaction = "closed_beyond"
            elif close == boundary:
                reaction = "closed_at_boundary"
            else:
                reaction = "closed_back_inside"
            emit("RETEST", t, {"level": name, "side": side, "boundary": _f(boundary),
                               "zone": [_f(zone_lo), _f(zone_hi)], "reaction": reaction,
                               "high": _f(high), "low": _f(low), "close": _f(close),
                               "bar_time": iso(t)})


def _confirm(spec: SentinelSpec, rec: dict, emit, *, side: str, boundary: float, t,
             close: float, low: float, high: float, name: str) -> None:
    mode = spec.confirm.mode
    closed_beyond = _beyond(side, close, boundary)
    detail: dict = {"mode": mode}

    if mode == "closes":
        rec["closes_beyond"] = rec["closes_beyond"] + 1 if closed_beyond else 0
        detail["closes_beyond"] = rec["closes_beyond"]
        detail["closes_required"] = spec.confirm.closes
        confirmed = rec["closes_beyond"] >= spec.confirm.closes
    elif mode == "distance":
        need = spec.confirm.distance(boundary, spec.watch.tick_size)
        got = (close - boundary) if side == "up" else (boundary - close)
        detail["distance"] = _f(got)
        detail["distance_required"] = _f(need)
        confirmed = closed_beyond and got >= need
    else:  # pullback: back into the range, then a close beyond again
        if not rec["pulled_back"] and pd.Timestamp(rec["break_ts"]) < pd.Timestamp(t):
            back_inside = (low <= boundary) if side == "up" else (high >= boundary)
            if back_inside:
                rec["pulled_back"] = True
        detail["pulled_back"] = rec["pulled_back"]
        confirmed = rec["pulled_back"] and closed_beyond and pd.Timestamp(rec["break_ts"]) < pd.Timestamp(t)

    if confirmed:
        rec["confirmed"] = True
        rec["confirmed_ts"] = iso(t)
        emit("BREAK_CONFIRMED", t, {"side": side, "level_name": name,
                                    "boundary": _f(boundary), "close": _f(close),
                                    "bar_time": iso(t), "break_time": rec["break_ts"],
                                    **detail})


def waiting_for(state: dict) -> str:
    """One word for what the machine is waiting on - drives the poll-interval hint."""
    if state.get("stopped") or state.get("phase") == "stopped":
        return "stopped"
    breaks = state.get("breaks") or {}
    if any(not b["confirmed"] for b in breaks.values()):
        return "confirmation"
    if any(b["confirmed"] and not b["retest_done"] for b in breaks.values()):
        return "retest"
    phase = state.get("phase", "pending")
    return {
        "pending": "session_open",
        "session_open": "range_open" if not state["range"]["opened"] else "range_close",
        "range_forming": "range_close",
        "range_closed": "break",
        "session_closed": "nothing",
    }.get(phase, "bars")
