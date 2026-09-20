"""FX Replay CSV -> normalized journal records + summary.

Built against a genuine export (`tests/fixtures/journal/fxreplay_sample.csv`). The
raw schema is mapped to semantic fields per the owner's vault-note conventions:
- `side` buy/sell -> "long"/"short"
- `day` weekday code (1..7, ISO) -> weekday name
- entry UTC hour -> session/killzone band
- `pair` resolved through symbols.py (OANDA:EURUSD -> EURUSD)
- `amount` units -> lots via the forex contract size

Times are parsed as `YYYY/MM/DD HH:MM:SS`; `utc_offset_hours` shifts the export's
timezone to UTC (default 0 = the export is already UTC). No Notion writes - this is
a read-only normalizer feeding the owner's own Notion MCP later.

M9: every record is a **superset of the canonical journal record** (`schema.FIELDS`)
- the FX Replay-specific keys (`r_pnl`, `entry_time`, `lots`, `session`, `day`, ...)
stay exactly as they were, and the canonical keys (`date`, `opened_at`, `pnl`,
`r_multiple`, `source`, `derived`, ...) sit beside them, so `tv_risk_guard` and any
other consumer can read an FX Replay export without knowing it is one.
`schema.to_canonical(record)` projects a record down to the canonical fields.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from ..backtest.forex import contract_size, killzone
from ..symbols import resolve
from . import schema

REQUIRED_COLUMNS = {
    "id", "dateStart", "dateEnd", "pair", "rPnL", "side", "entryPrice",
    "initialSL", "amount", "status", "day", "avgClosePrice",
    "avgRiskReward", "initialBalance", "currentRealizedBalance",
}

DAY_NAMES = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday",
             5: "Friday", 6: "Saturday", 7: "Sunday"}


def _num(v) -> float | None:
    if v is None or not str(v).strip():
        return None
    return float(v)


def _dt(v: str, utc_offset_hours: int) -> datetime:
    dt = datetime.strptime(v.strip(), "%Y/%m/%d %H:%M:%S")
    if utc_offset_hours:
        dt = dt - timedelta(hours=utc_offset_hours)
    return dt


def _split_tags(v: str) -> list[str]:
    return [t.strip() for t in v.split(",") if t.strip()] if v and str(v).strip() else []


def normalize_export(
    rows: list[dict],
    utc_offset_hours: int = 0,
    currency: str | None = None,
    raw_ref_prefix: str | None = None,
) -> tuple[list[dict], dict]:
    """Normalize FX Replay rows. `currency` labels `pnl_currency` (the export omits it)."""
    records = [
        _normalize_row(r, utc_offset_hours, currency, raw_ref_prefix, i + 2)
        for i, r in enumerate(rows)
    ]
    return records, _summary(records)


def _normalize_row(
    row: dict,
    utc_offset_hours: int,
    currency: str | None = None,
    raw_ref_prefix: str | None = None,
    row_number: int | None = None,
) -> dict:
    symbol = resolve(row["pair"])
    side_raw = (row.get("side") or "").strip().lower()
    if side_raw not in ("buy", "long", "sell", "short"):
        raise ValueError(f"Unknown side {side_raw!r}; expected buy/sell/long/short")
    side = "long" if side_raw in ("buy", "long") else "short"
    entry = _num(row.get("entryPrice"))
    sl = _num(row.get("initialSL"))
    exit_price = _num(row.get("avgClosePrice")) or _num(row.get("maxTP"))
    amount = _num(row.get("amount")) or 0.0
    r_pnl = _num(row.get("rPnL")) or 0.0
    risk = None
    if entry is not None and sl is not None and entry != sl:
        risk = amount * abs(entry - sl)
    r = round(r_pnl / risk, 3) if risk else None
    entry_dt = _dt(row["dateStart"], utc_offset_hours)
    exit_dt = _dt(row["dateEnd"], utc_offset_hours)
    try:
        lots = round(amount / contract_size(symbol.canonical), 4) if amount else None
    except ValueError:
        lots = None  # non-FX instrument: no defined contract size, size stays in units
    canonical = schema.make_record(
        source="fxreplay",
        id=row["id"],
        date=entry_dt.date(),
        opened_at=entry_dt,
        closed_at=exit_dt,
        symbol=row["pair"],
        side=side,
        entry=entry,
        exit=exit_price,
        size=lots if lots is not None else (amount or None),
        size_unit="lots" if lots is not None else None,
        # `rPnL` is realized money in the export's account currency; `r` is the
        # R-multiple recomputed from the row's own stop distance (see above), so
        # neither number is derived - both come from the export.
        r_multiple=r,
        pnl=r_pnl,
        pnl_currency=currency,
        tags=row.get("tags"),
        raw_ref=f"{raw_ref_prefix}#{row_number}" if raw_ref_prefix and row_number else None,
    )
    return {
        **canonical,
        "id": int(row["id"]),
        "symbol": symbol.canonical,
        "tv_symbol": row["pair"],
        "side": side,
        "status": row.get("status", "").strip(),
        "entry_time": entry_dt.isoformat(),
        "exit_time": exit_dt.isoformat(),
        "day": DAY_NAMES.get(int(float(row.get("day") or 0))),
        "session": killzone(entry_dt.hour) or "off",
        "entry_price": round(entry, 6) if entry is not None else None,
        "exit_price": round(exit_price, 6) if exit_price is not None else None,
        "sl": round(sl, 6) if sl is not None else None,
        "tp": round(_num(row.get("maxTP")), 6) if _num(row.get("maxTP")) is not None else None,
        "ideal_tp": round(_num(row.get("idealTP")), 6) if _num(row.get("idealTP")) is not None else None,
        "amount": amount,
        "lots": lots,
        "u_pnl": _num(row.get("uPnL")),
        "r_pnl": round(r_pnl, 6),
        "risk": round(risk, 6) if risk else None,
        "r": r,
        "avg_risk_reward": _num(row.get("avgRiskReward")),
        "tags": _split_tags(row.get("tags")),
        "balance_after": _num(row.get("currentRealizedBalance")),
    }


def _summary(records: list[dict]) -> dict:
    n = len(records)
    if n == 0:
        return {"trades": 0, "win_rate_pct": 0.0, "total_pnl": 0.0, "expectancy": 0.0}
    pnls = [r["r_pnl"] for r in records]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rs = [r["r"] for r in records if r["r"] is not None]
    by_session: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    by_side: dict[str, dict] = {}
    for r in records:
        for key, bucket in ((r["session"], by_session), (r["day"], by_day), (r["side"], by_side)):
            b = bucket.setdefault(key or "unknown", {"count": 0, "pnl": 0.0})
            b["count"] += 1
            b["pnl"] = round(b["pnl"] + r["r_pnl"], 6)
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate_pct": round(100 * len(wins) / n, 1),
        "total_pnl": round(sum(pnls), 6),
        "expectancy": round(sum(pnls) / n, 2),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
        "by_session": by_session,
        "by_day": by_day,
        "by_side": by_side,
    }


def looks_like_fxreplay(headers) -> bool:
    """True when a header row carries the full FX Replay export schema."""
    return not (REQUIRED_COLUMNS - {str(h).strip() for h in (headers or [])})


def parse_file(
    path: Path,
    utc_offset_hours: int = 0,
    max_rows: int = 10_000,
    max_bytes: int = 10_000_000,
    currency: str | None = None,
) -> tuple[list[dict], dict]:
    """Read an FX Replay CSV export and normalize it.

    Raises ValueError if the schema is missing or the file exceeds the input guard
    (whole-file row/byte caps so oversized exports fail fast instead of being parsed).
    """
    if path.stat().st_size > max_bytes:
        raise ValueError(f"{path.name} is {path.stat().st_size} bytes; limit is {max_bytes}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {h.strip() for h in (reader.fieldnames or [])}
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise ValueError(
                f"{path.name} is missing required FX Replay columns: {sorted(missing)}"
            )
        rows = []
        for row in reader:
            rows.append(dict(row))
            if len(rows) > max_rows:
                raise ValueError(f"{path.name} has more than {max_rows} rows; refused")
    return normalize_export(rows, utc_offset_hours, currency, path.name)