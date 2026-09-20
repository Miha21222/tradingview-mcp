"""Economic-calendar collection for the `calendar` toolset. (M9)

**Collection only, no policy.** This module fetches and normalizes what the
public calendar feeds say, and reads the static tables in `fallback_dates.json`
that no feed reliably carries (exchange holidays, half-days, quarterly futures
rollover). It never decides whether an event means "don't trade": which events
matter is the caller's judgment, and lives outside this repo.

Sources, tried in this order:

1. **Forex Factory** weekly JSON (`nfs.faireconomy.media/ff_calendar_*.json`).
   Serves last / this / next week only - a date outside those three weeks skips
   straight to source 2 without a request.
2. **FXStreet** day endpoint (`calendar-api.fxstreet.com`), which takes an
   arbitrary date range.
3. **`fallback_dates.json`** - a last resort with deliberately limited
   coverage. Reaching it sets `degraded: true`.

Network manners: one short-timeout request, at most one retry, a small
in-process TTL cache so a burst of calls hits the feeds once. A source being
down is a `degraded`/`warnings` fact, never an exception.

**Every string that comes back from a feed is untrusted data.** Titles are
stripped of control characters and truncated; nothing fetched here is ever
interpreted as an instruction.
"""

from __future__ import annotations

import json
import time as _time
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

FF_URLS = {
    "lastweek": "https://nfs.faireconomy.media/ff_calendar_lastweek.json",
    "thisweek": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "nextweek": "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
}
FXSTREET_BASE = "https://calendar-api.fxstreet.com/en/api/v1/eventDates"

TIMEOUT_S = 8.0
RETRIES = 1  # one retry, no more - a slow feed must not stall a tool call
CACHE_TTL_S = 600
MAX_TITLE = 140
MAX_VALUE = 32

IMPACT_RANK = {"low": 1, "medium": 2, "high": 3}
_FALLBACK_PATH = Path(__file__).with_name("fallback_dates.json")

# country code -> the currency code the feeds usually tag an event with, so a
# caller may filter by either ("US" and "USD" both match a USD row).
_CURRENCY_BY_COUNTRY = {
    "US": "USD", "GB": "GBP", "UK": "GBP", "JP": "JPY", "CA": "CAD", "AU": "AUD",
    "NZ": "NZD", "CH": "CHF", "CN": "CNY", "EU": "EUR", "EMU": "EUR", "DE": "EUR",
    "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR", "PT": "EUR", "IE": "EUR",
    "GR": "EUR", "AT": "EUR", "BE": "EUR", "FI": "EUR", "SE": "SEK", "NO": "NOK",
    "DK": "DKK", "MX": "MXN", "BR": "BRL", "IN": "INR", "ZA": "ZAR", "SG": "SGD",
    "HK": "HKD", "KR": "KRW", "TR": "TRY", "RU": "RUB", "PL": "PLN",
}
_COUNTRY_BY_CURRENCY = {"USD": "US", "GBP": "GB", "JPY": "JP", "CAD": "CA",
                        "AUD": "AU", "NZD": "NZ", "CHF": "CH", "CNY": "CN",
                        "EUR": "EU", "SEK": "SE", "NOK": "NO", "MXN": "MX",
                        "BRL": "BR", "INR": "IN", "ZAR": "ZA", "SGD": "SG",
                        "HKD": "HK", "KRW": "KR", "TRY": "TR", "RUB": "RU"}


