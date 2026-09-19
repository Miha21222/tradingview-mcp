"""Level-tag checks: has price touched a level/zone since a moment in time? (M8)

Pure pandas/python, feed-agnostic: `tv_scan_check_levels` feeds it free-provider
bars, `tv_desktop_check_levels` feeds it the live chart's bars. A level is
either a price (`{name, price}`) or a zone (`{name, high, low}`); a bar tags it
when `high >= low_edge and low <= high_edge`. The result says whether it was
tagged since `since`, the first tagging bar and the side price came from, the
closest approach when it was not, and a coverage warning whenever the bars
cannot answer the question honestly (history starts after `since`, no bars
after `since`, or the last bar is stale).

`parse_since` accepts ISO-8601 (naive = UTC) or `session:<name>[@YYYY-MM-DD]`
using the fixed-UTC session table `SESSION_WINDOWS_UTC` shared with
`tv_scan_sessions` - not DST-aware, by design of the pinned library.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd
from fastmcp.exceptions import ToolError

from .detectors import SESSION_WINDOWS_UTC

MAX_LEVELS = 50
STALE_FACTOR = 2  # last bar older than STALE_FACTOR x timeframe -> warning


def _iso(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").isoformat().replace("+00:00", "Z")


def to_utc(value) -> pd.Timestamp:
    """Unix seconds / ISO string / datetime / Timestamp -> tz-aware UTC Timestamp."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return pd.Timestamp(float(value), unit="s", tz="UTC")
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def parse_since(spec: str, timeframe_minutes: int | None = None,
                now: datetime | None = None) -> pd.Timestamp:
    """`since` argument -> UTC Timestamp, floored to the timeframe when given.

    - ISO-8601 (`2026-09-19T13:30:00Z`, `2026-09-19 13:30`, `2026-09-19`);
      naive values are UTC.
    - `session:<name>` = the most recent start of that session (today's if it
      has begun, else yesterday's); `session:<name>@YYYY-MM-DD` = that date's
      start. Names are case-insensitive keys of SESSION_WINDOWS_UTC; windows
      are fixed UTC hours, not DST-aware.
    """
    s = (spec or "").strip()
    if not s:
        raise ToolError("since must be an ISO-8601 time or session:<name>[@YYYY-MM-DD]")
    now_ts = to_utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if s.lower().startswith("session:"):
        body = s[len("session:"):]
        name, _, date_part = body.partition("@")
        key = next((k for k in SESSION_WINDOWS_UTC if k.lower() == name.strip().lower()), None)
        if key is None:
            raise ToolError(
                f"Unknown session {name.strip()!r}; use one of {sorted(SESSION_WINDOWS_UTC)}"
            )
        hh, mm = (int(x) for x in SESSION_WINDOWS_UTC[key][0].split(":"))
        if date_part:
            try:
                day = datetime.strptime(date_part.strip(), "%Y-%m-%d").date()
            except ValueError:
                raise ToolError(f"Bad date {date_part!r}; use YYYY-MM-DD") from None
            since = pd.Timestamp(datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc))
        else:
            day = now_ts.date()
            since = pd.Timestamp(datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc))
            if since > now_ts:
                since -= timedelta(days=1)
    else:
        try:
            since = to_utc(s)
        except (ValueError, TypeError):
            raise ToolError(
                f"Cannot parse since={s!r}; use ISO-8601 (UTC) or session:<name>[@YYYY-MM-DD]"
            ) from None
    if timeframe_minutes:
        since = since.floor(f"{int(timeframe_minutes)}min")
    return since


def normalize_levels(levels: list[dict]) -> list[dict]:
    """Validate `[{name, price} | {name, high, low}]` -> `[{name, lo, hi, kind}]`."""
    if not isinstance(levels, list) or not levels:
        raise ToolError("levels must be a non-empty list of {name, price} or {name, high, low}")
    if len(levels) > MAX_LEVELS:
        raise ToolError(f"At most {MAX_LEVELS} levels per call (got {len(levels)})")
    out = []
    for i, lv in enumerate(levels):
        if not isinstance(lv, dict):
            raise ToolError(f"levels[{i}] must be an object")
        name = str(lv.get("name") or f"level_{i + 1}")[:60]
        if lv.get("price") is not None:
            p = _num(lv["price"], f"levels[{i}].price")
            out.append({"name": name, "lo": p, "hi": p, "kind": "price"})
        elif lv.get("high") is not None and lv.get("low") is not None:
            hi = _num(lv["high"], f"levels[{i}].high")
            lo = _num(lv["low"], f"levels[{i}].low")
            if lo > hi:
                lo, hi = hi, lo
            out.append({"name": name, "lo": lo, "hi": hi, "kind": "zone"})
        else:
            raise ToolError(f"levels[{i}] needs either price or both high and low")
    return out


