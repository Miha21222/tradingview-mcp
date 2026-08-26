"""Pure DataFrame-level adapters over `smartmoneyconcepts` (pinned 0.0.27).

Each adapter takes a bars DataFrame (columns: time, open, high, low, close, volume)
and returns a normalized, JSON-ready result. The library emits *positional* row
indices (e.g. MitigatedIndex, BrokenIndex, Swept); these are mapped back to bar
timestamps here so output is round-trippable and feed-independent.

These are pure functions - no network, no I/O - so they are regression-testable
against labeled fixtures in tests/fixtures/labeled_setups/.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

# Suppress the library's ASCII-art credit banner on import. Must be set before the
# import (also avoids cp1251 console encoding errors on Windows).
os.environ.setdefault("SMC_CREDIT", "0")
from smartmoneyconcepts import smc  # noqa: E402

_BAR_COLS = ["time", "open", "high", "low", "close", "volume"]


def _as_smc(df: pd.DataFrame) -> pd.DataFrame:
    """Return lowercase ohlcv columns with a clean RangeIndex (positional)."""
    return df[["open", "high", "low", "close", "volume"]].copy().reset_index(drop=True)


def _times(df: pd.DataFrame) -> list[str]:
    return [t.isoformat().replace("+00:00", "Z") for t in pd.to_datetime(df["time"], utc=True)]


def _idx_time(times: list[str], i: float) -> str | None:
    """Map a positional index to a timestamp, guarding NaN / out-of-range."""
    if i is None or pd.isna(i):
        return None
    i = int(i)
    if i < 0 or i >= len(times):
        return None
    return times[i]


def _swings(df: pd.DataFrame, swing_length: int) -> pd.DataFrame:
    return smc.swing_highs_lows(_as_smc(df), swing_length=swing_length)


def _dir(v: float) -> str:
    return "bullish" if v == 1 else "bearish"


def scan_fvg(df: pd.DataFrame, join_consecutive: bool = False) -> list[dict]:
    """Fair Value Gaps: bullish when prev high < next low on an up candle, and vice versa."""
    times = _times(df)
    res = smc.fvg(_as_smc(df), join_consecutive=join_consecutive)
    out = []
    fvg = res["FVG"].values
    for i in range(len(res)):
        if pd.isna(fvg[i]):
            continue
        mi = res["MitigatedIndex"].values[i]
        mitigated = not pd.isna(mi) and mi != 0
        out.append(
            {
                "index": int(i),
                "time": times[i],
                "direction": _dir(fvg[i]),
                "top": round(float(res["Top"].values[i]), 6),
                "bottom": round(float(res["Bottom"].values[i]), 6),
                "mitigated_index": int(mi) if mitigated else None,
                "mitigated_time": _idx_time(times, mi) if mitigated else None,
            }
        )
    return out


def scan_ob(
    df: pd.DataFrame, swing_length: int = 5, close_mitigation: bool = False
) -> list[dict]:
    """Order blocks: last down-candle before a bullish breakout (and mirror)."""
    times = _times(df)
    res = smc.ob(_as_smc(df), _swings(df, swing_length), close_mitigation=close_mitigation)
    out = []
    ob = res["OB"].values
    for i in range(len(res)):
        if pd.isna(ob[i]):
            continue
        mi = res["MitigatedIndex"].values[i]
        pct = res["Percentage"].values[i]
        mitigated = not pd.isna(mi) and mi != 0
        out.append(
            {
                "index": int(i),
                "time": times[i],
                "direction": _dir(ob[i]),
                "top": round(float(res["Top"].values[i]), 6),
                "bottom": round(float(res["Bottom"].values[i]), 6),
                "strength_pct": round(float(pct), 2) if not pd.isna(pct) else None,
                "mitigated_index": int(mi) if mitigated else None,
                "mitigated_time": _idx_time(times, mi) if mitigated else None,
            }
        )
    return out


def scan_structure(
    df: pd.DataFrame, swing_length: int = 5, close_break: bool = True
) -> list[dict]:
    """Break of Structure (BOS) and Change of Character (CHoCH)."""
    times = _times(df)
    res = smc.bos_choch(_as_smc(df), _swings(df, swing_length), close_break=close_break)
    out = []
    bos = res["BOS"].values
    choch = res["CHOCH"].values
    for i in range(len(res)):
        kind, d = None, None
        if not pd.isna(bos[i]):
            kind, d = "bos", _dir(bos[i])
        elif not pd.isna(choch[i]):
            kind, d = "choch", _dir(choch[i])
        if kind is None:
            continue
        br = res["BrokenIndex"].values[i]
        out.append(
            {
                "index": int(i),
                "time": times[i],
                "kind": kind,
                "direction": d,
                "level": round(float(res["Level"].values[i]), 6),
                "broken_index": int(br) if not pd.isna(br) else None,
                "broken_time": _idx_time(times, br) if not pd.isna(br) else None,
            }
        )
    return out


def scan_liquidity(
    df: pd.DataFrame, swing_length: int = 5, range_percent: float = 0.01
) -> list[dict]:
    """Liquidity pools: multiple swing highs (or lows) clustered within a small range."""
    times = _times(df)
    res = smc.liquidity(
        _as_smc(df), _swings(df, swing_length), range_percent=range_percent
    )
    out = []
    liq = res["Liquidity"].values
    for i in range(len(res)):
        if pd.isna(liq[i]):
            continue
        end = res["End"].values[i]
        swept = res["Swept"].values[i]
        swept_ok = not pd.isna(swept) and swept != 0
        out.append(
            {
                "index": int(i),
                "time": times[i],
                "direction": _dir(liq[i]),
                "level": round(float(res["Level"].values[i]), 6),
                "end_index": int(end) if not pd.isna(end) else None,
                "end_time": _idx_time(times, end) if not pd.isna(end) else None,
                "swept_index": int(swept) if swept_ok else None,
                "swept_time": _idx_time(times, swept) if swept_ok else None,
            }
        )
    return out


def scan_sessions(
    df: pd.DataFrame,
    session: str,
    start_time: str = "",
    end_time: str = "",
    time_zone: str = "UTC",
) -> dict:
    """Contiguous killzone/session blocks with each block's high/low (dealing range)."""
    index = pd.to_datetime(df["time"], utc=True)
    ohlc = df.set_index(index)[["open", "high", "low", "close", "volume"]]
    times = _times(df)
    res = smc.sessions(ohlc, session, start_time, end_time, time_zone)
    active = res["Active"].values

    blocks = []
    start = None
    for i in range(len(active)):
        if active[i] == 1 and start is None:
            start = i
        elif active[i] == 0 and start is not None:
            blocks.append((start, i - 1))
            start = None
    if start is not None:
        blocks.append((start, len(active) - 1))

    out_blocks = []
    for s, e in blocks:
        out_blocks.append(
            {
                "start_time": times[s],
                "end_time": times[e],
                "count": e - s + 1,
                "high": round(float(np.nanmax(res["High"].values[s : e + 1])), 6),
                "low": round(float(np.nanmin(res["Low"].values[s : e + 1])), 6),
            }
        )
    return {"session": session, "time_zone": time_zone, "blocks": out_blocks}


def scan_prev_hl(df: pd.DataFrame, time_frame: str = "1D") -> dict:
    """Previous high/low of `time_frame` (e.g. prev day H/L on M15 bars) + broken flags."""
    index = pd.to_datetime(df["time"], utc=True)
    ohlc = df.set_index(index)[["open", "high", "low", "close", "volume"]]
    times = _times(df)
    res = smc.previous_high_low(ohlc, time_frame=time_frame)
    last = len(res) - 1
    ph = res["PreviousHigh"].values[last]
    pl = res["PreviousLow"].values[last]
    return {
        "time_frame": time_frame,
        "as_of": times[last],
        "previous_high": round(float(ph), 6) if not pd.isna(ph) else None,
        "previous_low": round(float(pl), 6) if not pd.isna(pl) else None,
        "broken_high": bool(res["BrokenHigh"].values[last] == 1),
        "broken_low": bool(res["BrokenLow"].values[last] == 1),
    }