# --------------------------------------------------------------------------- #
# untrusted-string hygiene
# --------------------------------------------------------------------------- #
def clean(value, limit: int = MAX_TITLE) -> str | None:
    """Feed string -> safe, bounded plain text (or None). Data, never instructions."""
    if value is None:
        return None
    s = str(value)
    s = "".join(ch for ch in s if ch.isprintable() or ch == " ").strip()
    if not s:
        return None
    return s[:limit]


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
class HttpFetcher:
    """`url -> parsed JSON | None`, with a short timeout, one retry and a TTL cache.

    Tests never use this: the toolset takes an injected fetcher instead, so the
    unit suite makes no network calls at all.
    """

    def __init__(self, timeout: float = TIMEOUT_S, retries: int = RETRIES,
                 ttl: float = CACHE_TTL_S) -> None:
        self.timeout = timeout
        self.retries = max(0, min(int(retries), RETRIES))
        self.ttl = ttl
        self._cache: dict[str, tuple[float, object]] = {}

    def __call__(self, url: str):
        hit = self._cache.get(url)
        if hit and (_time.monotonic() - hit[0]) < self.ttl:
            return hit[1]
        import httpx

        headers = {
            "User-Agent": "tvmcp/0.1 (+https://github.com/; economic calendar read)",
            "Accept": "application/json",
        }
        if "fxstreet" in url:
            headers["Referer"] = "https://www.fxstreet.com/economic-calendar"
        for attempt in range(self.retries + 1):
            try:
                r = httpx.get(url, timeout=self.timeout, headers=headers,
                              follow_redirects=True)
                r.raise_for_status()
                data = r.json()
            except Exception:  # noqa: BLE001 - any failure is the same answer: no data
                if attempt >= self.retries:
                    return None
                continue
            self._cache[url] = (_time.monotonic(), data)
            return data
        return None


# --------------------------------------------------------------------------- #
# country / impact helpers
# --------------------------------------------------------------------------- #
def country_tokens(*values) -> set[str]:
    """Every code an event (or a filter) may reasonably be matched by."""
    out: set[str] = set()
    for v in values:
        if not v:
            continue
        code = str(v).strip().upper()
        if not code:
            continue
        out.add(code)
        if code in _CURRENCY_BY_COUNTRY:
            out.add(_CURRENCY_BY_COUNTRY[code])
        if code in _COUNTRY_BY_CURRENCY:
            out.add(_COUNTRY_BY_CURRENCY[code])
    return out


def normalize_impact(value, all_day: bool = False, title: str | None = None) -> str:
    v = str(value or "").strip().lower()
    if v in ("holiday", "bank holiday"):
        return "holiday"
    if all_day and title and "holiday" in title.lower():
        return "holiday"
    if v in ("high", "3"):
        return "high"
    if v in ("medium", "moderate", "2"):
        return "medium"
    if v in ("low", "1"):
        return "low"
    return "none"


def passes_impact(impact: str, minimum: str | None) -> bool:
    """Holidays always pass - they are a fact about the session, not a data release."""
    if not minimum or impact == "holiday":
        return True
    return IMPACT_RANK.get(impact, 0) >= IMPACT_RANK.get(minimum, 0)


def _to_utc_iso(raw) -> tuple[str | None, str | None]:
    """Feed timestamp -> (`...Z` UTC ISO, the original string kept verbatim)."""
    if raw is None:
        return None, None
    text = str(raw)
    try:
        s = text.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None, clean(text, 40)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), clean(text, 40)


def _event(time_utc, time_raw, country, title, impact, actual, forecast,
           previous, source) -> dict:
    return {
        "time_utc": time_utc,
        "time_raw": time_raw,
        "country": clean(country, 8),
        "title": clean(title),
        "impact": impact,
        "actual": clean(actual, MAX_VALUE),
        "forecast": clean(forecast, MAX_VALUE),
        "previous": clean(previous, MAX_VALUE),
        "source": source,
    }


# --------------------------------------------------------------------------- #
# Forex Factory
# --------------------------------------------------------------------------- #
def ff_slot(target: _date, today: _date) -> str | None:
    """Which weekly FF feed covers `target`, or None when none of them does."""
    this_monday = today - timedelta(days=today.weekday())
    target_monday = target - timedelta(days=target.weekday())
    delta_weeks = (target_monday - this_monday).days // 7
    return {-1: "lastweek", 0: "thisweek", 1: "nextweek"}.get(delta_weeks)


def parse_ff(rows) -> list[dict]:
    if not isinstance(rows, list):
        return []
    out = []
    for e in rows:
        if not isinstance(e, dict):
            continue
        title = clean(e.get("title"))
        impact = normalize_impact(e.get("impact"), title=title)
        time_utc, time_raw = _to_utc_iso(e.get("date"))
        out.append(_event(time_utc, time_raw, e.get("country"), title, impact,
                          e.get("actual"), e.get("forecast"), e.get("previous"),
                          "forexfactory"))
    return out


