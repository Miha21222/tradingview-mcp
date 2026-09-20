"""`sentinel` toolset: a polled session-range watcher (M9).

Why no daemon: this MCP server is request/response over stdio. A background thread
watching a market would have nowhere to push an event, would die with the client,
and would make two agents sharing one server fight over the same state. So the run
is a FILE and the caller polls: `tv_sentinel_start` creates it, `tv_sentinel_poll`
pulls the bars that appeared since the last poll, advances the machine, appends
events and hands back the ones newer than `since_seq` plus a `next_poll_after_s`
hint, `tv_sentinel_status` reads, `tv_sentinel_stop` closes the run (the file
stays for the journal).

Polling is idempotent: bars are processed once (cursor), events are numbered once
and never rewritten, so polling twice with the same `since_seq` returns the same
events.

The machine is generic (see `machine.py`): sessions, ranges, breaks, retests,
sweeps, level tags, clocks, feed health. Trade semantics - what a break means, how
big a position is, which target to take - are workflow, and deliberately live in
the caller's skill, not here.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Callable

import pandas as pd
from fastmcp.exceptions import ToolError
from pydantic import Field, ValidationError

from ..bars import load_bars, window
from ..cache import BarCache
from ..config import Settings
from ..symbols import resolve, resolve_timeframe
from . import machine, store
from .spec import ReplaySpec, SentinelSpec, iso, schedule_for

MAX_EVENTS = 500
MAX_POLL_INTERVAL_S = 900
_PAD_BARS = 20


def _fail(exc: Exception, what: str) -> ToolError:
    if isinstance(exc, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or what}: {e['msg']}" for e in exc.errors()[:5]
        )
        return ToolError(f"Invalid {what}: {problems}")
    return ToolError(f"Invalid {what}: {exc}")


def _parse_spec(raw: dict) -> SentinelSpec:
    if not isinstance(raw, dict):
        raise ToolError("spec must be an object")
    try:
        return SentinelSpec.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        raise _fail(exc, "spec") from exc


def _parse_replay(raw: dict | None) -> ReplaySpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ToolError("replay must be an object {from, to}")
    try:
        return ReplaySpec.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        raise _fail(exc, "replay") from exc


def _default_run_id(spec: SentinelSpec, day: date) -> str:
    sym = "".join(ch for ch in spec.symbol.upper() if ch.isalnum()) or "SYM"
    return f"{sym}-{spec.timeframe.upper()}-{day.isoformat()}-{uuid.uuid4().hex[:4]}"


def _resolve(spec: SentinelSpec):
    try:
        return resolve(spec.symbol), resolve_timeframe(spec.timeframe)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _public_state(doc: dict) -> dict:
    state = doc["state"]
    cursor = doc.get("cursor") or {}
    return {
        "run_id": doc.get("run_id"),
        "status": doc.get("status", "running"),
        "phase": state.get("phase"),
        "waiting_for": machine.waiting_for(state),
        "schedule": state.get("schedule"),
        "session": state.get("session"),
        "range": state.get("range"),
        "breaks": list((state.get("breaks") or {}).values()),
        "levels_tagged": state.get("levels_tagged"),
        "sweeps": state.get("sweeps"),
        "clocks_done": state.get("clocks_done"),
        "flags": state.get("flags"),
        "last_bar": state.get("last_bar_ts"),
        "bars_seen": state.get("bars_seen", 0),
        "last_seq": cursor.get("last_seq", 0),
        "last_poll": cursor.get("last_poll"),
        "replay": doc.get("replay"),
    }


def _next_poll_after_s(state: dict, tf_minutes: int, now: pd.Timestamp,
                       replay: bool, stopped: bool) -> int:
    if replay or stopped:
        return 0
    base = max(60, tf_minutes * 60)
    waiting = machine.waiting_for(state)
    if waiting in ("confirmation", "retest"):
        value = max(5, base // 4)
    elif waiting == "range_close":
        value = max(5, base // 2)
    elif waiting == "session_open":
        opens = pd.Timestamp(state["schedule"]["session_open"])
        wait = (opens - now).total_seconds()
        value = int(min(max(wait, 5), 300)) if wait > 0 else max(5, base // 2)
    elif waiting == "nothing":
        value = base
    else:
        value = max(10, base)
    return int(min(value, MAX_POLL_INTERVAL_S))


def register(mcp: Any, settings: Settings, loader: Callable | None = None) -> None:
    cache = BarCache(settings.cache_dir)
    directory = settings.sentinel_dir

    def _default_load(symbol: str, timeframe: str, count: int, provider: str,
                      end: pd.Timestamp | None):
        sym, tf = resolve(symbol), resolve_timeframe(timeframe)
        count = min(count, settings.max_bars)
        if provider == "session":
            if not settings.toolset_enabled("session"):
                raise ToolError(
                    "spec.provider='session' needs the opt-in `session` toolset "
                    "(TV_TOOLSETS=...,session) and TV_SESSIONID - it reads through "
                    "YOUR TradingView account cookie (ToS risk). Use auto/dukascopy/oanda "
                    "for the free feeds."
                )
            if not settings.session_id:
                raise ToolError("TV_SESSIONID not set; the `session` provider needs it")
            from ..session.client import SessionClient
            from ..session.warnings import warn_once

            warn_once()
            df = SessionClient(settings.session_id).get_bars(sym, tf, count)
        else:
            end_ts = end if end is not None else pd.Timestamp.now(tz="UTC")
            start_ts, _ = window(count, tf.minutes, end_ts)
            df = load_bars(settings, cache, sym, tf, start_ts, end_ts, count, provider)
        return sym, tf, df

    load = loader or _default_load

    def _feed_window(doc: dict) -> tuple[pd.Timestamp, pd.Timestamp | None]:
        sched = doc["state"]["schedule"]
        start = min(pd.Timestamp(sched["session_open"]), pd.Timestamp(sched["range_open"]))
        end: pd.Timestamp | None = None
        if doc.get("replay"):
            r_from, r_to = pd.Timestamp(doc["replay"]["from"]), pd.Timestamp(doc["replay"]["to"])
            start, end = max(start, r_from), r_to
        return start, end

    def _bar_count(start: pd.Timestamp, end: pd.Timestamp, tf_minutes: int,
                   anchored: bool, now: pd.Timestamp) -> int:
        # The account-cookie feed has no end anchor: it always returns the LAST
        # `count` bars. A replay of an older day must therefore ask for enough
        # bars to reach back from NOW to the replay start - sizing by the width
        # of the replay window returns today's bars, which the window filter
        # then drops, and the run reports NO_DATA on perfectly good history.
        reach_to = end if anchored else max(end, now)
        span = max((reach_to - start).total_seconds() / 60.0, tf_minutes)
        return int(min(settings.max_bars, max(50, math.ceil(span / tf_minutes) + _PAD_BARS)))

    def _advance(doc: dict, spec: SentinelSpec, now: pd.Timestamp) -> tuple[list[dict], list[str]]:
        warnings: list[str] = []
        tf = _resolve(spec)[1]
        start, end = _feed_window(doc)
        horizon = end if end is not None else now
        anchored = spec.provider != "session"
        count = _bar_count(start, horizon, tf.minutes, anchored, now)
        df = None
        reason = None
        try:
            _, _, df = load(spec.symbol, spec.timeframe, count, spec.provider, end)
        except ToolError as exc:
            reason = str(exc)[:300]
            warnings.append(f"bar load failed: {reason}")
        if df is not None and len(df):
            d = df.copy()
            d["time"] = pd.to_datetime(d["time"], utc=True)
            earliest = d["time"].min()
            d = d[d["time"] >= start]
            if end is not None:
                d = d[d["time"] <= end]
            if not len(d) and earliest > start:
                # Say which side of the gap we are on: an empty window because
                # the feed's history is too short is a different problem from a
                # feed that is down, and only one of them is worth retrying.
                reason = (
                    f"the feed's {spec.timeframe} history starts at {iso(earliest)}, "
                    f"after this run's window begins ({iso(start)}); "
                    f"asked for {count} bars (cap TV_MAX_BARS={settings.max_bars})"
                )
                warnings.append(reason)
            df = d
        events = machine.advance(
            spec, doc["state"], df,
            now=now, seq_start=doc["cursor"].get("last_seq", 0),
            timeframe_minutes=tf.minutes, replay=bool(doc.get("replay")),
            no_data_reason=reason,
        )
        doc["events"].extend(events)
        doc["cursor"]["last_seq"] = doc["cursor"].get("last_seq", 0) + len(events)
        doc["cursor"]["last_bar_ts"] = doc["state"].get("last_bar_ts")
        doc["cursor"]["bars_seen"] = doc["state"].get("bars_seen", 0)
        doc["cursor"]["last_poll"] = iso(now)
        if doc.get("replay"):
            doc["state"]["flags"]["replay_done"] = True
            doc["status"] = "finished"
        return events, warnings

    @mcp.tool(tags={"sentinel"}, annotations={"readOnlyHint": True, "openWorldHint": False})
    def tv_sentinel_status(
        run_id: Annotated[str | None, Field(description="A run id; omit to list every run")] = None,
    ) -> dict:
        """One sentinel run's state, or the list of runs in the sentinel directory.

        Read-only: it never loads bars and never advances the machine (use
        tv_sentinel_poll for that). With `run_id` it returns the same `state`
        object a poll returns; without it, one summary row per run file.
        """
        if run_id is None:
            runs = store.list_runs(directory)
            return {"sentinel_dir": str(directory), "count": len(runs), "runs": runs}
        try:
            doc, _spec = store.load(directory, run_id)
        except store.StoreError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "sentinel_dir": str(directory),
            "run_id": doc.get("run_id", run_id),
            "spec_echo": doc["spec"],
            "state": _public_state(doc),
            "events_stored": len(doc["events"]),
        }

    if settings.read_only:
        return  # start/poll/stop write run files - excluded under TV_READ_ONLY

    @mcp.tool(tags={"sentinel"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_sentinel_start(
        spec: Annotated[dict, Field(description=(
            "Watch definition: {symbol, timeframe, provider?, session: {name} or "
            "{start, end, tz}, range: {source: session_open|window, minutes, start?, adr?}, "
            "levels: [{name, price} | {name, high, low}], watch: {breaks, break_range, "
            "break_levels, retest, retest_after, retest_zone: {ticks|percent|fraction_of_range}, "
            "sweep, sweep_levels, levels, stale_bars, tick_size}, clocks: [{name, at, tz}], "
            "confirm: {mode: closes|distance|pullback, closes|ticks|percent}, day?}"))],
        run_id: Annotated[str | None, Field(description=(
            "Your own id for the run (letters, digits, . _ -); omitted = generated. "
            "Reusing an existing id is refused, never overwritten"))] = None,
        replay: Annotated[dict | None, Field(description=(
            "{from, to} ISO-8601 UTC: run the same machine over historical bars "
            "instead of live ones, to verify a past day"))] = None,
    ) -> dict:
        """Create a sentinel run watching one session and return its id and initial state.

        Nothing runs in the background: this only resolves the timetable (session
        window, range window, clock marks) and writes the run file. Call
        tv_sentinel_poll to advance it. With `replay`, the same machine walks the
        historical window deterministically - one poll replays the whole window.

        Events the run can emit: SESSION_OPEN, SESSION_CLOSE, RANGE_OPEN,
        RANGE_CLOSED, BREAK, BREAK_CONFIRMED, RETEST, SWEEP, LEVEL_TAGGED, CLOCK,
        STALE_DATA, NO_DATA. They are observations, never recommendations.
        """
        parsed = _parse_spec(spec)
        rep = _parse_replay(replay)
        sym, tf = _resolve(parsed)
        if parsed.day:
            day = date.fromisoformat(parsed.day)
        elif rep:
            day = rep.window()[0].date()
        else:
            day = datetime.now(tz=timezone.utc).date()
        try:
            schedule = schedule_for(parsed, day)
        except ValueError as exc:
            raise ToolError(f"Invalid spec: {exc}") from exc

        try:
            rid = (store.validate_run_id(run_id) if run_id is not None
                   else _default_run_id(parsed, day))
        except store.StoreError as exc:
            raise ToolError(str(exc)) from exc
        try:
            if store.exists(directory, rid):
                existing, _ = store.load(directory, rid)
                raise ToolError(
                    f"Sentinel run {rid!r} already exists (status "
                    f"{existing.get('status', 'running')}, {len(existing['events'])} events, "
                    f"updated {existing.get('updated')}). Pick another run_id - a start never "
                    f"clobbers a run."
                )
        except store.StoreError as exc:
            raise ToolError(str(exc)) from exc

        doc = {
            "version": store.VERSION,
            "run_id": rid,
            "status": "running",
            "spec": parsed.model_dump(mode="json", by_alias=True),
            "replay": ({"from": iso(rep.window()[0]), "to": iso(rep.window()[1])} if rep else None),
            "state": machine.initial_state(schedule),
            "events": [],
            "cursor": {"last_seq": 0, "last_bar_ts": None, "bars_seen": 0, "last_poll": None},
            "created": store.now_iso(),
            "updated": store.now_iso(),
        }
        store.save(directory, rid, doc)
        return {
            "run_id": rid,
            "sentinel_dir": str(directory),
            "symbol": sym.canonical,
            "tv_symbol": sym.tv,
            "timeframe": tf.canonical,
            "provider": parsed.provider,
            "spec_echo": doc["spec"],
            "state": _public_state(doc),
            "event_types": list(machine.EVENT_TYPES),
            "next_poll_after_s": _next_poll_after_s(
                doc["state"], tf.minutes, pd.Timestamp.now(tz="UTC"), bool(rep), False
            ),
        }

    @mcp.tool(tags={"sentinel"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_sentinel_poll(
        run_id: Annotated[str, Field(description="The run to advance")],
        since_seq: Annotated[int, Field(ge=0, description=(
            "Return events with seq > this; 0 = from the beginning. Polling twice "
            "with the same value returns the same events"))] = 0,
        max_events: Annotated[int, Field(ge=1, le=MAX_EVENTS, description="Cap on returned events")] = 100,
    ) -> dict:
        """Advance a sentinel run with the bars that appeared since the last poll.

        Loads bars through the normal loaders (free feeds, or the opt-in session
        feed when the spec asks), feeds only the bars newer than the cursor to the
        machine, appends whatever events they produce, and returns the ones newer
        than `since_seq`. Idempotent: an event's `seq` and payload never change, and
        already-processed bars are never replayed. `next_poll_after_s` is a hint
        from the timeframe and what the machine is waiting for - not a guarantee
        that something will have happened by then.
        """
        try:
            doc, spec = store.load(directory, run_id)
        except store.StoreError as exc:
            raise ToolError(str(exc)) from exc

        now = pd.Timestamp.now(tz="UTC")
        warnings: list[str] = []
        stopped = doc.get("status") == "stopped"
        replay_done = bool(doc["state"].get("flags", {}).get("replay_done"))
        if stopped:
            warnings.append("run is stopped; no bars were loaded (stored events only)")
        elif replay_done:
            warnings.append("replay already completed; stored events only")
        else:
            _, load_warnings = _advance(doc, spec, now)
            warnings.extend(load_warnings)
            store.save(directory, run_id, doc)

        events = [e for e in doc["events"] if e["seq"] > since_seq]
        truncated = len(events) > max_events
        tf = _resolve(spec)[1]
        return {
            "run_id": doc.get("run_id", run_id),
            "since_seq": since_seq,
            "events": events[:max_events],
            "returned_count": min(len(events), max_events),
            "pending_count": len(events),
            "truncated": truncated,
            "last_seq": doc["cursor"].get("last_seq", 0),
            "state": _public_state(doc),
            "next_poll_after_s": _next_poll_after_s(
                doc["state"], tf.minutes, now, bool(doc.get("replay")), stopped
            ),
            "warnings": warnings,
        }

    @mcp.tool(tags={"sentinel"}, annotations={"readOnlyHint": False, "openWorldHint": False})
    def tv_sentinel_stop(
        run_id: Annotated[str, Field(description="The run to close")],
    ) -> dict:
        """Mark a sentinel run finished. The run file stays on disk for the journal.

        A stopped run still answers tv_sentinel_status and tv_sentinel_poll (stored
        events), but loads no more bars and produces no more events. Stopping is
        idempotent.
        """
        try:
            doc, _spec = store.load(directory, run_id)
        except store.StoreError as exc:
            raise ToolError(str(exc)) from exc
        already = doc.get("status") == "stopped"
        doc["status"] = "stopped"
        doc["state"]["stopped"] = True
        doc["stopped_at"] = doc.get("stopped_at") or store.now_iso()
        store.save(directory, run_id, doc)
        return {
            "run_id": doc.get("run_id", run_id),
            "stopped": True,
            "already_stopped": already,
            "events_stored": len(doc["events"]),
            "run_file": str(store.run_path(directory, run_id)),
            "state": _public_state(doc),
        }
