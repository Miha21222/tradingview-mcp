"""Parquet OHLCV cache: one file per (provider, symbol, timeframe), deduped on timestamp.

All bar DataFrames use the columns: time (UTC datetime64), open, high, low, close, volume.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

BAR_COLUMNS = ["time", "open", "high", "low", "close", "volume"]


class BarCache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, provider: str, symbol: str, timeframe: str) -> Path:
        return self.root / provider / f"{symbol}_{timeframe}.parquet"

    def load(self, provider: str, symbol: str, timeframe: str) -> pd.DataFrame:
        p = self._path(provider, symbol, timeframe)
        if not p.exists():
            return pd.DataFrame(columns=BAR_COLUMNS)
        return pd.read_parquet(p)

    def store(
        self, provider: str, symbol: str, timeframe: str, bars: pd.DataFrame
    ) -> pd.DataFrame:
        """Merge new bars into the cache; returns the full merged frame."""
        if bars.empty:
            return self.load(provider, symbol, timeframe)
        bars = bars[BAR_COLUMNS].copy()
        bars["time"] = pd.to_datetime(bars["time"], utc=True)
        existing = self.load(provider, symbol, timeframe)
        if not existing.empty:
            existing["time"] = pd.to_datetime(existing["time"], utc=True)
            merged = pd.concat([existing, bars], ignore_index=True)
        else:
            merged = bars
        merged = (
            merged.drop_duplicates(subset="time", keep="last")
            .sort_values("time")
            .reset_index(drop=True)
        )
        p = self._path(provider, symbol, timeframe)
        p.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(p, index=False)
        return merged

    def slice(
        self,
        provider: str,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        df = self.load(provider, symbol, timeframe)
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"], utc=True)
        if start is not None:
            df = df[df["time"] >= start]
        if end is not None:
            df = df[df["time"] <= end]
        return df.reset_index(drop=True)
