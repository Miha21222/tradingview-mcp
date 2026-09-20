"""Session levels: the price set a session trader marks before an open. (M9)

Pure pandas/python, feed-agnostic **and market-agnostic**: nothing here knows
which instrument, which session or whose day plan the caller has in mind. Every
session, anchor, multiple and lookback arrives as an argument.

What it computes, all from one bars DataFrame (columns time/open/high/low/close/
volume, `time` tz-aware UTC):

- previous day high/low/close, previous week high/low, previous month high/low
  (calendar periods in the caller's `tz`, so "previous day" is the previous day
  that actually traded);
- per named session window: high / low / open / close;
- "midnight open" style anchors: the open of the first bar at or after a named
  wall-clock time in a named IANA timezone;
- ADR(n): the average range of the last n completed periods (calendar day, or a
  named session), plus projections from a chosen anchor at chosen multiples;
- an opening-range / initial-balance block: the high/low of the first N minutes
  of a named session, its size, that size as a share of ADR, and extensions at
  configurable multiples above and below;
- gaps: one occurrence of a session closing into the next one opening, with size.

Sessions may be named from the fixed-UTC table `SESSION_WINDOWS_UTC` (the same
table `tv_scan_sessions` and `scan/levels.parse_since` use - **not DST-aware**),
or given explicitly as `{name, start, end, tz, day_offset}`. An explicit window
in an IANA zone IS DST-correct: it is built from local wall time on the day.

Judgment lives elsewhere. This module never classifies a range as wide/narrow,
never says a level "should" hold and never refuses a day. Where a requested
session has no bars in the frame it says so in `warnings` instead of inventing
a number.
"""

from __future__ import annotations

from datetime import date as _date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from fastmcp.exceptions import ToolError

from .detectors import SESSION_WINDOWS_UTC

__all__ = [
    "SESSION_WINDOWS_UTC",
    "DEFAULT_EXTENSIONS",
    "DEFAULT_ADR_MULTIPLES",
    "INCLUDE_KEYS",
    "normalize_sessions",
    "normalize_anchors",
    "session_bounds",
    "parse_day",
    "compute_levels",
]

