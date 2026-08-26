---
name: strategy-backtesting
description: "Backtest built-in and declarative strategies with correct forex math (R sizing, pip/JPY, spread, fill model). Use when validating a rule-based idea on historical data, sizing a strategy, or interpreting backtest stats. Triggers: backtest, run the strategy, validate this idea, what's the win rate, walk-forward."
---

# Strategy backtesting

Run vetted strategies over historical bars with account-correct math: pip sizes
(incl. JPY), risk-based position sizing from the actual stop distance, pip-scaled
spread, and explicit fill semantics. **Strategies are declarative YAML or built-in
only** — never run user/LLM Python code (blocked by the hard rules).

## When to use

- You want a statistical read (win rate, expectancy, profit factor, R distribution)
  on a rule-based idea, not a gut feeling.
- You need to compare a strategy across parameters or timeframes.
- You have a declarative spec and want to run it over fresh data.

## Tools

- `tv_backtest_run` — run a built-in strategy (`sma_cross`, `breakout`) with full
  currency/spread/fill controls.
- `tv_strategy_list` — list declarative YAML strategies in `TV_STRATEGY_DIR`.
- `tv_strategy_run` — run a declarative YAML strategy (same engine as backtest).
- `tv_data_get_bars` — fetch the bars you intend to test.

## Honest interpretation checklist

1. **Fill model**: default is next-bar-open (`trade_on_close=false`). Trades that
   signal on a bar fill at the *next* open — this is the conservative real-world
   default. `trade_on_close=true` fills at the signal bar close (optimistic; only
   if your execution genuinely works that way).
2. **Look-ahead**: ensure the strategy only used information available at the signal
   bar (the built-ins shift their indicators by one bar). Never test a rule that
   references the outcome bar.
3. **Sample size**: per setup, fewer than ~30 trades is not evidence. Report
   `# Trades` with every conclusion; a 100% win rate on 4 trades is noise.
4. **Currency**: `account_currency` and `quote_to_account_rate` are explicit. When
   the quote (e.g. JPY) differs from the account currency, supply the rate — the
   tool refuses to guess.
5. **Spread**: `spread_pips` is modeled as a relative rate exact at a reference
   price; it is applied once at entry (backtesting.py semantics). Treat reported
   P&L as pre-tolerance, not a broker quote.
6. **R**: each trade's `r` = `pnl / (size * |entry - sl|)`; a stop-hit is ~`-1.0R`.
   Judge a strategy by expectancy in R, not by total P&L (account size is arbitrary).

## Workflow

1. Fetch bars: `tv_data_get_bars(symbol, timeframe, count)` (or let the tool).
2. Run: `tv_backtest_run(strategy, symbol, timeframe, count, spread_pips, cash, ...)`.
3. Read `summary` (win rate, profit factor, expectancy, max drawdown) and the capped
   recent `trades` (each with direction, size, units, R, session).
4. Compare against a baseline (e.g. `sma_cross`); a strategy is only "better" with
   enough trades and a spread/commission-realistic expectancy above the baseline.
5. For a reusable idea, save a YAML spec (see `tv_strategy_list`) and re-run via
   `tv_strategy_run` instead of repeating parameters.

## Declarative YAML spec

```yaml
name: my_breakout
description: breakout, wider RR
strategy: breakout        # must be a vetted built-in
params:                   # class-attribute params of that strategy
  lookback: 25
  rr: 3.0
  risk_amount: 150
```

`name` and `symbol` are managed by the tool — do not set them in `params`.

## References

- `references/walk-forward.md` — in-sample/out-of-sample discipline.
- Pair with `risk-sizing` for position-size decisions and `strategy-review` for the
  owner's 5-block audit of the strategy definition itself.