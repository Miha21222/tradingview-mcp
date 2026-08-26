"""OANDA v20 REST candles (practice or live account; free practice keys at oanda.com).

Mid prices by default. Real broker feed: spreads and session opens match what a
retail FX trader actually gets filled at.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pandas as pd

from ..cache import BAR_COLUMNS
from ..symbols import Symbol, Timeframe
from .base import DataProviderError, ProviderStatus

_HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}
_MAX_COUNT = 5000  # OANDA per-request cap


class OandaProvider:
    name = "oanda"

    def __init__(self, api_key: str | None, env: str = "practice"):
        self.api_key = api_key
        self.env = env if env in _HOSTS else "practice"

    def status(self) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(
                self.name, False, "OANDA_API_KEY not set - create a free practice account to enable"
            )
        return ProviderStatus(self.name, True, f"ready ({self.env})")

    def fetch(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
        count: int | None = None,
    ) -> pd.DataFrame:
        if not self.api_key:
            raise DataProviderError("OANDA_API_KEY not set; set it or use provider='dukascopy'")
        if symbol.oanda is None:
            raise DataProviderError(
                f"{symbol.canonical} has no OANDA mapping; extend src/tvmcp/symbols.py"
            )
        if timeframe.oanda is None:
            raise DataProviderError(f"Timeframe {timeframe.canonical} unsupported by OANDA")

        params: dict[str, str] = {"granularity": timeframe.oanda, "price": "M"}
        if start is not None:
            params["from"] = start.isoformat().replace("+00:00", "Z")
            if end is not None:
                params["to"] = end.isoformat().replace("+00:00", "Z")
            else:
                params["count"] = str(min(count or _MAX_COUNT, _MAX_COUNT))
        else:
            params["count"] = str(min(count or 500, _MAX_COUNT))

        url = f"{_HOSTS[self.env]}/v3/instruments/{symbol.oanda}/candles"
        try:
            resp = httpx.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise DataProviderError(f"OANDA request failed: {exc}") from exc
        if resp.status_code == 401:
            raise DataProviderError("OANDA rejected the API key (401); check OANDA_API_KEY/OANDA_ENV")
        if resp.status_code != 200:
            raise DataProviderError(f"OANDA HTTP {resp.status_code}: {resp.text[:200]}")

        rows = []
        for c in resp.json().get("candles", []):
            if not c.get("complete", True):
                continue
            mid = c["mid"]
            rows.append(
                {
                    "time": pd.to_datetime(c["time"], utc=True),
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": float(c.get("volume", 0)),
                }
            )
        return pd.DataFrame(rows, columns=BAR_COLUMNS)
