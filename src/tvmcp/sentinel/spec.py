"""The sentinel watch spec: a fully explicit, strategy-free description of what to watch.

Nothing here knows about any instrument, session or trading school. A spec names
the symbol/timeframe, ONE session occurrence, the range window carved out of it,
the named levels to watch, which mechanics to report (`watch`), what counts as a
confirmed break (`confirm`), and the wall-clock marks to announce (`clocks`).

Time handling:
- `session.name` resolves against the fixed-UTC table `SESSION_WINDOWS_UTC` shared
  with `tv_scan_sessions` / `parse_since` - **not DST-aware**, by design of the
  pinned library.
- `session.start`/`end` (HH:MM) are interpreted in `session.tz` (IANA name) for the
  run's day, which IS DST-aware because it goes through `zoneinfo`.
- A session whose end is at or before its start spans midnight (end moves a day on).

Specs arrive as JSON from a caller and are re-validated on every load from disk:
`extra="forbid"` everywhere, no eval, no code paths driven by spec strings.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..scan.detectors import SESSION_WINDOWS_UTC

MAX_LEVELS = 50
MAX_CLOCKS = 20


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def _hhmm(value: str, what: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except (ValueError, AttributeError):
        raise ValueError(f"{what} must be HH:MM (24h), got {value!r}") from None
    return parsed.hour, parsed.minute


def _at(day: date, hhmm: tuple[int, int], tz: ZoneInfo) -> pd.Timestamp:
    """HH:MM on `day` in `tz` -> UTC Timestamp (DST-aware through zoneinfo)."""
    local = datetime(day.year, day.month, day.day, hhmm[0], hhmm[1], tzinfo=tz)
    return pd.Timestamp(local).tz_convert("UTC")


class SessionSpec(BaseModel):
    """Either a name from the fixed-UTC table, or explicit start/end in a timezone."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        description="A key of the fixed-UTC session table (Tokyo, London, New York, ...)",
    )
    start: str | None = Field(default=None, description="HH:MM in `tz`")
    end: str | None = Field(default=None, description="HH:MM in `tz`")
    tz: str = Field(default="UTC", description="IANA timezone for start/end")

    @model_validator(mode="after")
    def _check(self) -> SessionSpec:
        if self.name:
            key = next(
                (k for k in SESSION_WINDOWS_UTC if k.lower() == self.name.strip().lower()),
                None,
            )
            if key is None:
                raise ValueError(
                    f"unknown session {self.name!r}; use one of "
                    f"{sorted(SESSION_WINDOWS_UTC)} or give start/end/tz"
                )
            object.__setattr__(self, "name", key)
        elif self.start and self.end:
            _hhmm(self.start, "session.start")
            _hhmm(self.end, "session.end")
            _tz(self.tz)
        else:
            raise ValueError("session needs either `name` or both `start` and `end`")
        return self

    def label(self) -> str:
        return self.name or f"{self.start}-{self.end} {self.tz}"

    def window(self, day: date) -> tuple[pd.Timestamp, pd.Timestamp]:
        """(open, close) in UTC for `day`; a close at/before the open rolls a day on."""
        if self.name:
            tz = ZoneInfo("UTC")
            start_s, end_s = SESSION_WINDOWS_UTC[self.name]
        else:
            tz = _tz(self.tz)
            start_s, end_s = self.start, self.end  # type: ignore[assignment]
        open_ts = _at(day, _hhmm(start_s, "session.start"), tz)
        close_ts = _at(day, _hhmm(end_s, "session.end"), tz)
        if close_ts <= open_ts:
            close_ts = _at(day + timedelta(days=1), _hhmm(end_s, "session.end"), tz)
        return open_ts, close_ts


