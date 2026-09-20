"""`journal` toolset: watch-folder scanner, journal loader and risk guard.

The watch-folder (`TV_JOURNAL_DIR`) is scanned on-demand (`tv_journal_scan`), a
named FX Replay export is parsed into vault-mapped records + stats
(`tv_journal_parse`), a journal of **any** shape is loaded into the canonical
record schema (`tv_journal_load`, M9), and `tv_risk_guard` (M9) checks a journal
against the user's own written risk rules. No background watcher, no Notion writes,
no order placement - every tool here is read-only bookkeeping and stays registered
under `TV_READ_ONLY=1`.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import Settings
from . import risk, schema
from .loader import load_journal
from .normalize import parse_file

# Signal columns that suggest an FX Replay-style trade export. Loose on purpose:
# this is a pre-filter for `tv_journal_scan`; `tv_journal_parse` does the real
# schema check against REQUIRED_COLUMNS in normalize.py.
_SIGNAL_COLUMNS = {
    "symbol": ("symbol", "pair", "instrument", "ticker", "asset"),
    "side": ("side", "buy/sell", "direction", "type", "order type", "action"),
    "profit": ("profit", "pnl", "result", "p/l", "net", "earnings"),
    "time": ("time", "date", "open time", "close time", "entry time", "exit time", "datetime"),
}


def _normalize(headers: list[str]) -> list[str]:
    return [h.strip().lower() for h in headers]


def _sniff_csv(path: Path, max_rows: int = 5) -> tuple[list[str], int, bool]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return [], 0, False
        row_count = 0
        for _ in reader:
            row_count += 1
    headers = _normalize(header)
    lower = " ".join(headers)
    signals = {
        kind: any(k in lower for k in keys)
        for kind, keys in _SIGNAL_COLUMNS.items()
    }
    likely = all(signals.values())
    return header, row_count, likely


_LOADABLE_SUFFIXES = {".csv", ".tsv", ".txt", ".json"}


def _resolve_journal_path(settings: Settings, path_or_name: str) -> Path:
    """A bare file name resolves inside TV_JOURNAL_DIR; anything with a separator is
    taken as an explicit path (journals commonly live in a vault, not the watch-folder)."""
    raw = (path_or_name or "").strip()
    if not raw:
        raise ToolError("path_or_name is empty; pass a file name inside TV_JOURNAL_DIR or a full path")
    p = Path(raw).expanduser()
    if p.suffix.lower() not in _LOADABLE_SUFFIXES:
        raise ToolError(f"{raw!r}: expected one of {sorted(_LOADABLE_SUFFIXES)}")
    if p.is_absolute() or len(p.parts) > 1:
        target = p.resolve()
        if not target.is_file():
            raise ToolError(f"No such journal file: {target}")
        return target
    root = settings.journal_dir
    if not root.exists():
        raise ToolError(
            f"Journal folder {root} does not exist; set TV_JOURNAL_DIR or pass a full path"
        )
    target = (root / raw).resolve()
    if target.parent != root.resolve():
        raise ToolError(f"{raw!r} is not a file directly inside {root}")
    if not target.is_file():
        raise ToolError(f"No such file in journal folder: {raw}")
    return target


def _records_from_input(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Normalize caller-supplied records; unusable ones become problems, not silence."""
    out: list[dict] = []
    problems: list[dict] = []
    for i, raw in enumerate(records):
        if not isinstance(raw, dict):
            problems.append({"row": i + 1, "reason": "record is not an object"})
            continue
        try:
            rec = schema.make_record(
                source=raw.get("source") or "inline",
                **{k: raw.get(k) for k in schema.FIELDS if k not in ("source", "derived")},
                derived=raw.get("derived") or [],
            )
        except ValueError as exc:
            problems.append({"row": i + 1, "reason": str(exc)})
            continue
        if not schema.has_result(rec):
            problems.append({"row": i + 1, "reason": "record carries neither pnl nor r_multiple"})
            continue
        out.append(rec)
    return out, problems


