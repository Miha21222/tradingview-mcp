"""Vetted, built-in strategies for the `backtest` toolset.

Only strategies defined here (never user/LLM-supplied code) are runnable - the
sandboxed strategy extension point is a later milestone (M5). Each strategy is a
plain `backtesting.py` Strategy with class-attribute params that `tv_backtest_run`
can override.
"""

from __future__ import annotations

import pandas as pd
from backtesting import Strategy
from backtesting.lib import crossover
from backtesting.test import SMA


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


STRATEGIES: dict[str, type[Strategy]] = {
    "sma_cross": SMACross,
    "breakout": Breakout,
}
