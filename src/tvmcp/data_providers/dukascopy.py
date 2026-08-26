"""Dukascopy historical bars via the `dukascopy-node` CLI (Node.js, free tick-era data).

First fetch of a range is slow (Dukascopy serves hourly .bi5 files); results are
merged into the Parquet cache, so repeat queries are instant.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ..cache import BAR_COLUMNS
from ..symbols import Symbol, Timeframe
from .base import DataProviderError, ProviderStatus

_TIMEOUT_S = 300


def _npx() -> str | None:
    return shutil.which("npx")


class DukascopyProvider:
    name = "dukascopy"

    def status(self) -> ProviderStatus:
        npx = _npx()
        if npx is None:
            return ProviderStatus(
                self.name, False, "npx not found on PATH - install Node.js (winget install OpenJS.NodeJS.LTS) to enable Dukascopy"
            )
        return ProviderStatus(self.name, True, "ready (npx dukascopy-node)")

    def fetch(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        if symbol.dukascopy is None:
            raise DataProviderError(
                f"{symbol.canonical} has no Dukascopy mapping; use provider='oanda' "
                "or extend src/tvmcp/symbols.py"
            )
        if timeframe.dukascopy is None:
            raise DataProviderError(f"Timeframe {timeframe.canonical} unsupported by Dukascopy")
        npx = _npx()
        if npx is None:
            raise DataProviderError(
                "npx not found on PATH - install Node.js (winget install "
                "OpenJS.NodeJS.LTS), restart the terminal, and retry; or run "
                "tv_setup_doctor for a full diagnosis"
            )

        # dukascopy-node's `to` is exclusive of the final day for intraday data;
        # pad by one day so the requested end date is included.
        to = end + timedelta(days=1)
        with tempfile.TemporaryDirectory(prefix="tvmcp_duk_") as tmp:
            cmd = [
                npx,
                "--yes",
                "dukascopy-node",
                "-i", symbol.dukascopy,
                "-from", start.isoformat(),
                "-to", to.isoformat(),
                "-t", timeframe.dukascopy,
                "-f", "csv",
                "-dir", tmp,
                "--cache",
            ]
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
                )
            except subprocess.TimeoutExpired as exc:
                raise DataProviderError(
                    f"dukascopy-node timed out after {_TIMEOUT_S}s; narrow the date range"
                ) from exc
            csv_files = list(Path(tmp).glob("*.csv"))
            if proc.returncode != 0 or not csv_files:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                raise DataProviderError(
                    "dukascopy-node produced no data for "
                    f"{symbol.canonical} {timeframe.canonical} {start}..{end}: "
                    + " | ".join(tail)
                )
            return _read_csv(csv_files[0])


def _read_csv(path: Path) -> pd.DataFrame:
    rows = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(
                {
                    "time": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r.get("volume") or 0.0),
                }
            )
    return pd.DataFrame(rows, columns=BAR_COLUMNS)