# --------------------------------------------------------------------------- #
# FXStreet
# --------------------------------------------------------------------------- #
def fxstreet_url(start: _date, end: _date, countries: list[str] | None = None) -> str:
    params: list[tuple[str, str]] = [("volatilities", v) for v in ("HIGH", "MEDIUM", "LOW")]
    codes: list[str] = []
    for token in countries or []:
        code = str(token).strip().upper()
        codes.append(_COUNTRY_BY_CURRENCY.get(code, code))
    params.append(("countries", ",".join(sorted(set(codes)))))
    params.append(("categories", ""))
    return (f"{FXSTREET_BASE}/{start.isoformat()}T00:00:00Z/"
            f"{end.isoformat()}T23:59:59Z?{urlencode(params)}")


def parse_fxstreet(rows) -> list[dict]:
    if not isinstance(rows, list):
        return []
    out = []
    for e in rows:
        if not isinstance(e, dict):
            continue
        title = clean(e.get("name"))
        impact = normalize_impact(e.get("volatility"), bool(e.get("isAllDay")), title)
        time_utc, time_raw = _to_utc_iso(e.get("dateUtc"))
        country = e.get("currencyCode") or e.get("countryCode")
        out.append(_event(time_utc, time_raw, country, title, impact,
                          e.get("actual"), e.get("consensus"), e.get("previous"),
                          "fxstreet"))
    return out


# --------------------------------------------------------------------------- #
# static tables
# --------------------------------------------------------------------------- #
_FALLBACK: dict | None = None


