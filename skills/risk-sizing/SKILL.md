---
name: risk-sizing
description: "Compute position sizes and risk numbers by hand (pip value incl. JPY, R-multiples, risk-per-trade, account-currency conversion), and check a journal against written risk rules with tv_risk_guard before trading. Use before any trade or backtest to decide what size risks what, and to answer whether today's limits are already used up. Triggers: size the position, how many lots, what's the risk, pip value, R multiple, risk per trade, am I allowed to trade today, daily loss limit, drawdown limit, cooldown, room to the floor."
---

# Risk sizing (judgment) + the risk guard (bookkeeping)

Position size is the single variable that decides survival. Compute it **by hand**
before touching a chart or backtest; the arithmetic below matches what the backtest
engine uses. The one thing that is not judgment is whether today's limits are
already spent — that is bookkeeping over your journal, and `tv_risk_guard` does it.

## "Am I allowed to trade today?" → `tv_risk_guard`

`tv_risk_guard(journal=... | records=..., rules=... | rules_path=..., today=...,
account=...)` → `{status, checks[], account, window, fail_closed, warnings}`.

- `status`: `stop` = one of **your** rules was breached · `flag` = a soft rule fired
  · `ok` = nothing fired. Report the failing check's `detail` and `numbers`; do not
  add a verdict of your own on top.
- Every threshold comes from the rules config — the tool ships none of its own. Copy
  `risk_rules.example.json` from the server's `journal` package, put your numbers in
  it, pass `rules_path`. Limits name their unit: `{"pct": 2}` (percent of the
  account), `{"currency": 200}` or `{"r": 2}`, and a loss that **reaches** the limit
  breaches it. Count limits use `"max"` = the largest allowed count.
- Check types: daily / weekly / monthly loss (calendar or rolling window), trades
  and losing trades per day and per week, cooldown after N consecutive losses
  (counted in **sessions**, not calendar days), room above a static or trailing
  account floor, rest after a stop-out, an idle flag, an optional profit target.
- Account numbers go in `account`: `{balance, starting_balance, balance_asof,
  currency, risk_per_r, floor | max_loss_pct, floor_type: "static"|"trailing"}`.
  Your firm's rules are *your* input; the tool knows no firm and no strategy.
- `today` is required (a date, or a full ISO timestamp when you want the
  minute-based rest check). The guard never guesses which session you mean.
- `fail_closed: true` (an unparseable journal row) forces `stop`. Fix the row and
  re-run — never route around it.
- A check reading `unknown` means a number was missing, not that it passed. Say so.
- This is arithmetic over trades already recorded: **not advice**, not a position
  sizer, and it never says what to trade. Sizing stays with the hand arithmetic below.

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