DEFAULT_EXTENSIONS = (0.5, 1.0, 1.5, 2.0)
DEFAULT_ADR_MULTIPLES = (0.5, 1.0)
INCLUDE_KEYS = (
    "prev_day", "prev_week", "prev_month",
    "sessions", "anchors", "adr", "opening_range", "gaps",
)
MAX_SESSIONS = 8
MAX_ANCHORS = 8
MAX_MULTIPLES = 8
MAX_DAY_OFFSET = 7
LOOKBACK_DAYS = 200  # hard cap when walking back for ADR periods
GAP_LOOKBACK_DAYS = 14


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _iso(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").isoformat().replace("+00:00", "Z")


def _r(x) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    return round(float(x), 6)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except Exception:  # noqa: BLE001 - any tz-database failure has the same answer
        raise ToolError(
            f"Unknown timezone {name!r}; use an IANA name such as UTC, "
            "America/New_York, Europe/London, Asia/Tokyo"
        ) from None


def _hhmm(value, what: str) -> tuple[int, int]:
    s = str(value).strip()
    try:
        h, m = s.split(":")
        hh, mm = int(h), int(m)
    except (ValueError, AttributeError):
        raise ToolError(f"{what} must be HH:MM (24h), got {value!r}") from None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ToolError(f"{what} must be HH:MM (24h), got {value!r}")
    return hh, mm


def _mult_key(m: float) -> str:
    return f"{float(m):g}"


def _multiples(values, default, what: str) -> tuple[float, ...]:
    if values is None:
        return tuple(default)
    if not isinstance(values, (list, tuple)):
        raise ToolError(f"{what} must be a list of positive numbers")
    if len(values) > MAX_MULTIPLES:
        raise ToolError(f"At most {MAX_MULTIPLES} {what} (got {len(values)})")
    out = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ToolError(f"{what} must be numbers, got {v!r}") from None
        if not (0 < f <= 20):
            raise ToolError(f"{what} must be > 0 and <= 20, got {v!r}")
        out.append(f)
    return tuple(sorted(set(out))) or tuple(default)


def parse_day(value) -> _date | None:
    """`None`/`""` -> None; `YYYY-MM-DD` (or a date/datetime) -> date."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    try:
        return _date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ToolError(f"Bad date {value!r}; use YYYY-MM-DD") from None


def normalize_sessions(sessions) -> list[dict]:
    """`["London", {"name": "RTH", "start": "09:30", "end": "16:00", "tz": ...}]` -> specs.

    A string must be a key of SESSION_WINDOWS_UTC (case-insensitive, fixed UTC
    hours, not DST-aware). A dict needs `start`/`end` as HH:MM and may carry
    `tz` (IANA, default UTC) and `day_offset` (whole days added to the session
    date, e.g. -1 for "yesterday's New York").
    """
    if sessions is None:
        return []
    if isinstance(sessions, (str, dict)):
        sessions = [sessions]
    if not isinstance(sessions, list):
        raise ToolError("sessions must be a list of names or {name,start,end,tz} objects")
    if len(sessions) > MAX_SESSIONS:
        raise ToolError(f"At most {MAX_SESSIONS} sessions per call (got {len(sessions)})")
    out: list[dict] = []
    seen: set[str] = set()
    for i, s in enumerate(sessions):
        if isinstance(s, str):
            key = next((k for k in SESSION_WINDOWS_UTC if k.lower() == s.strip().lower()), None)
            if key is None:
                raise ToolError(
                    f"Unknown session {s!r}; use one of {sorted(SESSION_WINDOWS_UTC)} "
                    "or an explicit {name,start,end,tz} window"
                )
            start, end = SESSION_WINDOWS_UTC[key]
            spec = {
                "name": key, "start": start, "end": end, "tz": "UTC", "day_offset": 0,
                "source": "SESSION_WINDOWS_UTC (fixed UTC hours, not DST-aware)",
            }
        elif isinstance(s, dict):
            name = str(s.get("name") or f"session_{i + 1}")[:40]
            if s.get("start") is None or s.get("end") is None:
                raise ToolError(f"sessions[{i}] needs start and end as HH:MM")
            start = str(s["start"]).strip()
            end = str(s["end"]).strip()
            _hhmm(start, f"sessions[{i}].start")
            _hhmm(end, f"sessions[{i}].end")
            tzname = str(s.get("tz") or "UTC")
            _zone(tzname)
            try:
                offset = int(s.get("day_offset") or 0)
            except (TypeError, ValueError):
                raise ToolError(
                    f"sessions[{i}].day_offset must be a whole number of days"
                ) from None
            if abs(offset) > MAX_DAY_OFFSET:
                raise ToolError(
                    f"sessions[{i}].day_offset must be within +/-{MAX_DAY_OFFSET} days"
                )
            spec = {
                "name": name, "start": start, "end": end, "tz": tzname,
                "day_offset": offset,
                "source": f"explicit window {start}-{end} {tzname}"
                          + (f" day_offset {offset:+d}" if offset else ""),
            }
        else:
            raise ToolError(f"sessions[{i}] must be a name or an object")
        if spec["name"] in seen:
            raise ToolError(f"duplicate session name {spec['name']!r}")
        seen.add(spec["name"])
        out.append(spec)
    return out


def normalize_anchors(anchors) -> list[dict]:
    """`[{"name": "midnight open", "time": "00:00", "tz": "America/New_York"}]` -> specs."""
    if anchors is None:
        return []
    if isinstance(anchors, dict):
        anchors = [anchors]
    if not isinstance(anchors, list):
        raise ToolError("anchors must be a list of {name, time, tz, day_offset} objects")
    if len(anchors) > MAX_ANCHORS:
        raise ToolError(f"At most {MAX_ANCHORS} anchors per call (got {len(anchors)})")
    out = []
    for i, a in enumerate(anchors):
        if not isinstance(a, dict):
            raise ToolError(f"anchors[{i}] must be an object {{name, time, tz}}")
        if a.get("time") is None:
            raise ToolError(f"anchors[{i}] needs time as HH:MM")
        t = str(a["time"]).strip()
        _hhmm(t, f"anchors[{i}].time")
        tzname = str(a.get("tz") or "UTC")
        _zone(tzname)
        try:
            offset = int(a.get("day_offset") or 0)
        except (TypeError, ValueError):
            raise ToolError(f"anchors[{i}].day_offset must be a whole number of days") from None
        if abs(offset) > MAX_DAY_OFFSET:
            raise ToolError(f"anchors[{i}].day_offset must be within +/-{MAX_DAY_OFFSET} days")
        out.append({
            "name": str(a.get("name") or f"{t} {tzname} open")[:40],
            "time": t, "tz": tzname, "day_offset": offset,
        })
    return out


def session_bounds(spec: dict, day: _date) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Absolute UTC [start, end) of `spec` on the session date `day`.

    A session is labelled by the date it STARTS on; a window whose end is at or
    before its start wraps into the next calendar day (Sydney 21:00-06:00).
    Built from local wall time, so an explicit IANA zone is DST-correct.
    """
    tz = _zone(spec["tz"])
    d = day + timedelta(days=int(spec.get("day_offset") or 0))
    sh, sm = _hhmm(spec["start"], "session start")
    eh, em = _hhmm(spec["end"], "session end")
    d_end = d if (eh, em) > (sh, sm) else d + timedelta(days=1)
    start = pd.Timestamp(
        datetime(d.year, d.month, d.day, sh, sm, tzinfo=tz)
    ).tz_convert("UTC")
    end = pd.Timestamp(
        datetime(d_end.year, d_end.month, d_end.day, eh, em, tzinfo=tz)
    ).tz_convert("UTC")
    return start, end


def _slice(d: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return d[(d["time"] >= start) & (d["time"] < end)]


def _ohlc(sl: pd.DataFrame) -> dict | None:
    if sl is None or sl.empty:
        return None
    return {
        "open": _r(sl["open"].iloc[0]),
        "high": _r(sl["high"].max()),
        "low": _r(sl["low"].min()),
        "close": _r(sl["close"].iloc[-1]),
        "bars": int(len(sl)),
        "first_bar_time": _iso(sl["time"].iloc[0]),
        "last_bar_time": _iso(sl["time"].iloc[-1]),
    }


def _infer_tf_minutes(times: pd.Series) -> int:
    if len(times) < 2:
        return 1
    d = times.diff().dropna().dt.total_seconds()
    if not len(d):
        return 1
    return max(1, int(round(float(d.median()) / 60)))


def _covers(sl: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, tf_min: int) -> bool:
    """True when the slice spans the whole window (within one bar at each edge)."""
    if sl.empty:
        return False
    tol = pd.Timedelta(minutes=tf_min)
    return (sl["time"].iloc[0] <= start + tol) and (sl["time"].iloc[-1] + tol >= end - tol)


# --------------------------------------------------------------------------- #
# calendar period groups (previous day / week / month)
# --------------------------------------------------------------------------- #
def _period_key_series(local: pd.Series, kind: str) -> pd.Series:
    if kind == "day":
        return local.dt.strftime("%Y-%m-%d")
    if kind == "month":
        return local.dt.strftime("%Y-%m")
    iso = local.dt.isocalendar()
    return (
        iso["year"].astype(int).astype(str)
        + "-W"
        + iso["week"].astype(int).astype(str).str.zfill(2)
    )


def _period_key_of(day: _date, kind: str) -> str:
    if kind == "day":
        return day.isoformat()
    if kind == "month":
        return f"{day.year:04d}-{day.month:02d}"
    iso = day.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def _groups(d: pd.DataFrame, kind: str) -> list[dict]:
    keys = _period_key_series(d["_local"], kind)
    g = d.assign(_k=keys.values)
    rows = []
    for k, sub in g.groupby("_k", sort=True):
        row = _ohlc(sub)
        row["label"] = str(k)
        rows.append(row)
    return rows


def _previous_group(rows: list[dict], day: _date, kind: str) -> tuple[dict | None, bool]:
    """Last group strictly before `day`'s own group; flag = it is the oldest loaded."""
    key = _period_key_of(day, kind)
    before = [r for r in rows if r["label"] < key]
    if not before:
        return None, False
    return before[-1], before[-1] is rows[0]


# --------------------------------------------------------------------------- #
# the main computation
# --------------------------------------------------------------------------- #
def compute_levels(
    df: pd.DataFrame,
    *,
    day=None,
    sessions=None,
    include=None,
    anchors=None,
    adr_days: int = 5,
    adr_session: str = "",
    adr_anchor: str = "auto",
    adr_multiples=None,
    opening_range_session: str = "",
    opening_range_minutes: int = 60,
    extensions=None,
    tz: str = "UTC",
    timeframe_minutes: int | None = None,
) -> dict:
    """Level set for `day` over `df`. See the module docstring for the contract."""
    if df is None or len(df) == 0:
        raise ToolError("no bars loaded - cannot compute levels")
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], utc=True)
    d = d.sort_values("time").reset_index(drop=True)
    zone = _zone(tz)
    d["_local"] = d["time"].dt.tz_convert(zone)
    tf = int(timeframe_minutes or _infer_tf_minutes(d["time"]))

    specs = normalize_sessions(sessions)
    anchor_specs = normalize_anchors(anchors)
    inc = _include(include)
    ext_mult = _multiples(extensions, DEFAULT_EXTENSIONS, "extensions")
    adr_mult = _multiples(adr_multiples, DEFAULT_ADR_MULTIPLES, "adr_multiples")
    if not isinstance(adr_days, int) or isinstance(adr_days, bool) or not (1 <= adr_days <= 60):
        raise ToolError("adr_days must be a whole number 1..60")
    if (not isinstance(opening_range_minutes, int) or isinstance(opening_range_minutes, bool)
            or not (1 <= opening_range_minutes <= 1440)):
        raise ToolError("opening_range_minutes must be a whole number 1..1440")

    the_day = parse_day(day) or d["_local"].iloc[-1].date()
    warnings: list[str] = []
    levels: list[dict] = []

    first_bar, last_bar = d["time"].iloc[0], d["time"].iloc[-1]
    day_start_utc = pd.Timestamp(
        datetime(the_day.year, the_day.month, the_day.day, tzinfo=zone)
    ).tz_convert("UTC")
    if first_bar > day_start_utc:
        warnings.append(
            f"history starts at {_iso(first_bar)}, after {the_day.isoformat()} 00:00 {tz} - "
            "earlier periods are missing; raise `count` to cover them"
        )
    if last_bar < day_start_utc:
        warnings.append(
            f"last loaded bar {_iso(last_bar)} is before {the_day.isoformat()} 00:00 {tz} - "
            f"nothing of {the_day.isoformat()} is in the frame"
        )

    # ---- previous calendar periods ---------------------------------------- #
    period_groups: dict = {}
    for kind, wanted in (("day", "prev_day" in inc),
                         ("week", "prev_week" in inc),
                         ("month", "prev_month" in inc)):
        if not wanted:
            continue
        rows = period_groups.setdefault(kind, _groups(d, kind))
        prev, oldest = _previous_group(rows, the_day, kind)
        if prev is None:
            warnings.append(
                f"no complete previous {kind} before {the_day.isoformat()} in the loaded bars"
            )
            continue
        note = "oldest loaded period - may be truncated by the load window" if oldest else None
        src = f"previous {kind} {prev['label']} ({tz}, {prev['bars']} bars)"
        label = {"day": "Previous day", "week": "Previous week", "month": "Previous month"}[kind]
        levels.append(_level(f"{label} high", prev["high"], src, prev["last_bar_time"], note))
        levels.append(_level(f"{label} low", prev["low"], src, prev["last_bar_time"], note))
        if kind == "day":
            levels.append(_level("Previous day close", prev["close"], src,
                                 prev["last_bar_time"], note))
        period_groups[f"prev_{kind}"] = prev

    # ---- sessions ---------------------------------------------------------- #
    session_out: list[dict] = []
    session_ohlc: dict[str, dict] = {}
    for spec in specs:
        start, end = session_bounds(spec, the_day)
        sl = _slice(d, start, end)
        block = {
            "name": spec["name"], "start": spec["start"], "end": spec["end"],
            "tz": spec["tz"], "day_offset": spec["day_offset"],
            "window_utc": [_iso(start), _iso(end)], "source": spec["source"],
        }
        o = _ohlc(sl)
        if o is None:
            block.update(bars=0, high=None, low=None, open=None, close=None, complete=False)
            warnings.append(
                f"session {spec['name']!r} ({spec['start']}-{spec['end']} {spec['tz']}) "
                f"has no bars in {_iso(start)}..{_iso(end)} - no levels emitted for it"
            )
        else:
            block.update(o)
            block["complete"] = _covers(sl, start, end, tf)
            session_ohlc[spec["name"]] = {**o, "start": start, "end": end}
            if "sessions" in inc:
                src = f"{spec['name']} session {_iso(start)}..{_iso(end)}"
                note = None if block["complete"] else "session not fully covered by the loaded bars"
                levels.append(_level(f"{spec['name']} high", o["high"], src,
                                     o["last_bar_time"], note))
                levels.append(_level(f"{spec['name']} low", o["low"], src,
                                     o["last_bar_time"], note))
                levels.append(_level(f"{spec['name']} open", o["open"], src,
                                     o["first_bar_time"], note))
                levels.append(_level(f"{spec['name']} close", o["close"], src,
                                     o["last_bar_time"], note))
        session_out.append(block)

    # ---- anchors ----------------------------------------------------------- #
    if "anchors" in inc:
        for a in anchor_specs:
            az = _zone(a["tz"])
            ad = the_day + timedelta(days=a["day_offset"])
            ah, am = _hhmm(a["time"], "anchor time")
            at = pd.Timestamp(
                datetime(ad.year, ad.month, ad.day, ah, am, tzinfo=az)
            ).tz_convert("UTC")
            after = d[d["time"] >= at]
            if after.empty:
                warnings.append(
                    f"anchor {a['name']!r} at {_iso(at)}: no bar at or after it in the loaded bars"
                )
                continue
            row = after.iloc[0]
            offset_min = int(round((row["time"] - at).total_seconds() / 60))
            note = None
            if offset_min > max(tf, 1):
                note = (f"first bar is {offset_min}m after the anchor "
                        "(market closed or a gap in the feed)")
            levels.append(_level(
                a["name"], row["open"],
                f"open of the first bar at or after {a['time']} {a['tz']} on {ad.isoformat()}",
                _iso(row["time"]), note,
            ))

    # ---- ADR --------------------------------------------------------------- #
    adr = None
    if "adr" in inc:
        adr, adr_warn = _adr(d, the_day, adr_days, adr_session, specs, tf, tz, period_groups)
        warnings.extend(adr_warn)
        if adr and adr["value"] is not None:
            anchor, a_warn = _adr_anchor(adr_anchor, specs, session_ohlc,
                                         period_groups, d, the_day)
            warnings.extend(a_warn)
            adr["anchor"] = anchor
            if anchor and anchor["price"] is not None:
                for m in adr_mult:
                    up = anchor["price"] + m * adr["value"]
                    dn = anchor["price"] - m * adr["value"]
                    adr["projections"][f"up_{_mult_key(m)}"] = _r(up)
                    adr["projections"][f"down_{_mult_key(m)}"] = _r(dn)
                    src = (f"ADR({adr['n_used']}) {adr['value']} x{_mult_key(m)} "
                           f"from {anchor['name']} {anchor['price']}")
                    levels.append(_level(f"ADR +{_mult_key(m)}", up, src, None))
                    levels.append(_level(f"ADR -{_mult_key(m)}", dn, src, None))

    # ---- opening range / initial balance ----------------------------------- #
    opening_range = None
    if "opening_range" in inc and specs:
        or_spec = _pick_session(opening_range_session, specs, "opening_range_session")
        opening_range, or_warn = _opening_range(
            d, or_spec, the_day, opening_range_minutes, ext_mult,
            (adr or {}).get("value"), tf,
        )
        warnings.extend(or_warn)
        if opening_range:
            src = (f"first {opening_range_minutes}m of {or_spec['name']} "
                   f"({opening_range['window_utc'][0]}..{opening_range['window_utc'][1]})")
            note = None if opening_range["complete"] else "opening range still forming"
            levels.append(_zone_level(
                f"{or_spec['name']} opening range", opening_range["high"],
                opening_range["low"], src, opening_range["last_bar_time"], note))
            levels.append(_level(f"{or_spec['name']} OR high", opening_range["high"], src,
                                 opening_range["last_bar_time"], note))
            levels.append(_level(f"{or_spec['name']} OR low", opening_range["low"], src,
                                 opening_range["last_bar_time"], note))
            levels.append(_level(f"{or_spec['name']} OR mid", opening_range["mid"], src,
                                 opening_range["last_bar_time"], note))
            for k, v in opening_range["extensions"].items():
                direction, mult = k.split("_", 1)
                levels.append(_level(
                    f"{or_spec['name']} OR {k}", v,
                    f"{src}; OR size {opening_range['size']} x{mult} {direction}",
                    None, note,
                ))
    elif "opening_range" in inc and not specs:
        warnings.append("opening_range requested but no sessions given - nothing to open")

    # ---- gaps -------------------------------------------------------------- #
    gaps: list[dict] = []
    if "gaps" in inc:
        gaps, gap_warn, gap_levels = _gaps(d, specs, the_day, period_groups, tz)
        warnings.extend(gap_warn)
        levels.extend(gap_levels)

    return {
        "date": the_day.isoformat(),
        "tz": tz,
        "timeframe_minutes": tf,
        "bars_scanned": int(len(d)),
        "bars_range": [_iso(first_bar), _iso(last_bar)],
        "include": sorted(inc),
        "sessions": session_out,
        "levels": levels,
        "adr": adr,
        "opening_range": opening_range,
        "gaps": gaps,
        "warnings": warnings,
    }