def load_fallback(path: Path | None = None) -> dict:
    """The static tables, read once. Missing/broken file -> empty tables."""
    global _FALLBACK
    if path is not None:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if _FALLBACK is None:
        try:
            _FALLBACK = json.loads(_FALLBACK_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _FALLBACK = {}
    return _FALLBACK


def _in_range(day: _date, start: _date, end: _date) -> bool:
    return start <= day <= end


def holidays_for(start: _date, end: _date, countries: list[str] | None,
                 table: dict | None = None) -> tuple[list[dict], list[str]]:
    """Exchange holidays and half-days from the static table, plus coverage warnings."""
    data = (table if table is not None else load_fallback()).get("holidays", {})
    wanted = country_tokens(*(countries or []))
    out: list[dict] = []
    warnings: list[str] = []
    years = sorted({str(y) for y in range(start.year, end.year + 1)})
    for code, block in data.items():
        if wanted and not (country_tokens(code) & wanted):
            continue
        for year in years:
            covered = False
            for kind, key in (("full", "full"), ("half", "half")):
                rows = (block.get(key) or {}).get(year)
                if rows is None:
                    continue
                covered = True
                for row in rows:
                    try:
                        day = _date.fromisoformat(row[0])
                    except (ValueError, IndexError, TypeError):
                        continue
                    if not _in_range(day, start, end):
                        continue
                    entry = {
                        "date": day.isoformat(),
                        "country": code,
                        "name": clean(row[1] if len(row) > 1 else kind),
                        "kind": kind,
                        "market": clean(block.get("market"), 160),
                        "source": "fallback_dates.json",
                    }
                    if kind == "half":
                        entry["close_local"] = clean(row[2] if len(row) > 2 else None, 8)
                        entry["tz"] = clean(block.get("tz"), 40)
                    out.append(entry)
            if not covered:
                warnings.append(
                    f"no holiday table for {code} in {year} - holidays are UNKNOWN for "
                    "that year, not absent (fallback_dates.json has limited coverage)"
                )
    out.sort(key=lambda h: (h["date"], h["country"]))
    return out, warnings


def third_friday(year: int, month: int) -> _date:
    d = _date(year, month, 15)
    while d.weekday() != 4:  # Friday
        d += timedelta(days=1)
    return d


def _root_candidates(symbol: str) -> list[str]:
    """`CME_MINI:ES1!` -> ['ES']; `ESZ2026` -> ['ESZ', 'ES']; `SPX500` -> ['SPX500', 'SPX'].

    Continuous-contract suffixes (`1!`, `2!`) are always stripped. A trailing
    month-letter + year is offered as a SECOND candidate only, so a cash ticker
    that happens to end in a month letter (SPX500) is not mangled into a root.
    """
    s = str(symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    while s and s[-1] == "!":
        s = s[:-1]
        while s and s[-1].isdigit():
            s = s[:-1]
    if not s:
        return []
    out = [s]
    if len(s) > 2 and s[-1].isdigit():
        body = s.rstrip("0123456789")
        if len(body) > 1 and body[-1] in "FGHJKMNQUVXZ":
            out.append(body[:-1])
    return out


def rollover_for(symbol: str, on: _date, table: dict | None = None) -> dict | None:
    """Next quarterly expiry + conventional roll date for an equity-index future.

    Facts only: the expiry the third-Friday rule gives, the roll date the
    convention gives, and whether `on` sits between them. An unknown root
    returns None with the reason - never a guessed rule.
    """
    cfg = (table if table is not None else load_fallback()).get("rollover") or {}
    roots = cfg.get("roots") or {}
    candidates = _root_candidates(symbol)
    if not candidates:
        return None
    root = candidates[0]
    info = None
    for cand in candidates:
        if cand in roots:
            root, info = cand, roots[cand]
            break
    if info is None:
        return {
            "symbol": clean(symbol, 40), "root": root, "known": False,
            "note": ("no rollover table for this root; fallback_dates.json covers "
                     "quarterly equity-index futures only"),
        }
    months = cfg.get("months") or [3, 6, 9, 12]
    roll_before = int(cfg.get("roll_days_before") or 8)
    expiry = None
    for year in (on.year, on.year + 1):
        for m in sorted(months):
            cand = third_friday(year, m)
            if cand >= on:
                expiry = cand
                break
        if expiry:
            break
    if expiry is None:
        return None
    roll = expiry - timedelta(days=roll_before)
    return {
        "symbol": clean(symbol, 40),
        "root": root,
        "known": True,
        "contract": clean(info.get("name"), 60),
        "exchange": clean(info.get("exchange"), 20),
        "rule": f"{cfg.get('rule', 'third_friday')}; roll {roll_before} days before expiry",
        "expiry_date": expiry.isoformat(),
        "roll_date": roll.isoformat(),
        "days_to_expiry": (expiry - on).days,
        "days_to_roll": (roll - on).days,
        "in_roll_window": roll <= on <= expiry,
    }


_WEEKDAY_FRIDAY = 4


def _first_weekday(year: int, month: int, weekday: int) -> _date:
    d = _date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def _last_weekday(year: int, month: int, weekday: int) -> _date:
    if month == 12:
        d = _date(year, 12, 31)
    else:
        d = _date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def fallback_events(start: _date, end: _date, countries: list[str] | None,
                    table: dict | None = None) -> tuple[list[dict], list[str]]:
    """Last-resort events: the published dates we carry plus approximate monthly rules."""
    data = table if table is not None else load_fallback()
    wanted = country_tokens(*(countries or []))
    out: list[dict] = []
    warnings: list[str] = []

    scheduled = data.get("scheduled_events") or {}
    for code, years in scheduled.items():
        if wanted and not (country_tokens(code) & wanted):
            continue
        for year in range(start.year, end.year + 1):
            rows = years.get(str(year))
            if rows is None:
                warnings.append(
                    f"fallback: no published event table for {code} in {year}"
                )
                continue
            for row in rows:
                try:
                    day = _date.fromisoformat(row["date"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not _in_range(day, start, end):
                    continue
                out.append(_event(
                    _local_iso(day, row.get("time_local"), row.get("tz")),
                    None, code, row.get("title"),
                    normalize_impact(row.get("impact")),
                    None, None, None, "fallback_dates.json",
                ))

    for rule in data.get("monthly_rules") or []:
        code = rule.get("country")
        if wanted and not (country_tokens(code) & wanted):
            continue
        for day in _rule_days(rule.get("rule"), start, end):
            out.append(_event(
                _local_iso(day, rule.get("time_local"), rule.get("tz")), None,
                code, rule.get("title"), normalize_impact(rule.get("impact")),
                None, None, None, "fallback_dates.json (approximate rule)",
            ))
    out.sort(key=lambda e: (e["time_utc"] or "", e["title"] or ""))
    return out, warnings


def _rule_days(rule: str | None, start: _date, end: _date) -> list[_date]:
    days: list[_date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        if rule == "first_friday":
            d = _first_weekday(year, month, _WEEKDAY_FRIDAY)
        elif rule == "last_friday":
            d = _last_weekday(year, month, _WEEKDAY_FRIDAY)
        else:
            d = None
        if d is not None and _in_range(d, start, end):
            days.append(d)
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return days


def _local_iso(day: _date, time_local: str | None, tzname: str | None) -> str | None:
    if not time_local:
        return None
    try:
        hh, mm = (int(x) for x in str(time_local).split(":"))
        zone = ZoneInfo(str(tzname or "UTC"))
    except Exception:  # noqa: BLE001
        return None
    dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# the collection pipeline
# --------------------------------------------------------------------------- #
def collect_events(start: _date, end: _date, countries: list[str] | None,
                   fetch, today: _date) -> tuple[list[dict], list[str], list[str], bool]:
    """(events, sources_used, warnings, degraded) for [start, end] inclusive.

    Forex Factory first (it only serves last/this/next week), then FXStreet,
    then the static tables. A source being down is reported, never raised.
    """
    sources_used: list[str] = []
    warnings: list[str] = []

    slots = []
    for offset in range((end - start).days + 1):
        slot = ff_slot(start + timedelta(days=offset), today)
        if slot and slot not in slots:
            slots.append(slot)
    if len(slots) * 7 < (end - start).days + 1 or not slots:
        warnings.append(
            "forexfactory serves last/this/next week only - the requested range is "
            "not fully covered by it"
        )
    rows: list[dict] = []
    ff_failed = False
    for slot in slots:
        data = fetch(FF_URLS[slot])
        if data is None:
            ff_failed = True
            continue
        rows.extend(parse_ff(data))
    if rows:
        sources_used.append("forexfactory")
    if ff_failed:
        warnings.append("forexfactory did not answer for part of the range")

    # "The first source answered" is not the same as "the answer covers what was
    # asked". Forex Factory can return a full week and still hold nothing for the
    # requested day or country - and an empty calendar reads as "no news today",
    # which is the one wrong answer a calendar must never give quietly. So the
    # usefulness of the rows is measured against the actual request before
    # deciding whether the next source is still needed.
    def _useful(candidate: list[dict]) -> list[dict]:
        wanted = country_tokens(*(countries or [])) if countries else None
        out = []
        for e in candidate:
            if e.get("time_utc"):
                try:
                    day = _date.fromisoformat(e["time_utc"][:10])
                except ValueError:
                    continue
                if not _in_range(day, start, end):
                    continue
            if wanted and not (country_tokens(e.get("country")) & wanted):
                continue
            out.append(e)
        return out

    ff_useful = _useful(rows)
    if rows and not ff_useful:
        seen = sorted({str(e.get("country") or "?") for e in rows})[:8]
        warnings.append(
            "forexfactory answered but carried nothing for this day/country filter "
            f"(codes it did carry: {', '.join(seen) or 'none'}) - trying fxstreet"
        )
        if "forexfactory" in sources_used:
            sources_used.remove("forexfactory")

    if not ff_useful:
        data = fetch(fxstreet_url(start, end, countries))
        if data is None:
            warnings.append("fxstreet did not answer")
        else:
            fx_rows = parse_fxstreet(data)
            if _useful(fx_rows):
                rows = fx_rows
                sources_used.append("fxstreet")
            elif not rows:
                rows = fx_rows

    degraded = not rows
    if degraded:
        rows, fb_warn = fallback_events(start, end, countries)
        warnings.extend(fb_warn)
        warnings.append(
            "both live calendars failed - falling back to fallback_dates.json, whose "
            "coverage is limited (published FOMC dates for the years it carries, plus "
            "approximate monthly rules). A date missing from it means UNKNOWN."
        )
        if rows:
            sources_used.append("fallback_dates.json")

    kept = []
    for e in rows:
        if e["time_utc"]:
            try:
                day = _date.fromisoformat(e["time_utc"][:10])
            except ValueError:
                continue
            if not _in_range(day, start, end):
                continue
        kept.append(e)
    return kept, sources_used, warnings, degraded
