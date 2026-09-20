"""The one normalized journal record every tvmcp journal consumer speaks.

A record is a plain JSON-safe dict carrying exactly `FIELDS`:

    id, date, opened_at, closed_at, symbol, side, entry, exit, size, size_unit,
    r_multiple, pnl, pnl_currency, fees, strategy, tags, source, raw_ref, derived

Contract (M9):

- `date` is the **session date** (ISO `YYYY-MM-DD`) the trade belongs to - the key
  every daily/weekly/monthly aggregate groups by. When the source has no session
  date it is taken from `opened_at` and that is written into `derived`.
- `opened_at` / `closed_at` are naive ISO-8601 in **UTC** (no offset suffix, like
  the rest of tvmcp), or null.
- `r_multiple` and `pnl` may each be missing. One is derived from the other **only**
  when the data supports it: a per-record risk amount (from the source's own risk /
  stop columns) or an explicit `risk_per_r` (account currency per 1R). Every
  derivation is named in the record's `derived` list. Nothing is ever invented - a
  record with neither a result nor the means to derive one stays incomplete and the
  caller turns it into a `problem`.
- A row that cannot be parsed is never silently dropped: `make_record` raises
  `ValueError` and the loader records it in `problems[]` with the row number and the
  reason. Any aggregate over a file with problems carries `fail_closed: true` - the
  generalized form of the E0 rule in the owner's workflow script: a broken journal
  must never quietly produce a reassuring number.
"""

from __future__ import annotations

import math
import re
from datetime import date as _date
from datetime import datetime, timedelta

from ..symbols import resolve as _resolve_symbol

FIELDS = (
    "id", "date", "opened_at", "closed_at", "symbol", "side", "entry", "exit",
    "size", "size_unit", "r_multiple", "pnl", "pnl_currency", "fees", "strategy",
    "tags", "source", "raw_ref", "derived",
)

SIDES = ("long", "short")
SIZE_UNITS = ("contracts", "lots", "shares")

_SIDE_ALIASES = {
    "buy": "long", "b": "long", "long": "long", "l": "long", "bull": "long",
    "bought": "long", "buy limit": "long",
    "sell": "short", "s": "short", "short": "short", "sh": "short",
    "bear": "short", "sold": "short", "sell limit": "short",
}
_UNIT_ALIASES = {
    "contract": "contracts", "contracts": "contracts", "cont": "contracts",
    "lot": "lots", "lots": "lots",
    "share": "shares", "shares": "shares", "stock": "shares", "stocks": "shares",
}

_DT_FORMATS = (
    "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
    "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",          # MetaTrader-style exports
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%Y%m%d %H:%M:%S",
)
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d.%m.%Y", "%m/%d/%Y", "%Y%m%d")

_BLANKS = {"", "-", "--", "n/a", "na", "none", "null", "nan"}
_NUM_JUNK = re.compile(r"[^0-9,.\-+eE]")


# ---------------------------------------------------------------- primitives

def is_blank(value) -> bool:
    return value is None or str(value).strip().lower() in _BLANKS


def as_float(value, field: str = "value") -> float | None:
    """Parse a number out of a journal cell. Blank -> None.

    Tolerates thousands separators, a decimal comma, currency symbols and
    accounting parentheses. Raises ValueError on anything else - a cell that
    looks like a number but is not must fail closed, never be read as 0.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field}={value!r} is not a number")
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).strip()
        if is_blank(s):
            return None
        negative = s.startswith("(") and s.endswith(")")
        s = _NUM_JUNK.sub("", s)
        if "," in s and "." in s:
            s = s.replace(",", "")
        elif s.count(",") == 1 and s.count(".") == 0:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
        if s in ("", "-", "+", ".", "-.", "+."):
            raise ValueError(f"{field}={value!r} is not a number")
        try:
            v = float(s)
        except ValueError as exc:
            raise ValueError(f"{field}={value!r} is not a number") from exc
        if negative:
            v = -v
    if not math.isfinite(v):
        raise ValueError(f"{field}={value!r} is not finite")
    return v


def parse_datetime(value, utc_offset_hours: int = 0, field: str = "time") -> datetime | None:
    """Parse a timestamp into a naive UTC datetime. Blank -> None, junk -> ValueError.

    An explicit offset in the string (`...Z`, `...+03:00`) wins over
    `utc_offset_hours`, which only shifts naive timestamps.
    """
    if isinstance(value, datetime):
        dt = value if value.tzinfo is None else (value - value.utcoffset()).replace(tzinfo=None)
        if value.tzinfo is not None:
            return dt
        return dt - timedelta(hours=utc_offset_hours) if utc_offset_hours else dt
    if is_blank(value):
        return None
    s = str(value).strip()
    dt: datetime | None = None
    try:
        parsed = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is not None:
            return (parsed - parsed.utcoffset()).replace(tzinfo=None)
        dt = parsed
    if dt is None:
        for fmt in _DT_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        try:
            d = parse_date(s, field=field)
        except ValueError:
            d = None
        if d is not None:
            dt = datetime(d.year, d.month, d.day)
    if dt is None:
        raise ValueError(f"{field}={value!r} is not a recognizable timestamp")
    return dt - timedelta(hours=utc_offset_hours) if utc_offset_hours else dt


def parse_date(value, field: str = "date") -> _date | None:
    """Parse a session date. Blank -> None, junk -> ValueError."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    if is_blank(value):
        return None
    s = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    for fmt in _DT_FORMATS:  # a full timestamp sitting in a date column
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s).date()
    except ValueError as exc:
        raise ValueError(f"{field}={value!r} is not a recognizable date") from exc


