"""Load a trade journal of any shape into canonical records (`schema.FIELDS`).

Three shapes exist in the wild and every consumer used to re-implement the mapping:

1. **FX Replay exports** - the fixed schema `normalize.py` already handles.
2. **A workflow's own CSV** - one row per session, an `entered` yes/no column and a
   result in R (`result_r`), no symbol or money at all.
3. **Broker / platform exports** - a symbol, a side, money, sometimes lots, whatever
   the platform felt like calling its columns.

`load_journal()` sniffs the delimiter and the columns, maps them onto the canonical
record and returns `{records, stats, problems, fail_closed, mapping_used}`. An
explicit `mapping` ({canonical_field: column_name}) overrides the sniffer for odd
exports.

Fail-closed rule (generalized from the owner's E0): a row that cannot be parsed, or
that records an entry with no result, lands in `problems[]` with its row number and
reason - it is never silently dropped - and every aggregate over that file carries
`fail_closed: true`. Rows that are not trades at all (`entered: no`, a still-open
position) are *skipped*, not problems: they are correctly-recorded non-events.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from . import schema
from .normalize import looks_like_fxreplay, normalize_export

MAX_BYTES = 10_000_000
MAX_ROWS = 10_000

# canonical field -> header aliases (compared after _key(): lowercased, punctuation
# collapsed to single spaces). First matching header in the file wins.
ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "trade id", "ticket", "order id", "deal id", "position id", "trade"),
    "date": ("date", "session date", "trade date", "day date", "session"),
    "opened_at": ("opened at", "open time", "opened", "entry time", "datestart",
                  "open date", "datetime", "time", "entry datetime", "open"),
    "closed_at": ("closed at", "close time", "closed", "exit time", "dateend",
                  "close date", "exit datetime", "close"),
    "symbol": ("symbol", "pair", "instrument", "ticker", "asset", "market", "contract"),
    "side": ("side", "direction", "buy sell", "type", "action", "position", "order type"),
    "entry": ("entry", "entry price", "open price", "price in", "entryprice", "avg entry"),
    "exit": ("exit", "exit price", "close price", "price out", "avgcloseprice", "avg exit"),
    "stop": ("stop", "sl", "stop loss", "initialsl", "initial sl", "stop price"),
    "size": ("size", "volume", "lots", "lot", "lot size", "quantity", "qty",
             "contracts", "amount", "units", "shares"),
    "size_unit": ("size unit", "unit", "volume unit"),
    "r_multiple": ("r", "r multiple", "rmultiple", "result r", "r result", "rr result",
                   "avgriskreward", "risk reward realized"),
    "pnl": ("pnl", "p l", "net p l", "net pnl", "realized p l", "gross p l", "profit",
            "net profit", "result", "realized pnl", "rpnl", "profit loss", "net",
            "gross profit", "profit usd", "money"),
    "pnl_currency": ("currency", "ccy", "pnl currency", "account currency"),
    "fees": ("fees", "fee", "commission", "commissions", "swap", "costs", "cost"),
    "strategy": ("strategy", "setup", "system", "playbook", "model", "plan"),
    "tags": ("tags", "tag", "labels", "label"),
    "risk": ("risk", "risk amount", "risk per trade", "risked", "risk usd"),
    "entered": ("entered", "taken", "executed", "traded", "in trade", "did trade"),
    "status": ("status", "state", "trade status"),
}
# Fields the record itself does not carry but the mapping needs.
HELPER_FIELDS = ("stop", "risk", "entered", "status")

_UNIT_BY_HEADER = {
    "lots": "lots", "lot": "lots", "lot size": "lots",
    "contracts": "contracts",
    "shares": "shares",
}
_TRUE = {"yes", "y", "true", "1", "da", "да", "ja", "+", "x", "entered", "taken"}
_FALSE = {"no", "n", "false", "0", "net", "нет", "nein", "-", "", "skip", "skipped", "none"}
_OPEN_STATES = {"open", "opened", "running", "live", "pending", "working", "active"}


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(header).strip().lower()).strip()


def _relabel(message: str, mapping: dict[str, str]) -> str:
    """Report a bad cell under the file's own column name, not the schema field."""
    for field, column in mapping.items():
        if message.startswith(f"{field}=") and column != field:
            return column + message[len(field):]
    return message


def sniff_delimiter(sample: str) -> str:
    """Best-effort delimiter sniff over the head of a CSV; comma when unsure."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        head = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {d: head.count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] else ","


def sniff_mapping(headers: list[str]) -> dict[str, str]:
    """Map canonical fields onto the file's own headers. First match per field wins."""
    keyed = [(h, _key(h)) for h in headers]
    mapping: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        for header, key in keyed:
            if key in aliases and header not in mapping.values():
                mapping[field] = header
                break
    return mapping