def _num(v, what: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ToolError(f"{what} must be a number") from None
    if not math.isfinite(f):
        raise ToolError(f"{what} must be finite")
    return f


def _side_of(price: float, lo: float, hi: float) -> str:
    if price < lo:
        return "below"
    if price > hi:
        return "above"
    return "inside"


def _infer_tf_minutes(times: pd.Series) -> int | None:
    if len(times) < 2:
        return None
    d = times.diff().dropna().dt.total_seconds()
    return max(1, int(round(float(d.median()) / 60))) if len(d) else None


def check_levels(df: pd.DataFrame, levels: list[dict], since_ts,
                 timeframe_minutes: int | None = None,
                 now: datetime | None = None) -> list[dict]:
    """Per level: {name, tagged, first_tag, closest_approach, coverage_warning}."""
    norm = normalize_levels(levels)
    since = to_utc(since_ts)
    now_ts = to_utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if df is None or len(df) == 0:
        base = [{"name": n["name"], "tagged": False, "first_tag": None,
                 "closest_approach": None, "coverage_warning": "no bars loaded"}
                for n in norm]
        return base
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], utc=True)
    d = d.sort_values("time").reset_index(drop=True)
    tf = timeframe_minutes or _infer_tf_minutes(d["time"])
    warnings: list[str] = []
    first_bar, last_bar = d["time"].iloc[0], d["time"].iloc[-1]
    if first_bar > since:
        warnings.append(
            f"history starts at {_iso(first_bar)}, after since={_iso(since)} - "
            "tags before that are unknown"
        )
    after = d[d["time"] >= since]
    if after.empty:
        warnings.append(f"no bars at or after since={_iso(since)}")
    if tf and (now_ts - last_bar) > pd.Timedelta(minutes=STALE_FACTOR * tf):
        warnings.append(
            f"last bar {_iso(last_bar)} is stale (> {STALE_FACTOR}x{tf}m before now) - "
            "recent tags may be missing"
        )
    warning = "; ".join(warnings) or None

    results = []
    for lv in norm:
        lo, hi = lv["lo"], lv["hi"]
        entry = {"name": lv["name"], "kind": lv["kind"]}
        if lv["kind"] == "price":
            entry["price"] = lo
        else:
            entry["high"], entry["low"] = hi, lo
        entry.update(tagged=False, first_tag=None, closest_approach=None,
                     coverage_warning=warning)
        if after.empty:
            results.append(entry)
            continue
        hit = after[(after["high"] >= lo) & (after["low"] <= hi)]
        if not hit.empty:
            i = int(hit.index[0])
            row = d.loc[i]
            if i > 0:
                ref = float(d.loc[i - 1, "close"])
            else:
                ref = float(row["open"])
            side = _side_of(ref, lo, hi)
            entry["tagged"] = True
            entry["first_tag"] = {
                "time": _iso(row["time"]), "from": side,
                "high": float(row["high"]), "low": float(row["low"]),
            }
            entry["closest_approach"] = {"distance": 0.0, "time": _iso(row["time"]),
                                         "side": side}
            results.append(entry)
            continue
        # untagged: nearest bar edge to the zone, and which side price stayed on
        below = (lo - after["high"]).where(after["high"] < lo)
        above = (after["low"] - hi).where(after["low"] > hi)
        dist = below.fillna(above)
        j = int(dist.idxmin())
        side = "below" if pd.notna(below.loc[j]) else "above"
        entry["closest_approach"] = {
            "distance": round(float(dist.loc[j]), 8),
            "time": _iso(d.loc[j, "time"]),
            "side": side,
        }
        results.append(entry)
    return results


def rows_to_df(rows: list[list]) -> pd.DataFrame:
    """`[[t,o,h,l,c,v], ...]` (unix seconds) -> the DataFrame `check_levels` wants."""
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df