def normalize_side(value, field: str = "side") -> str | None:
    if is_blank(value):
        return None
    s = str(value).strip().lower()
    side = _SIDE_ALIASES.get(s)
    if side is None:
        raise ValueError(f"{field}={value!r} is not a known side (buy/sell/long/short)")
    return side


def normalize_size_unit(value) -> str | None:
    if is_blank(value):
        return None
    return _UNIT_ALIASES.get(str(value).strip().lower())


def split_tags(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(t).strip() for t in value if str(t).strip()]
    return [t.strip() for t in re.split(r"[,;|]", str(value)) if t.strip()]


# ---------------------------------------------------------------- the record

def make_record(
    *,
    source: str,
    id=None,
    date=None,
    opened_at=None,
    closed_at=None,
    symbol=None,
    side=None,
    entry=None,
    exit=None,
    size=None,
    size_unit=None,
    r_multiple=None,
    pnl=None,
    pnl_currency=None,
    fees=None,
    strategy=None,
    tags=None,
    raw_ref=None,
    risk_amount: float | None = None,
    risk_per_r: float | None = None,
    utc_offset_hours: int = 0,
    derived: list[str] | None = None,
) -> dict:
    """Build one normalized record, deriving r_multiple<->pnl only when supported.

    Cells arrive raw (strings from a CSV) or already parsed; parsing and validation
    happen here so every caller fails the same way. Raises ValueError with an
    actionable message when a cell cannot be read - the caller turns that into a
    `problems[]` entry instead of dropping the row.
    """
    derived = list(derived or [])
    opened = parse_datetime(opened_at, utc_offset_hours, "opened_at")
    closed = parse_datetime(closed_at, utc_offset_hours, "closed_at")
    session = parse_date(date)
    if session is None and opened is not None:
        session = opened.date()
        derived.append("date from opened_at")
    elif session is None and closed is not None:
        session = closed.date()
        derived.append("date from closed_at")

    r = as_float(r_multiple, "r_multiple")
    money = as_float(pnl, "pnl")

    if r is None and money is not None:
        if risk_amount:
            r = round(money / risk_amount, 4)
            derived.append("r_multiple from pnl / risk_amount")
        elif risk_per_r:
            r = round(money / risk_per_r, 4)
            derived.append("r_multiple from pnl / risk_per_r")
    elif money is None and r is not None:
        if risk_amount:
            money = round(r * risk_amount, 4)
            derived.append("pnl from r_multiple * risk_amount")
        elif risk_per_r:
            money = round(r * risk_per_r, 4)
            derived.append("pnl from r_multiple * risk_per_r")

    return {
        "id": None if id is None else str(id),
        "date": session.isoformat() if session else None,
        "opened_at": opened.isoformat() if opened else None,
        "closed_at": closed.isoformat() if closed else None,
        "symbol": None if is_blank(symbol) else _resolve_symbol(str(symbol)).canonical,
        "side": normalize_side(side),
        "entry": as_float(entry, "entry"),
        "exit": as_float(exit, "exit"),
        "size": as_float(size, "size"),
        "size_unit": normalize_size_unit(size_unit),
        "r_multiple": r,
        "pnl": money,
        "pnl_currency": None if is_blank(pnl_currency) else str(pnl_currency).strip().upper(),
        "fees": as_float(fees, "fees"),
        "strategy": None if is_blank(strategy) else str(strategy).strip(),
        "tags": split_tags(tags),
        "source": source,
        "raw_ref": None if raw_ref is None else str(raw_ref),
        "derived": derived,
    }


def to_canonical(record: dict) -> dict:
    """Project any superset record (e.g. the FX Replay one) down to FIELDS."""
    return {k: record.get(k) for k in FIELDS}