def _truthy(value) -> bool | None:
    s = str(value or "").strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def _row_record(
    row: dict,
    mapping: dict[str, str],
    *,
    source: str,
    row_number: int,
    raw_ref_prefix: str | None,
    currency: str | None,
    risk_per_r: float | None,
    utc_offset_hours: int,
) -> tuple[dict | None, str | None]:
    """One raw row -> (record, skip_reason). Raises ValueError for a broken row."""
    def cell(field: str):
        col = mapping.get(field)
        return row.get(col) if col else None

    if all(schema.is_blank(v) for v in row.values()):
        return None, "blank row"

    entered = _truthy(cell("entered")) if mapping.get("entered") else None
    status = str(cell("status") or "").strip().lower()
    if entered is False:
        return None, "not entered"
    if status in _OPEN_STATES:
        return None, f"position still {status}"

    r_raw, pnl_raw = cell("r_multiple"), cell("pnl")
    if schema.is_blank(r_raw) and schema.is_blank(pnl_raw):
        if entered is True:
            raise ValueError("entry recorded but no result (pnl / r_multiple) in the row")
        raise ValueError("row has neither pnl nor r_multiple, so it cannot be aggregated")

    entry = schema.as_float(cell("entry"), "entry")
    stop = schema.as_float(cell("stop"), "stop")
    size = schema.as_float(cell("size"), "size")
    risk_amount = schema.as_float(cell("risk"), "risk")
    if risk_amount is None and None not in (entry, stop, size) and entry != stop:
        risk_amount = abs(entry - stop) * size

    unit = schema.normalize_size_unit(cell("size_unit"))
    if unit is None and mapping.get("size"):
        unit = _UNIT_BY_HEADER.get(_key(mapping["size"]))

    rec = schema.make_record(
        source=source,
        id=cell("id") if not schema.is_blank(cell("id")) else row_number,
        date=cell("date"),
        opened_at=cell("opened_at"),
        closed_at=cell("closed_at"),
        symbol=cell("symbol"),
        side=cell("side"),
        entry=entry,
        exit=cell("exit"),
        size=size,
        size_unit=unit,
        r_multiple=r_raw,
        pnl=pnl_raw,
        pnl_currency=cell("pnl_currency") or currency,
        fees=cell("fees"),
        strategy=cell("strategy"),
        tags=cell("tags"),
        raw_ref=f"{raw_ref_prefix}#{row_number}" if raw_ref_prefix else str(row_number),
        risk_amount=risk_amount,
        risk_per_r=risk_per_r,
        utc_offset_hours=utc_offset_hours,
    )
    if rec["date"] is None:
        raise ValueError("row has no session date and no timestamp to take one from")
    return rec, None


def _read_rows(path: Path) -> tuple[list[dict], list[str], str]:
    """Read a CSV/TSV/JSON journal into raw dict rows + headers + the delimiter used."""
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ValueError(f"{path.name} is {size} bytes; limit is {MAX_BYTES}")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        if isinstance(payload, dict):
            for key in ("records", "trades", "rows", "data"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            raise ValueError(f"{path.name}: expected a JSON array of trades")
        rows = [dict(r) for r in payload if isinstance(r, dict)]
        headers = list(rows[0].keys()) if rows else []
        for r in rows[1:]:
            headers += [k for k in r if k not in headers]
        if len(rows) > MAX_ROWS:
            raise ValueError(f"{path.name} has more than {MAX_ROWS} rows; refused")
        return rows, headers, "json"
    delimiter = sniff_delimiter(text[:8192])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for row in reader:
        rows.append({(k.strip() if k else k): v for k, v in row.items()})
        if len(rows) > MAX_ROWS:
            raise ValueError(f"{path.name} has more than {MAX_ROWS} rows; refused")
    return rows, headers, delimiter


def load_journal(
    path: Path,
    *,
    source: str | None = None,
    mapping: dict[str, str] | None = None,
    currency: str | None = None,
    risk_per_r: float | None = None,
    utc_offset_hours: int = 0,
) -> dict:
    """Load any journal shape into canonical records + stats + problems.

    `source` forces a shape (`fxreplay` | `csv` | `json` | any label of your own);
    omitted, the FX Replay schema is auto-detected and anything else is mapped by
    the column sniffer. `mapping` overrides the sniffer per field and must name
    columns that exist.
    """
    rows, headers, delimiter = _read_rows(path)

    if (source or "").lower() == "fxreplay" or (source is None and looks_like_fxreplay(headers)):
        records, _legacy = normalize_export(rows, utc_offset_hours, currency, path.name)
        return {
            "path": str(path),
            "source": "fxreplay",
            "delimiter": delimiter,
            "mapping_used": {"schema": "fxreplay (fixed column set)"},
            "records": records,
            "stats": schema.stats(records, []),
            "problems": [],
            "skipped": [],
            "fail_closed": False,
        }

    resolved = sniff_mapping(headers)
    if mapping:
        unknown_fields = sorted(set(mapping) - set(ALIASES))
        if unknown_fields:
            raise ValueError(
                f"mapping names fields that are not part of the journal schema: {unknown_fields}; "
                f"known: {sorted(ALIASES)}"
            )
        missing_cols = sorted({c for c in mapping.values() if c not in headers})
        if missing_cols:
            raise ValueError(f"mapping names columns absent from {path.name}: {missing_cols}; file has {headers}")
        resolved.update(mapping)

    if not ({"r_multiple", "pnl"} & set(resolved)):
        raise ValueError(
            f"{path.name}: no result column found (looked for pnl / r_multiple aliases in {headers}). "
            "Pass an explicit mapping, e.g. mapping={'pnl': 'Net P/L'}"
        )

    label = source or ("json" if delimiter == "json" else "csv")
    records: list[dict] = []
    problems: list[dict] = []
    skipped: list[dict] = []
    for i, row in enumerate(rows):
        row_number = i + 2 if delimiter != "json" else i + 1
        try:
            rec, skip = _row_record(
                row, resolved, source=label, row_number=row_number,
                raw_ref_prefix=path.name, currency=currency,
                risk_per_r=risk_per_r, utc_offset_hours=utc_offset_hours,
            )
        except ValueError as exc:
            problems.append({"row": row_number, "reason": _relabel(str(exc), resolved)})
            continue
        if rec is None:
            if skip != "blank row":
                skipped.append({"row": row_number, "reason": skip})
            continue
        records.append(rec)

    return {
        "path": str(path),
        "source": label,
        "delimiter": delimiter,
        "mapping_used": {k: v for k, v in sorted(resolved.items())},
        "records": records,
        "stats": schema.stats(records, problems),
        "problems": problems,
        "skipped": skipped,
        "fail_closed": bool(problems),
    }