def _include(include) -> set[str]:
    if include is None:
        return set(INCLUDE_KEYS)
    if isinstance(include, str):
        include = [include]
    if not isinstance(include, list) or not include:
        raise ToolError(f"include must be a non-empty list from {list(INCLUDE_KEYS)}")
    out = set()
    for k in include:
        key = str(k).strip().lower()
        if key == "all":
            out |= set(INCLUDE_KEYS)
            continue
        if key not in INCLUDE_KEYS:
            raise ToolError(f"Unknown include {k!r}; use any of {list(INCLUDE_KEYS)} or 'all'")
        out.add(key)
    return out


def _level(name: str, price, source: str, time_iso: str | None,
           note: str | None = None) -> dict:
    return {
        "name": name, "kind": "price", "price": _r(price),
        "source": source, "time": time_iso, "note": note,
    }


def _zone_level(name: str, high, low, source: str, time_iso: str | None,
                note: str | None = None) -> dict:
    hi, lo = _r(high), _r(low)
    if hi is not None and lo is not None and lo > hi:
        hi, lo = lo, hi
    return {
        "name": name, "kind": "zone", "high": hi, "low": lo,
        "source": source, "time": time_iso, "note": note,
    }


def _pick_session(name: str, specs: list[dict], what: str) -> dict:
    if not name:
        return specs[0]
    hit = next((s for s in specs if s["name"].lower() == str(name).strip().lower()), None)
    if hit is None:
        raise ToolError(
            f"{what}={name!r} is not one of the requested sessions "
            f"({[s['name'] for s in specs]})"
        )
    return hit