class RangeSpec(BaseModel):
    """The window whose high/low becomes the range the machine watches breaks of."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["session_open", "window"] = "session_open"
    minutes: int = Field(default=30, ge=1, le=1440, description="Length of the range window")
    start: str | None = Field(
        default=None, description="HH:MM in the session's tz; required for source='window'"
    )
    adr: float | None = Field(
        default=None,
        gt=0,
        description="Optional reference range (e.g. an average daily range) so "
        "RANGE_CLOSED can report size_vs_adr; purely descriptive",
    )

    @model_validator(mode="after")
    def _check(self) -> RangeSpec:
        if self.source == "window":
            if not self.start:
                raise ValueError("range.source='window' needs range.start (HH:MM)")
            _hhmm(self.start, "range.start")
        return self


class LevelSpec(BaseModel):
    """A named price or zone. Same shape `tv_scan_check_levels` takes."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    price: float | None = None
    high: float | None = None
    low: float | None = None

    @model_validator(mode="after")
    def _check(self) -> LevelSpec:
        if self.price is not None:
            if not math.isfinite(self.price):
                raise ValueError(f"level {self.name!r}: price must be finite")
        elif self.high is not None and self.low is not None:
            if not (math.isfinite(self.high) and math.isfinite(self.low)):
                raise ValueError(f"level {self.name!r}: high/low must be finite")
            if self.low > self.high:
                object.__setattr__(self, "low", self.high)
                object.__setattr__(self, "high", self.low)
        else:
            raise ValueError(f"level {self.name!r} needs price, or both high and low")
        return self

    def bounds(self) -> tuple[float, float]:
        if self.price is not None:
            return float(self.price), float(self.price)
        return float(self.low), float(self.high)  # type: ignore[arg-type]

    def as_check_level(self) -> dict:
        if self.price is not None:
            return {"name": self.name, "price": float(self.price)}
        return {"name": self.name, "high": float(self.high), "low": float(self.low)}  # type: ignore[arg-type]


class ZoneSpec(BaseModel):
    """How wide a zone is, in one of three units. Used for retests."""

    model_config = ConfigDict(extra="forbid")

    ticks: float | None = Field(default=None, gt=0)
    percent: float | None = Field(default=None, gt=0, description="Percent of the boundary price")
    fraction_of_range: float | None = Field(
        default=None, gt=0, description="Fraction of the formed range's size"
    )

    @model_validator(mode="after")
    def _check(self) -> ZoneSpec:
        given = [x for x in (self.ticks, self.percent, self.fraction_of_range) if x is not None]
        if len(given) != 1:
            raise ValueError("give exactly one of ticks, percent, fraction_of_range")
        return self

    def width(self, price: float, range_size: float | None, tick_size: float) -> float:
        if self.ticks is not None:
            return self.ticks * tick_size
        if self.percent is not None:
            return abs(price) * self.percent / 100.0
        return (range_size or 0.0) * (self.fraction_of_range or 0.0)


class WatchSpec(BaseModel):
    """Which mechanics the machine reports. Everything defaults to on but sweeps/retests
    need a zone/level list to be meaningful, so they carry their own switches."""

    model_config = ConfigDict(extra="forbid")

    breaks: bool = Field(default=True, description="Emit BREAK / BREAK_CONFIRMED")
    break_levels: list[str] = Field(
        default_factory=list,
        description="Names from levels[] that are ALSO break boundaries, besides the range",
    )
    break_range: bool = Field(default=True, description="Watch range_high / range_low breaks")
    retest: bool = Field(default=False, description="Emit RETEST after a break")
    retest_after: Literal["break", "confirm"] = "confirm"
    retest_zone: ZoneSpec | None = None
    sweep: bool = Field(default=False, description="Emit SWEEP on named levels")
    sweep_levels: list[str] = Field(
        default_factory=list, description="Names to watch for sweeps; empty = every level"
    )
    sweep_once: bool = Field(default=True, description="At most one SWEEP per level+side")
    levels: bool = Field(default=True, description="Emit LEVEL_TAGGED on first touch")
    stale_bars: float = Field(
        default=2.0, gt=0, le=1000, description="Feed is stale after this many timeframes with no new bar"
    )
    tick_size: float = Field(default=0.01, gt=0, description="Price of one tick, for tick-denominated zones")

    @model_validator(mode="after")
    def _check(self) -> WatchSpec:
        if self.retest and self.retest_zone is None:
            raise ValueError("watch.retest needs watch.retest_zone")
        return self


class ConfirmSpec(BaseModel):
    """What turns a BREAK into a BREAK_CONFIRMED. One of three modes, spelled out."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["closes", "distance", "pullback"] = "closes"
    closes: int = Field(default=2, ge=1, le=50, description="mode='closes': consecutive closes beyond")
    ticks: float | None = Field(default=None, gt=0, description="mode='distance': close beyond by N ticks")
    percent: float | None = Field(
        default=None, gt=0, description="mode='distance': close beyond by percent of the boundary price"
    )

    @model_validator(mode="after")
    def _check(self) -> ConfirmSpec:
        if self.mode == "distance":
            given = [x for x in (self.ticks, self.percent) if x is not None]
            if len(given) != 1:
                raise ValueError("confirm.mode='distance' needs exactly one of ticks / percent")
        return self

    def distance(self, price: float, tick_size: float) -> float:
        if self.ticks is not None:
            return self.ticks * tick_size
        return abs(price) * (self.percent or 0.0) / 100.0


class ClockSpec(BaseModel):
    """A wall-clock mark to announce once, with no meaning attached to it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    at: str = Field(description="HH:MM")
    tz: str = "UTC"

    @model_validator(mode="after")
    def _check(self) -> ClockSpec:
        _hhmm(self.at, "clock.at")
        _tz(self.tz)
        return self

    def when(self, day: date, session_open: pd.Timestamp, session_close: pd.Timestamp) -> pd.Timestamp:
        ts = _at(day, _hhmm(self.at, "clock.at"), _tz(self.tz))
        # an overnight session: a mark before the open belongs to the following day
        if session_close.date() > session_open.date() and ts < session_open:
            ts = _at(day + timedelta(days=1), _hhmm(self.at, "clock.at"), _tz(self.tz))
        return ts


