"""Symbol-mapping layer.

Feeds disagree on naming (TradingView `OANDA:EURUSD`, Dukascopy `eurusd`,
OANDA v20 `EUR_USD`). All tools accept any alias; this module canonicalizes.
Extend mappings here, nowhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Currencies recognized as FX legs for 6-letter pair detection.
_FX_CCY = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "TRY", "ZAR",
    "MXN", "SGD", "HKD", "CNH",
}

_METALS = {"XAU", "XAG"}  # quoted like FX pairs on most feeds


@dataclass(frozen=True)
class Symbol:
    canonical: str        # "EURUSD"
    kind: str             # "fx" | "metal" | "other"
    tv: str               # "OANDA:EURUSD"
    dukascopy: str | None  # "eurusd"
    oanda: str | None      # "EUR_USD"


def resolve(raw: str) -> Symbol:
    """Resolve any alias to a Symbol. Non-FX symbols pass through as kind='other'."""
    s = raw.strip()
    exchange = None
    if ":" in s:
        exchange, s = s.split(":", 1)
        exchange = exchange.strip().upper()
    s = s.strip().upper().replace("/", "").replace("_", "").replace("-", "")

    if len(s) == 6:
        base, quote = s[:3], s[3:]
        if base in _FX_CCY and quote in _FX_CCY:
            return Symbol(
                canonical=s,
                kind="fx",
                tv=f"{exchange or 'OANDA'}:{s}",
                dukascopy=s.lower(),
                oanda=f"{base}_{quote}",
            )
        if base in _METALS and quote in _FX_CCY:
            return Symbol(
                canonical=s,
                kind="metal",
                tv=f"{exchange or 'OANDA'}:{s}",
                dukascopy=s.lower(),
                oanda=f"{base}_{quote}",
            )

    # Unknown instrument: keep whatever the caller gave us, TV-style.
    tv = f"{exchange}:{s}" if exchange else s
    return Symbol(canonical=s, kind="other", tv=tv, dukascopy=None, oanda=None)


_TF_RE = re.compile(r"^(?P<n>\d+)?\s*(?P<u>[a-zA-Z]+)$")

# canonical timeframe -> (minutes, dukascopy code, oanda granularity)
_TIMEFRAMES: dict[str, tuple[int, str | None, str | None]] = {
    "M1": (1, "m1", "M1"),
    "M5": (5, "m5", "M5"),
    "M15": (15, "m15", "M15"),
    "M30": (30, "m30", "M30"),
    "H1": (60, "h1", "H1"),
    "H4": (240, "h4", "H4"),
    "D1": (1440, "d1", "D"),
}

_ALIASES = {
    "1": "M1", "1M": "M1", "1MIN": "M1", "M1": "M1",
    "5": "M5", "5M": "M5", "5MIN": "M5", "M5": "M5",
    "15": "M15", "15M": "M15", "15MIN": "M15", "M15": "M15",
    "30": "M30", "30M": "M30", "30MIN": "M30", "M30": "M30",
    "60": "H1", "1H": "H1", "H1": "H1",
    "240": "H4", "4H": "H4", "H4": "H4",
    "D": "D1", "1D": "D1", "D1": "D1", "DAILY": "D1",
}


@dataclass(frozen=True)
class Timeframe:
    canonical: str  # "M15"
    minutes: int
    dukascopy: str | None
    oanda: str | None


def resolve_timeframe(raw: str) -> Timeframe:
    key = raw.strip().upper().replace(" ", "")
    canonical = _ALIASES.get(key)
    if canonical is None:
        raise ValueError(
            f"Unknown timeframe {raw!r}. Supported: "
            + ", ".join(sorted(_TIMEFRAMES, key=lambda k: _TIMEFRAMES[k][0]))
        )
    minutes, duk, oanda = _TIMEFRAMES[canonical]
    return Timeframe(canonical=canonical, minutes=minutes, dukascopy=duk, oanda=oanda)