# --------------------------------------------------------------------------- #
# ADR
# --------------------------------------------------------------------------- #
def _adr(d, the_day, n, adr_session, specs, tf, tz, period_groups) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    if adr_session:
        if not specs:
            raise ToolError("adr_session given but no sessions were requested")
        spec = _pick_session(adr_session, specs, "adr_session")
        periods = _session_periods(d, spec, the_day, n, tf)
        basis = f"session:{spec['name']} ({spec['start']}-{spec['end']} {spec['tz']})"
    else:
        rows = period_groups.setdefault("day", _groups(d, "day"))
        key = _period_key_of(the_day, "day")
        before = [r for r in rows if r["label"] < key]
        if len(before) > n:
            # the oldest loaded day is usually cut short by the load window
            before = before[1:]
        periods = [
            {"label": r["label"], "high": r["high"], "low": r["low"],
             "range": _r(r["high"] - r["low"]), "bars": r["bars"]}
            for r in before[-n:]
        ]
        basis = f"calendar day ({tz})"
    if not periods:
        warnings.append(
            f"ADR: no completed periods before {the_day.isoformat()} in the loaded bars"
        )
        return ({"n_requested": n, "n_used": 0, "basis": basis, "value": None,
                 "periods": [], "anchor": None, "projections": {}}, warnings)
    if len(periods) < n:
        warnings.append(
            f"ADR: only {len(periods)} of {n} requested periods available in the loaded "
            "bars - raise `count` for a fuller average"
        )
    value = sum(p["range"] for p in periods) / len(periods)
    return ({
        "n_requested": n, "n_used": len(periods), "basis": basis,
        "value": _r(value), "periods": periods, "anchor": None, "projections": {},
    }, warnings)


