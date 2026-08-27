"""Vetted, built-in strategies for the `backtest` toolset.

Only strategies defined here (never user/LLM-supplied code) are runnable - the
sandboxed strategy extension point is a later milestone (M5). Each strategy is a
plain `backtesting.py` Strategy with class-attribute params that `tv_backtest_run`
can override.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from backtesting import Strategy
from backtesting.lib import crossover
from backtesting.test import SMA

from ..scan.detectors import smc


class SMACross(Strategy):
    """Long on fast MA crossing above slow MA; close on the reverse. No SL/TP."""

    fast = 10
    slow = 30

    def init(self) -> None:
        price = self.data.Close
        self.fast_ma = self.I(SMA, price, self.fast, name=f"SMA{self.fast}")
        self.slow_ma = self.I(SMA, price, self.slow, name=f"SMA{self.slow}")

    def next(self) -> None:
        if crossover(self.fast_ma, self.slow_ma):
            self.position.close()
            self.buy()
        elif crossover(self.slow_ma, self.fast_ma):
            self.position.close()


class Breakout(Strategy):
    """Buy a close above the prior `lookback`-bar high with an SL at the prior
    `lookback`-bar low and a TP at `rr` risk-reward; mirror for shorts on a close
    below the prior low. Sizes each position so the entry-to-SL distance risks
    `risk_amount` account units (size = risk / stop_distance). Note: a next-open gap
    can alter the realized risk vs the intended amount."""

    lookback = 20
    risk_amount = 100.0
    rr = 2.0

    def init(self) -> None:
        def _prior_high(h, lb=self.lookback):
            return pd.Series(h).rolling(lb).max().shift(1)

        def _prior_low(l, lb=self.lookback):
            return pd.Series(l).rolling(lb).min().shift(1)

        # highest high / lowest low of the prior `lookback` bars (shift 1: no look-ahead)
        self.prior_high = self.I(_prior_high, self.data.High, name="prior_high")
        self.prior_low = self.I(_prior_low, self.data.Low, name="prior_low")

    def _size(self, stop_distance: float) -> int:
        if stop_distance <= 0:
            return 0
        return max(1, round(self.risk_amount / stop_distance))

    def next(self) -> None:
        c = self.data.Close[-1]
        ph = self.prior_high[-1]
        pl = self.prior_low[-1]
        if pd.isna(ph) or pd.isna(pl):
            return

        if c > ph and not self.position:
            sl = pl
            if c - sl <= 0:
                return
            size = self._size(c - sl)
            if size == 0:
                return
            tp = c + self.rr * (c - sl)
            self.buy(size=size, sl=sl, tp=tp)
        elif c < pl and not self.position:
            sl = ph
            if sl - c <= 0:
                return
            size = self._size(sl - c)
            if size == 0:
                return
            tp = c - self.rr * (sl - c)
            self.sell(size=size, sl=sl, tp=tp)


class SMCH4M15(Strategy):
    """H4-bias / M15-FVG-retrace SMC setup (the owner's multi-timeframe scheme).

    Bias: M15 bars are resampled to H4; structure breaks (BOS, and CHoCH when
    `use_choch`) from `smartmoneyconcepts` set the direction. A break counts only
    from the CLOSE of the H4 bar that broke the level (close_break) - no
    look-ahead. Entry: an M15 Fair Value Gap in the bias direction becomes an
    active zone once its third candle has closed; when a LATER bar retraces into
    the zone (low touches the top for bullish, mirror for bearish) the strategy
    enters at next-bar-open with SL at the far edge of the gap and TP at `rr`
    times the risk. Zones die on expiry (`expiry_bars`) or when a close passes
    beyond the far edge. Sized as `risk_amount / stop_distance` like Breakout;
    a next-open gap can alter realized risk.
    """

    swing_length = 5     # H4 swing length for structure detection
    rr = 2.0
    risk_amount = 100.0
    expiry_bars = 96     # M15 zone lifetime (96 = one day)
    use_choch = True     # CHoCH flips the bias too (False: BOS only)
    min_stop_frac = 0.0003  # skip entries with stop distance < this fraction of price
                            # (tiny stops make TP land inside the next-open fill + spread)

    def init(self) -> None:
        df = self.data.df  # full frame in init; look-ahead is handled by indices below
        m15 = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        n = len(m15)

        # --- H4 bias: +1 bull / -1 bear / 0 unknown, active from the H4 close that confirmed it
        h4 = m15.resample("4h").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        bias = np.zeros(n)
        if len(h4) > 2 * self.swing_length:
            h4_pos = h4.reset_index(drop=True)  # smc wants a positional index
            swings = smc.swing_highs_lows(h4_pos, swing_length=self.swing_length)
            struct = smc.bos_choch(h4_pos, swings, close_break=True)
            events = []  # (h4 broken row, direction)
            for col in ("BOS",) + (("CHOCH",) if self.use_choch else ()):
                vals = struct[col].values
                broken = struct["BrokenIndex"].values
                for i in range(len(struct)):
                    if not pd.isna(vals[i]) and not pd.isna(broken[i]):
                        events.append((int(broken[i]), 1 if vals[i] == 1 else -1))
            if events:
                events.sort()
                # confirmation moment = END of the breaking H4 bar (index labels are opens)
                confirm = pd.Series(
                    [d for _, d in events],
                    index=[h4.index[b] + pd.Timedelta(hours=4) for b, _ in events],
                )
                confirm = confirm[~confirm.index.duplicated(keep="last")]
                bias = confirm.reindex(m15.index, method="ffill").fillna(0).to_numpy()
        self._bias = bias

        # --- M15 FVG zones, keyed by the bar that CONFIRMS them (third candle, i+1)
        fvg = smc.fvg(m15.reset_index(drop=True))
        self._zones_by_confirm: dict[int, list[dict]] = {}
        vals = fvg["FVG"].values
        for i in range(n):
            if pd.isna(vals[i]) or i + 1 >= n:
                continue
            self._zones_by_confirm.setdefault(i + 1, []).append({
                "dir": 1 if vals[i] == 1 else -1,
                "top": float(fvg["Top"].values[i]),
                "bottom": float(fvg["Bottom"].values[i]),
            })
        self._active: list[dict] = []

    def _size(self, stop_distance: float) -> int:
        if stop_distance <= 0:
            return 0
        return max(1, round(self.risk_amount / stop_distance))

    def next(self) -> None:
        j = len(self.data) - 1
        for z in self._zones_by_confirm.get(j, []):
            self._active.append({**z, "born": j})

        c = self.data.Close[-1]
        low, high = self.data.Low[-1], self.data.High[-1]

        # expire / invalidate zones (a close beyond the far edge kills the gap)
        self._active = [
            z for z in self._active
            if j - z["born"] <= self.expiry_bars
            and not (z["dir"] == 1 and c < z["bottom"])
            and not (z["dir"] == -1 and c > z["top"])
        ]

        if self.position:
            return
        bias = self._bias[j]
        if bias == 0:
            return

        floor = self.min_stop_frac * c
        for z in list(self._active):
            if z["dir"] != bias or z["born"] == j:  # retrace must be AFTER confirmation bar
                continue
            if bias == 1 and low <= z["top"]:
                sl = z["bottom"]
                dist = c - sl
                size = self._size(dist)
                if size == 0 or dist < floor:
                    continue
                try:
                    self.buy(size=size, sl=sl, tp=c + self.rr * dist)
                except ValueError:
                    continue  # next-open fill (plus spread) gapped outside the SL/TP bracket
                self._active.remove(z)
                return
            if bias == -1 and high >= z["bottom"]:
                sl = z["top"]
                dist = sl - c
                size = self._size(dist)
                if size == 0 or dist < floor:
                    continue
                try:
                    self.sell(size=size, sl=sl, tp=c - self.rr * dist)
                except ValueError:
                    continue
                self._active.remove(z)
                return


STRATEGIES: dict[str, type[Strategy]] = {
    "sma_cross": SMACross,
    "breakout": Breakout,
    "smc_h4_m15": SMCH4M15,
}
