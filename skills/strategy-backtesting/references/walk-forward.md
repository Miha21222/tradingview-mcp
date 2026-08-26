# Walk-forward discipline

Backtest results are only trustworthy if the test set was never seen by the rule.

## Rules of thumb

- Split history into in-sample (fit params) and out-of-sample (verify). A strategy
  whose edge only exists in-sample is a curve-fit.
- Walk-forward: optimize on a rolling window, then validate the next window forward.
- Report the **out-of-sample** expectancy and win rate, not the tuned in-sample.
- A parameter grid that "can't lose" in-sample is the strongest red flag.

## Minimum sample

- ~30 trades per setup before any real-risk conclusion (owner's framework rule).
- 100+ trades before trusting drawdown stats.
- State the sample with every number you report.

## Look-ahead traps in this toolchain

- Built-in strategies shift rolling indicators by one bar — no look-ahead by
  construction. If you ever extend strategies declaratively, keep that invariant.
- Default fill is next-bar-open; `trade_on_close=true` is optimistic and must be
  justified by actual execution.
- Spread is charged at entry (backtesting.py model) at a reference price — P&L is
  approximate, add real brokerage tolerance before trusting an edge.

## Reporting template

```
strategy: <name>   timeframe: <tf>   bars: <n>   provider: <feed>
trades: <n>  win_rate: <x%>  expectancy: <y>R  profit_factor: <z>
drawdown: <d%>   sample: <n trades, in/out-of-sample>
```

Do not report a single number in isolation.