def _session_periods(d, spec, the_day, n, tf) -> list[dict]:
    """The last n COMPLETE occurrences of `spec` strictly before `the_day`."""
    earliest = d["time"].iloc[0]
    got: list[dict] = []
    probe = the_day - timedelta(days=1)
    steps = 0
    while len(got) < n and steps < LOOKBACK_DAYS:
        start, end = session_bounds(spec, probe)
        if end < earliest:
            break
        sl = _slice(d, start, end)
        if not sl.empty and _covers(sl, start, end, tf):
            hi, lo = _r(sl["high"].max()), _r(sl["low"].min())
            got.append({"label": probe.isoformat(), "high": hi, "low": lo,
                        "range": _r(hi - lo), "bars": int(len(sl))})
        probe -= timedelta(days=1)
        steps += 1
    got.reverse()
    return got


def _adr_anchor(mode, specs, session_ohlc, period_groups, d,
                the_day) -> tuple[dict | None, list[str]]:
    """Resolve the price the ADR projections are measured from."""
    warnings: list[str] = []
    m = str(mode or "auto").strip().lower()
    if m == "none":
        return None, warnings
    try:
        price = float(m)
    except ValueError:
        price = None
    if price is not None:
        return ({"name": "explicit", "price": _r(price),
                 "source": f"caller-supplied anchor {m}"}, warnings)

    def _session_open():
        if not specs:
            return None
        o = session_ohlc.get(specs[0]["name"])
        if not o:
            return None
        return {"name": f"{specs[0]['name']} open", "price": o["open"],
                "source": f"open of the {specs[0]['name']} session on {the_day.isoformat()}"}

    def _prev_close():
        prev = period_groups.get("prev_day")
        if prev is None:
            rows = period_groups.setdefault("day", _groups(d, "day"))
            prev, _ = _previous_group(rows, the_day, "day")
        if prev is None:
            return None
        return {"name": "previous day close", "price": prev["close"],
                "source": f"close of {prev['label']}"}

    def _last_close():
        return {"name": "last close", "price": _r(d["close"].iloc[-1]),
                "source": f"close of the last loaded bar {_iso(d['time'].iloc[-1])}"}

    if m == "session_open":
        got = _session_open()
        if got is None:
            warnings.append(
                "adr_anchor=session_open: the session has no bars - projections skipped"
            )
        return got, warnings
    if m == "prev_day_close":
        got = _prev_close()
        if got is None:
            warnings.append("adr_anchor=prev_day_close: no previous day in the loaded bars")
        return got, warnings
    if m == "last_close":
        return _last_close(), warnings
    if m != "auto":
        raise ToolError(
            f"Unknown adr_anchor {mode!r}; use auto, session_open, prev_day_close, "
            "last_close, none, or a number"
        )
    for fn in (_session_open, _prev_close, _last_close):
        got = fn()
        if got is not None and got["price"] is not None:
            return got, warnings
    return None, warnings


