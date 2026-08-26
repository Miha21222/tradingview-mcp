---
name: smc-scanning
description: "Scan market data for SMC/ICT patterns (FVG, order blocks, BOS/CHoCH, liquidity plus sweeps, killzone sessions, previous day H/L). Use when you need to find trade setups, identify structure shifts, or build a bias from price action. Triggers: scan for FVG, find order blocks, check for liquidity sweep, is there a BOS, killzone analysis, ICT setup."
---

# SMC / ICT pattern scanning

Detect smart-money / ICT structures on OHLCV bars: Fair Value Gaps, Order Blocks,
Break of Structure / Change of Character, liquidity pools + sweeps, killzone
sessions, and previous-period high/low. **Numbers are the authority** — the chart is
an artifact; read the JSON, not pictures.

## When to use

- Establishing a trading bias before entry decisions.
- Finding confluent setups: e.g. a liquidity sweep into a killzone that leaves an FVG.
- Checking whether a level is broken (BOS) or a move changed character (CHoCH).
- You have a symbol/timeframe in mind and want the structure, not a raw quote.

## Tools

Front these first (all read-only, all name their data feed via `provider`):

- `tv_data_get_bars` — OHLCV bars (Dukascopy default; OANDA if `OANDA_API_KEY` set).
- `tv_scan_fvg` — Fair Value Gaps (bullish/bearish boxes with top/bottom).
- `tv_scan_ob` — Order Blocks (boxes + strength % + mitigation).
- `tv_scan_structure` — BOS and CHoCH (level + broken_index/time).
- `tv_scan_liquidity` — clustered swing highs/lows + sweeps.
- `tv_scan_sessions` — killzone blocks with each block's high/low (dealing range).
- `tv_scan_prev_hl` — previous-day/week high/low and whether price broke them.

Optional: `tv_chart_render` to draw the detected structures (see the chart-markup skill).

## Workflow

1. **Get bars**: `tv_data_get_bars(symbol, timeframe, count)` — for M15 day-trading,
   500–1000 bars is a reasonable window; match the timeframe to the intent
   (M15/M30 for intraday, H1/H4 for swing).
2. **Scan structure**: `tv_scan_structure(symbol, timeframe, count, swing_length)`.
   A bullish BOS above a prior swing high after a sweep is a classic setup seed.
3. **Scan liquidity**: `tv_scan_liquidity` — look for *swept* pools (a `swept_time`
   present) as potential reversal triggers.
4. **Scan FVG/OB**: `tv_scan_fvg` and `tv_scan_ob` — gaps and blocks in the
   direction of your bias; treat a fresh FVG as support/resistance, not a signal.
5. **Session filter**: `tv_scan_sessions(symbol, timeframe, count, session=...)` —
   note each killzone block's high/low (the dealing range where liquidity sits).
6. **Previous H/L**: `tv_scan_prev_hl` — levels traders watch; a sweep of the
   previous day high/low is a common stop-hunt.
7. **Synthesize a narrative**, naming each detected element with its timestamp and
   price, then (optionally) render it: build a `markup_json` and call
   `tv_chart_render` (see chart-markup skill).

## Reading results

Every scan returns compact JSON. Key facts to honor:

- **`provider`** — the feed (dukascopy/oanda/session). Feeds disagree; never present
  a level as feed-independent.
- **`repaint_note`** — swing-based detectors (ob, structure, liquidity) classify a
  swing using *future* candles; the trailing `swing_length` bars are **unconfirmed**.
  Do not trade a signal whose `index` is within the last `swing_length` bars.
- **`truncated` / `total_count` / `returned_count`** — the tool caps output; widen
  your own window, don't assume you saw everything.
- Timestamps are UTC ISO strings; map them to sessions yourself if needed.

## Swing length guidance

`swing_length` = candles each side of a swing. The library default (50) is far too
coarse for M5/M15. Use **5–15** on intraday, larger on H4/D1. A too-small value makes
noise; too-large misses structure.

## Anti-patterns

- Do not treat a single FVG/OB as a signal by itself — it is a zone, not a trigger.
- Do not use the final `swing_length` bars (repaint).
- Do not mix feeds when quoting levels (always carry the `provider`).
- Do not scan H4 with M15 swings — match swing_length to the timeframe.

## References

- `references/detector-output.md` — each detector's exact output fields.
- Pair with `strategy-backtesting` to validate a setup idea statistically, and
  `chart-markup` to visualize a scan before deciding.