def register(mcp: Any, settings: Settings) -> None:
    @mcp.tool(tags={"journal"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_journal_scan(
        limit: Annotated[int, Field(description="Max files to inspect", ge=1, le=100)] = 25,
    ) -> dict:
        """List CSV files in the journal watch-folder and sniff their columns.

        On-demand only (no watcher). Reports each file's column headers, row count
        (excluding header), and whether it looks like an FX Replay export. No schema
        normalization happens until a genuine export is supplied - this is a parser
        boundary, not a guesser.
        """
        root = settings.journal_dir
        if not root.exists():
            raise ToolError(
                f"Journal folder {root} does not exist; set TV_JOURNAL_DIR to your "
                "FX Replay CSV export folder"
            )
        files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
        if not files:
            return {"journal_dir": str(root), "files": [], "note": "no CSV files found"}

        out = []
        for p in files[:limit]:
            try:
                header, rows, likely = _sniff_csv(p)
            except (OSError, csv.Error) as exc:
                raise ToolError(f"Could not read {p.name}: {exc}") from exc
            out.append({
                "name": p.name,
                "rows": rows,
                "columns": header,
                "fxreplay_likely": likely,
            })
        return {
            "journal_dir": str(root),
            "count": len(out),
            "total_csv_files": len(files),
            "files": out,
        }

    @mcp.tool(tags={"journal"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_journal_parse(
        filename: Annotated[str, Field(description="CSV file name inside the journal watch-folder to parse")],
        utc_offset_hours: Annotated[int, Field(description="Hours the export's timestamps are ahead of UTC; 0 if already UTC", ge=-14, le=14)] = 0,
    ) -> dict:
        """Parse + normalize an FX Replay CSV export into structured journal records + summary.

        Vault-note mappings: buy/sell -> long/short, weekday code -> day name, entry
        UTC hour -> session/killzone. `pair` is resolved through symbols.py. Read-only
        (no Notion writes); summary includes win rate, expectancy, profit factor and
        per-session/day/side breakdowns.
        """
        root = settings.journal_dir
        if not root.exists():
            raise ToolError(
                f"Journal folder {root} does not exist; set TV_JOURNAL_DIR to your "
                "FX Replay CSV export folder"
            )
        root_res = root.resolve()
        target = (root / filename).resolve()
        # exact resolved-child check: filename must be a direct .csv file inside root
        if target.suffix.lower() != ".csv" or target.parent != root_res:
            raise ToolError(f"{filename!r} is not a CSV file directly inside {root}")
        if not target.exists():
            raise ToolError(f"No such file in journal folder: {filename}")
        try:
            records, summary = parse_file(target, utc_offset_hours)
        except (ValueError, csv.Error) as exc:
            raise ToolError(f"Could not parse {filename}: {exc}") from exc
        total = len(records)
        shown = records[-100:]
        return {
            "filename": filename,
            "journal_dir": str(root),
            "total_count": total,
            "returned_count": len(shown),
            "truncated": total > 100,
            "trades": total,
            "summary": summary,
            "records": shown,
        }

    @mcp.tool(tags={"journal"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_journal_load(
        path_or_name: Annotated[str, Field(description="Journal file: a name inside TV_JOURNAL_DIR, or a full path to a .csv/.tsv/.json")],
        source: Annotated[str | None, Field(description="Force a shape: 'fxreplay' for an FX Replay export, or any label for a generic file. Omitted = auto-detect")] = None,
        mapping: Annotated[dict[str, str] | None, Field(description="Column overrides for odd exports, e.g. {'pnl': 'Net P/L', 'date': 'Session'}. Fields: id,date,opened_at,closed_at,symbol,side,entry,exit,stop,size,size_unit,r_multiple,pnl,pnl_currency,fees,strategy,tags,risk,entered,status")] = None,
        currency: Annotated[str | None, Field(description="Account currency to label money with when the file does not name one")] = None,
        risk_per_r: Annotated[float | None, Field(description="What 1R is worth in account currency; lets R be derived from money (or money from R) - never guessed without it")] = None,
        utc_offset_hours: Annotated[int, Field(description="Hours the file's naive timestamps are ahead of UTC; 0 if already UTC", ge=-14, le=14)] = 0,
        limit: Annotated[int, Field(description="Max records to return (stats always cover the whole file)", ge=1, le=1000)] = 100,
    ) -> dict:
        """Load a trade journal of any shape into one normalized record schema + stats.

        Handles the three shapes that exist in practice: FX Replay exports (fixed
        schema, auto-detected), a workflow's own CSV (one row per session, an
        `entered` yes/no column, a result in R) and broker/platform exports
        (whatever the platform called its columns). The delimiter and the columns
        are sniffed; `mapping` overrides the sniffer.

        Every record carries: id, date (session date), opened_at, closed_at, symbol,
        side, entry, exit, size, size_unit, r_multiple, pnl, pnl_currency, fees,
        strategy, tags, source, raw_ref and `derived` (which numbers were computed
        rather than read). `r_multiple` and `pnl` may each be missing; one is derived
        from the other only when the file carries a risk column/stop distance or you
        pass `risk_per_r` - nothing is invented.

        A row that cannot be parsed is never dropped silently: it lands in
        `problems[]` with its row number and reason, and `fail_closed` turns true -
        aggregates over a broken journal must not read as reassuring. Rows that are
        not trades (`entered: no`, a still-open position) are listed under `skipped`.
        """
        path = _resolve_journal_path(settings, path_or_name)
        try:
            result = load_journal(
                path, source=source, mapping=mapping, currency=currency,
                risk_per_r=risk_per_r, utc_offset_hours=utc_offset_hours,
            )
        except (ValueError, csv.Error, json.JSONDecodeError) as exc:
            raise ToolError(f"Could not load {path.name}: {exc}") from exc
        records = result["records"]
        shown = records[-limit:]
        return {
            **result,
            "records": [schema.to_canonical(r) for r in shown],
            "total_count": len(records),
            "returned_count": len(shown),
            "truncated": len(records) > len(shown),
        }

    @mcp.tool(tags={"journal"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_risk_guard(
        journal: Annotated[str | None, Field(description="Journal file to check: a name inside TV_JOURNAL_DIR or a full path. Omit when passing `records`")] = None,
        records: Annotated[list[dict] | None, Field(description="Already-normalized records (tv_journal_load output) instead of a file")] = None,
        rules: Annotated[dict | None, Field(description="Rules config object (see risk_rules.example.json). Either this or rules_path")] = None,
        rules_path: Annotated[str | None, Field(description="Path to a JSON rules config. Omit both and the shipped generic example is used, with a warning")] = None,
        today: Annotated[str | None, Field(description="Session date 'YYYY-MM-DD' (or a full ISO timestamp, which also enables the minute-based rest check)")] = None,
        account: Annotated[dict | None, Field(description="{balance, starting_balance, balance_asof, currency, risk_per_r, floor | max_loss_pct, floor_type: 'static'|'trailing', high_water_mark}")] = None,
        source: Annotated[str | None, Field(description="Force the journal shape when loading a file (see tv_journal_load)")] = None,
        mapping: Annotated[dict[str, str] | None, Field(description="Column overrides when loading a file (see tv_journal_load)")] = None,
    ) -> dict:
        """Check a journal against the user's own written risk rules. Bookkeeping, not advice.

        This is arithmetic over trades the user already recorded: loss limits per
        day / week / month, trades and losing trades per day, a consecutive-loss
        cooldown counted in sessions, the room left above an account floor (static or
        trailing), a rest period after a stop-out, an idle warning and an optional
        profit-side guard. It never suggests what to trade, never sizes a position and
        holds no thresholds of its own - every number comes from `rules` and `account`.
        It is not financial advice and not a broker connection.

        `status` is the worst check: `stop` = one of your rules was breached, `flag` =
        a soft rule fired, `ok` = nothing fired. A check that cannot be evaluated
        (no account size, no currency, mixed units) reports `unknown` with the reason
        in `warnings` and lifts `status` to `flag` rather than reading as `ok`. An
        unparseable journal row forces `stop` through the `journal_integrity` check.

        Limits are written as {"pct": x} (percent of the account), {"currency": x} or
        {"r": x}; a loss that reaches the limit exactly breaches it. Count limits use
        "max" = the largest allowed count. See `risk_rules.example.json` in this
        package for a documented config.
        """
        if bool(journal) == bool(records is not None):
            raise ToolError("pass exactly one of `journal` (a file) or `records` (already normalized)")
        if not today:
            raise ToolError(
                "today is required: pass the session date as 'YYYY-MM-DD' (or a full ISO "
                "timestamp) - the guard never guesses which session you mean"
            )
        try:
            resolved_rules, warnings = risk.load_rules(rules, rules_path)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ToolError(f"Could not read the rules config: {exc}") from exc

        if journal:
            path = _resolve_journal_path(settings, journal)
            try:
                loaded = load_journal(
                    path, source=source, mapping=mapping,
                    currency=(account or {}).get("currency"),
                    risk_per_r=(account or {}).get("risk_per_r"),
                )
            except (ValueError, csv.Error, json.JSONDecodeError) as exc:
                raise ToolError(f"Could not load {path.name}: {exc}") from exc
            recs, problems = loaded["records"], loaded["problems"]
            origin = {"journal": str(path), "source": loaded["source"],
                      "mapping_used": loaded["mapping_used"], "skipped": len(loaded["skipped"])}
        else:
            recs, problems = _records_from_input(records or [])
            origin = {"journal": None, "source": "inline", "records_in": len(records or [])}

        try:
            result = risk.evaluate(
                recs, resolved_rules, today=today, account=account,
                fail_closed=bool(problems), problems=problems,
            )
        except ValueError as exc:
            raise ToolError(f"Could not evaluate the risk rules: {exc}") from exc
        result["warnings"] = warnings + result["warnings"]
        result["problems"] = problems
        result["origin"] = origin
        return result