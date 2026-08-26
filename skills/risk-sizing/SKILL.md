---
name: risk-sizing
description: "Compute position sizes and risk numbers by hand (pip value incl. JPY, R-multiples, risk-per-trade, account-currency conversion). Zero tools - pure judgment and arithmetic. Use before any trade or backtest to decide what size risks what. Triggers: size the position, how many lots, what's the risk, pip value, R multiple, risk per trade."
---

# Risk sizing (pure judgment)

Position size is the single variable that decides survival. Compute it before
touching a chart or backtest. **Zero tools** — this is arithmetic you verify
yourself; the numbers below match what the backtest engine uses.

## Pip size and pip value

- FX pip: `0.0001` normally; **JPY-quoted pairs `0.01`** (USDJPY, EURJPY, ...).
- Metals: XAU 0.1, XAG 0.01.
- Contract per standard lot: 100 000 units (FX), 100 oz (XAU), 5000 oz (XAG).
- Pip value per lot (in quote currency) = pip size × contract size
  (EURUSD ≈ $10/pip/lot; USDJPY ≈ 1000 JPY/pip/lot).

## Account-currency conversion

When the quote currency differs from your account currency, convert explicitly
(e.g. USD account on USDJPY: USD per pip per lot = 1000 JPY ÷ USDJPY). Never assume
the quote equals the account.

## Risk-based position size

`size (units) = risk_amount / stop_distance_in_price`

A full stop-loss move costs exactly `risk_amount` account units. In pips:
`size = risk_amount / (stop_pips × pip_size)`. This is independent of the
quote→account rate when size is treated in the backtest engine's convention.

## R-multiple

`R = profit / initial_risk`. A stop-hit ≈ −1R. Judge expectancy in R, not dollars
(account size is arbitrary). A setup with WR=30% can still be strongly profitable
if winners average >3R.

## Risk budget template

| item | rule of thumb |
|---|---|
| risk per trade | 0.5–1% of account (owner framework: a hard number, not a mood) |
| risk per day | ≤ 2× per-trade risk |
| max trades / day | fixed; stop trading after a loss series or the daily cap |
| move to break-even | after 1R in your favor, by rule, not by feel |
| partial close / exit | predefined conditions (e.g. 2R target, structure break) |

## Cross-check with the backtest engine

`risk_size(symbol, risk_amount, stop_pips)` in `src/tvmcp/backtest/forex.py` is the
same math this skill uses — verify your hand calculation against it once if unsure,
then trust the numbers.