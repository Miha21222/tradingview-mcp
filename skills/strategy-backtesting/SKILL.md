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

- `tv_backtest_run` — run a built-in strategy (`sma_cross`, `breakout`, `smc_h4_m15`) with full
  currency/spread/fill controls.
- `tv_backtest_render_trades` — same backtest, but renders the most recent N closed
  trades to PNGs (entry line, SL red, TP green, exit labeled with R, band over the
  trade's lifetime) for a visual sanity check of the fills; needs headless Chromium.
  `extra_markup_json` layers custom drawings (killzones, boxes, text, markers,
  hex colors — full tv_chart_render schema) onto every image.
- Fully custom screenshots: `tv_backtest_run` gives every trade's entry/exit
  times + prices + sl/tp; feed them to `tv_chart_render` with `end_time` (window
  any historical trade) and your own `markup_json`. A user's preferred style
  (colors, what to draw, sizes) belongs in a skill file so the agent applies it
  every time — that's the customization path, no code changes.
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

## Benchmark and sanity gates (apply to every result you report)

Habits distilled from the honest minority of public "AI trading" setups — the
ones that showed their strategy losing. Run them before calling anything good:

1. **Buy-and-hold first.** Every result is quoted next to the same period's
   buy-and-hold return (and its drawdown). Compute it from the bars you tested;
   on the user's TradingView chart `tv_desktop_read_strategy` returns it as
   `buy_hold_return`. A strategy below buy-and-hold is not "profitable", whatever
   the net profit says.
2. **Two runs: frictionless and realistic.** Once with `spread_pips=0` /
   zero commission, once with the user's real spread + commission. Report both;
   the gap is the strategy's cost sensitivity. If the realistic run flips the
   sign, say that first.
3. **Start-date sensitivity.** Rerun with the window shifted by ~10% of its
   length (e.g. start 50 bars later). A verdict that flips with the start date
   is noise, not edge — report the range, not one number.
4. **Trade-count gate.** Compare trades per month with what the rules should
   produce (a "15-minute reversal" firing 570 times in 10 months is a bug, not a
   discovery). Fewer than ~30 trades in total: no conclusion, say so.
5. **Cross-asset / cross-timeframe scorecard.** When the user wants robustness,
   tabulate the same rules over 3–7 symbols or timeframes with win rate, profit
   factor, expectancy in R, trades, buy-and-hold — and one line of *why* the weak
   cells fail. Include the buy-and-hold row as a benchmark row, not a footnote.
6. **Improve-only-if-better, and abort dead ends.** In an optimization loop
   keep a variant only when the realistic-run expectancy improves on ALL
   scorecard assets, not one; after two rounds with no improvement, stop and
   say the idea is exhausted instead of tuning it into an in-sample fit.
7. **Forward-test before capital.** A surviving strategy goes into a dated
   "incubation" note: rerun it on new bars weekly and compare with the
   backtest stats. Divergence within a month = overfit; say so.

Quote returns as expectancy in R and profit factor; never headline a total P&L
percentage (in-sample single-asset numbers like "3600%" are the standard sign of
an overfit loop).

## Workflow

1. Fetch bars: `tv_data_get_bars(symbol, timeframe, count)` (or let the tool).
2. Run: `tv_backtest_run(strategy, symbol, timeframe, count, spread_pips, cash, ...)`.
3. Read `summary` (win rate, profit factor, expectancy, max drawdown) and the capped
   recent `trades` (each with direction, size, units, R, session).
3b. Eyeball the fills: `tv_backtest_render_trades(strategy, symbol, ..., max_renders)`
   renders each recent trade on the actual bars — check the entry sits where the rule
   says, the SL/TP bracket looks right, and a -1R exit actually tags the SL line.
4. Compare against a baseline (e.g. `sma_cross`); a strategy is only "better" with
   enough trades and a spread/commission-realistic expectancy above the baseline.
5. For a reusable idea, save a YAML spec (see `tv_strategy_list`) and re-run via
   `tv_strategy_run` instead of repeating parameters.


### smc_h4_m15 (SMC multi-timeframe)

H4 bias (BOS/CHoCH on resampled bars, confirmed only at the breaking H4 close) +
M15 FVG retrace entry, SL at the far gap edge, TP at `rr`. Run it on M15 bars
(the tool rejects H4 and slower). Params: `swing_length` (H4 structure),
`rr`, `risk_amount`, `expiry_bars` (zone lifetime in bars), `use_choch`
(bias flips on CHoCH too), `min_stop_frac` (skip micro-stops). It is a
mechanical simplification of the owner's H4→M15 scheme — no killzone window,
no liquidity-sweep precondition yet — so treat raw results as a harness
baseline, not a verdict on the discretionary setup.

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