# --------------------------------------------------------------------------- #
# opening range
# --------------------------------------------------------------------------- #
def _opening_range(d, spec, the_day, minutes, ext_mult, adr_value,
                   tf) -> tuple[dict | None, list[str]]:
    warnings: list[str] = []
    s_start, s_end = session_bounds(spec, the_day)
    end = min(s_start + pd.Timedelta(minutes=minutes), s_end)
    if end <= s_start:
        raise ToolError("opening_range_minutes resolves to an empty window")
    sl = _slice(d, s_start, end)
    if sl.empty:
        warnings.append(
            f"opening range: {spec['name']!r} has no bars in {_iso(s_start)}..{_iso(end)} "
            "- no opening-range levels emitted"
        )
        return None, warnings
    hi, lo = _r(sl["high"].max()), _r(sl["low"].min())
    size = _r(hi - lo)
    complete = _covers(sl, s_start, end, tf)
    out = {
        "session": spec["name"],
        "minutes": int(minutes),
        "window_utc": [_iso(s_start), _iso(end)],
        "high": hi, "low": lo, "mid": _r((hi + lo) / 2),
        "open": _r(sl["open"].iloc[0]), "close": _r(sl["close"].iloc[-1]),
        "size": size,
        "bars": int(len(sl)),
        "complete": complete,
        "first_bar_time": _iso(sl["time"].iloc[0]),
        "last_bar_time": _iso(sl["time"].iloc[-1]),
        "size_vs_adr": _r(size / adr_value) if adr_value else None,
        "extensions": {},
    }
    if not complete:
        warnings.append(
            f"opening range for {spec['name']!r} covers {out['bars']} bars up to "
            f"{out['last_bar_time']} - the {minutes}m window is not closed yet"
        )
    for m in ext_mult:
        out["extensions"][f"up_{_mult_key(m)}"] = _r(hi + m * size)
        out["extensions"][f"down_{_mult_key(m)}"] = _r(lo - m * size)
    return out, warnings