class ReplaySpec(BaseModel):
    """Historical window to run the same machine over instead of live bars."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", description="ISO-8601 UTC start")
    to: str = Field(description="ISO-8601 UTC end")

    @model_validator(mode="after")
    def _check(self) -> ReplaySpec:
        if self.window()[0] >= self.window()[1]:
            raise ValueError("replay.from must be before replay.to")
        return self

    def window(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        from ..scan.levels import to_utc

        return to_utc(self.from_), to_utc(self.to)


class SentinelSpec(BaseModel):
    """The whole watch definition. Nothing implicit, nothing strategy-shaped."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=40)
    timeframe: str = Field(default="M5", max_length=8)
    provider: str = Field(
        default="auto", description="auto | dukascopy | oanda | session (opt-in TV cookie feed)"
    )
    session: SessionSpec
    range: RangeSpec = Field(default_factory=RangeSpec)
    levels: list[LevelSpec] = Field(default_factory=list, max_length=MAX_LEVELS)
    watch: WatchSpec = Field(default_factory=WatchSpec)
    clocks: list[ClockSpec] = Field(default_factory=list, max_length=MAX_CLOCKS)
    confirm: ConfirmSpec = Field(default_factory=ConfirmSpec)
    day: str | None = Field(
        default=None, description="YYYY-MM-DD the session belongs to; default = derived at start"
    )

    @model_validator(mode="after")
    def _check(self) -> SentinelSpec:
        if self.provider not in ("auto", "dukascopy", "oanda", "session"):
            raise ValueError(
                f"unknown provider {self.provider!r}; use auto, dukascopy, oanda or session"
            )
        names = [lv.name for lv in self.levels]
        if len(set(names)) != len(names):
            raise ValueError("level names must be unique")
        for n in self.watch.break_levels:
            if n not in names:
                raise ValueError(f"watch.break_levels names {n!r}, which is not in levels[]")
        for n in self.watch.sweep_levels:
            if n not in names:
                raise ValueError(f"watch.sweep_levels names {n!r}, which is not in levels[]")
        clock_names = [c.name for c in self.clocks]
        if len(set(clock_names)) != len(clock_names):
            raise ValueError("clock names must be unique")
        if self.day:
            try:
                date.fromisoformat(self.day)
            except ValueError:
                raise ValueError(f"day must be YYYY-MM-DD, got {self.day!r}") from None
        return self

    def level(self, name: str) -> LevelSpec | None:
        return next((lv for lv in self.levels if lv.name == name), None)

    def sweep_names(self) -> list[str]:
        if not self.watch.sweep:
            return []
        return self.watch.sweep_levels or [lv.name for lv in self.levels]


def schedule_for(spec: SentinelSpec, day: date) -> dict:
    """Resolve the run's fixed timetable: session, range window and clocks, all UTC."""
    session_open, session_close = spec.session.window(day)
    if spec.range.source == "session_open":
        range_open = session_open
    else:
        tz = ZoneInfo("UTC") if spec.session.name else _tz(spec.session.tz)
        range_open = _at(day, _hhmm(spec.range.start or "00:00", "range.start"), tz)
        if range_open < session_open:
            range_open = _at(
                day + timedelta(days=1), _hhmm(spec.range.start or "00:00", "range.start"), tz
            )
    range_close = range_open + pd.Timedelta(minutes=spec.range.minutes)
    return {
        "day": day.isoformat(),
        "session": spec.session.label(),
        "session_open": iso(session_open),
        "session_close": iso(session_close),
        "range_open": iso(range_open),
        "range_close": iso(range_close),
        "clocks": [
            {"name": c.name, "at": iso(c.when(day, session_open, session_close))}
            for c in spec.clocks
        ],
    }


def iso(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").isoformat().replace("+00:00", "Z")