def has_result(record: dict) -> bool:
    return record.get("r_multiple") is not None or record.get("pnl") is not None


def outcome(record: dict) -> str:
    """win | loss | breakeven | unknown - pnl decides, r_multiple is the fallback."""
    for key in ("pnl", "r_multiple"):
        v = record.get(key)
        if v is not None:
            return "win" if v > 0 else ("loss" if v < 0 else "breakeven")
    return "unknown"


# ---------------------------------------------------------------- aggregates

def _bucket(rows: list[dict]) -> dict:
    wins = [r for r in rows if outcome(r) == "win"]
    rs = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
    ps = [r["pnl"] for r in rows if r.get("pnl") is not None]
    return {
        "count": len(rows),
        "wins": len(wins),
        "win_rate_pct": round(100 * len(wins) / len(rows), 1) if rows else None,
        "total_r": round(sum(rs), 4) if rs else None,
        "total_pnl": round(sum(ps), 2) if ps else None,
    }


def stats(records: list[dict], problems: list | None = None) -> dict:
    """Summary over normalized records; carries fail_closed when problems exist.

    Expectancy and profit factor are reported in R and in currency separately - a
    journal that only has R-multiples still gets honest R stats, one that only has
    money gets money stats, and the two are never mixed into a single number.
    """
    n = len(records)
    fail_closed = bool(problems)
    if n == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "win_rate_pct": None,
            "expectancy_r": None, "expectancy_currency": None, "total_r": None,
            "total_pnl": None, "profit_factor": None, "profit_factor_r": None,
            "best": None, "worst": None, "by_strategy": {}, "by_symbol": {},
            "first_date": None, "last_date": None, "trading_days": 0,
            "currency": None, "records_with_r": 0, "records_with_pnl": 0,
            "fail_closed": fail_closed, "warnings": [],
        }
    warnings: list[str] = []
    outcomes = [outcome(r) for r in records]
    wins, losses = outcomes.count("win"), outcomes.count("loss")
    breakeven, unknown = outcomes.count("breakeven"), outcomes.count("unknown")
    if unknown:
        warnings.append(f"{unknown} record(s) carry neither pnl nor r_multiple")

    rs = [r["r_multiple"] for r in records if r.get("r_multiple") is not None]
    ps = [r["pnl"] for r in records if r.get("pnl") is not None]
    currencies = {r["pnl_currency"] for r in records if r.get("pnl_currency")}
    currency = currencies.pop() if len(currencies) == 1 else ("mixed" if currencies else None)
    if currency == "mixed":
        warnings.append("records carry more than one pnl_currency; money aggregates mix currencies")

    def _pf(values: list[float]) -> float | None:
        loss = abs(sum(v for v in values if v < 0))
        return round(sum(v for v in values if v > 0) / loss, 3) if loss else None

    dates = sorted({r["date"] for r in records if r.get("date")})
    if len({id(r) for r in records if r.get("date")}) < n:
        warnings.append("some records have no session date; they fall outside every date window")

    scored = [r for r in records if has_result(r)]

    def _key(r: dict):
        return r["r_multiple"] if r.get("r_multiple") is not None else r["pnl"]

    def _brief(r: dict | None) -> dict | None:
        return None if r is None else {k: r.get(k) for k in ("id", "date", "symbol", "strategy", "r_multiple", "pnl")}

    by_strategy: dict[str, list[dict]] = {}
    by_symbol: dict[str, list[dict]] = {}
    for r in records:
        by_strategy.setdefault(r.get("strategy") or "unspecified", []).append(r)
        by_symbol.setdefault(r.get("symbol") or "unspecified", []).append(r)

    decided = wins + losses + breakeven
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate_pct": round(100 * wins / decided, 1) if decided else None,
        "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
        "expectancy_currency": round(sum(ps) / len(ps), 2) if ps else None,
        "total_r": round(sum(rs), 4) if rs else None,
        "total_pnl": round(sum(ps), 2) if ps else None,
        "profit_factor": _pf(ps) if ps else None,
        "profit_factor_r": _pf(rs) if rs else None,
        "best": _brief(max(scored, key=_key, default=None)),
        "worst": _brief(min(scored, key=_key, default=None)),
        "by_strategy": {k: _bucket(v) for k, v in sorted(by_strategy.items())},
        "by_symbol": {k: _bucket(v) for k, v in sorted(by_symbol.items())},
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "trading_days": len(dates),
        "currency": currency,
        "records_with_r": len(rs),
        "records_with_pnl": len(ps),
        "fail_closed": fail_closed,
        "warnings": warnings,
    }