# --------------------------------------------------------------------------- #
# gaps
# --------------------------------------------------------------------------- #
def _gaps(d, specs, the_day, period_groups, tz) -> tuple[list[dict], list[str], list[dict]]:
    warnings: list[str] = []
    gaps: list[dict] = []
    levels: list[dict] = []

    for spec in specs:
        start, end = session_bounds(spec, the_day)
        cur = _slice(d, start, end)
        if cur.empty:
            continue  # the session block already warned about this
        prev = None
        probe = the_day - timedelta(days=1)
        for _ in range(GAP_LOOKBACK_DAYS):
            p_start, p_end = session_bounds(spec, probe)
            sl = _slice(d, p_start, p_end)
            if not sl.empty:
                prev = {"label": probe.isoformat(), "close": _r(sl["close"].iloc[-1]),
                        "time": _iso(sl["time"].iloc[-1])}
                break
            probe -= timedelta(days=1)
        if prev is None:
            warnings.append(
                f"gap for {spec['name']!r}: no earlier occurrence of the session within "
                f"{GAP_LOOKBACK_DAYS} days of loaded bars"
            )
            continue
        open_px = _r(cur["open"].iloc[0])
        size = _r(open_px - prev["close"])
        gaps.append({
            "name": f"{spec['name']} open gap",
            "from": {"label": prev["label"], "price": prev["close"], "time": prev["time"],
                     "what": f"{spec['name']} close"},
            "to": {"label": the_day.isoformat(), "price": open_px,
                   "time": _iso(cur["time"].iloc[0]), "what": f"{spec['name']} open"},
            "size": size,
            "direction": "up" if size > 0 else ("down" if size < 0 else "flat"),
        })
        levels.append(_level(
            f"{spec['name']} previous close", prev["close"],
            f"close of the {spec['name']} session on {prev['label']}", prev["time"],
        ))
        if size:
            levels.append(_zone_level(
                f"{spec['name']} open gap", open_px, prev["close"],
                f"gap between the {spec['name']} close on {prev['label']} and its open "
                f"on {the_day.isoformat()} (size {size})",
                _iso(cur["time"].iloc[0]),
            ))

    rows = period_groups.setdefault("day", _groups(d, "day"))
    prev_day, _ = _previous_group(rows, the_day, "day")
    cur_day = next((r for r in rows if r["label"] == the_day.isoformat()), None)
    if prev_day is not None and cur_day is not None:
        size = _r(cur_day["open"] - prev_day["close"])
        gaps.append({
            "name": "day open gap",
            "from": {"label": prev_day["label"], "price": prev_day["close"],
                     "time": prev_day["last_bar_time"], "what": f"day close ({tz})"},
            "to": {"label": cur_day["label"], "price": cur_day["open"],
                   "time": cur_day["first_bar_time"], "what": f"day open ({tz})"},
            "size": size,
            "direction": "up" if size > 0 else ("down" if size < 0 else "flat"),
        })
    return gaps, warnings, levels
