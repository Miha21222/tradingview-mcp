"""`journal` toolset: on-demand FX Replay watch-folder scanner + CSV normalizer.

The watch-folder (`TV_JOURNAL_DIR`) is scanned on-demand (`tv_journal_scan`), and a
named export is parsed into vault-mapped records + stats (`tv_journal_parse`). The
normalizer is built against a genuine FX Replay export (sanitized fixture in
tests/fixtures/journal/). No background watcher, no Notion writes - normalization is
read-only and feeds the owner's own Notion MCP later.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import Settings
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