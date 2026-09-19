---
name: pine-authoring
description: "Author and iterate Pine Script v5 via TradingView's real compiler (tv_pine_compile). Use when writing or fixing Pine scripts, converting an idea into Pine, or debugging a compile error. Triggers: write Pine, compile Pine, fix Pine error, Pine script."
---

# Pine Script authoring with a compiler loop

Write Pine v5 and verify it against TradingView's real compiler in a loop:
author → compile → fix errors → recompile. Nothing is published or saved to any
TradingView account — this is a read-only compile/typecheck workflow.

## When to use

- You have a strategy/indicator idea and need working Pine source.
- You're debugging a compiler error and want line/column feedback fast.
- You want to sanity-check Pine syntax before pasting it into TradingView.

## Tools

- `tv_pine_compile(source)` — the whole loop. POST the full source (including the
  `//@version=5` line); returns `{success, errors[], warnings[]}`.

## Workflow

1. Draft the script with the `//@version=5` header.
2. Call `tv_pine_compile(source)`.
3. If `success` is false, read each `error` (`message`, `line`, `column`), fix, and
   recompile. `warnings` are non-fatal but worth reading.
4. Repeat until `success: true` (no errors), then paste into TradingView.

## Common Pine v5 gotchas

- `//@version=5` must be the first line.
- Indicator vs strategy: `indicator("...", overlay=true)` vs `strategy("...", ...)`;
  `strategy.*` functions (entry, exit, close) only work in a strategy.
- Built-in variables are `close`, `high`, `low`, `open`, `volume`, `time`, `bar_index`.
- `ta.*` (e.g. `ta.sma`, `ta.crossover`) replaces the old `sma()`/`crossover()` forms.
- Series vs simple: don't use a series in a `static` position (e.g. `line.new`
  coordinates need to be passed correctly).
- `input.*` for user parameters; `plot()` for output.
- Beware `request.security()` syntax (symbol, timeframe, expression, gaps) for
  higher-timeframe context.

## Normalize a strategy() before any backtest

Strategy Tester numbers are only comparable when the script pins its own
assumptions. Before backtesting (in TradingView or by porting the rules to
`tv_backtest_run`), make sure the `strategy()` call and inputs carry:

- **Date range as inputs**: `input.time(...)` start/end and a `inDateRange`
  guard on every entry — never "full history", and never scroll the chart to
  window a test.
- **Costs**: `commission_type=strategy.commission.cash_per_order` (or percent)
  with the user's real value, `slippage` in ticks; run once with zero costs and
  once with real costs (see `strategy-backtesting`).
- **Sizing**: `default_qty_type=strategy.fixed`, `default_qty_value=1` (or the
  user's contracts), `initial_capital` explicit. For futures/CFD add
  `margin_long=0, margin_short=0` — TradingView otherwise rejects orders as
  under-margined and the trade list silently shrinks.
- **Names**: `shorttitle` ≤ 10 chars (compile error otherwise).
- **Zero trades?** Add a temporary `table` with counters (signals seen, entries
  blocked by date range, by margin, by pyramiding) before guessing.

Compile-check the normalized script with `tv_pine_compile` first; then, if the
user wants it on THEIR chart, push it through the desktop loop below.

## Editor loop on the user's live chart (opt-in desktop tier)

`tv_desktop_pine_set_source` → `tv_desktop_pine_compile` (Monaco `errors` with
line/column, `study_added`) → fix → repeat; `tv_desktop_pine_save` only when the
user wants it kept; `tv_desktop_read_strategy` for the Strategy Tester report
(always quote `buy_hold_return` next to `net_profit`). Two rules: (1) a saved
script open in the editor is auto-saved to the user's account the moment its text
changes — the set tool refuses without `overwrite_saved=true`; get a yes first or
have them open a new blank script; (2) `tv_pine_compile` (server-side) is the
cheap syntax check — do not burn the user's editor on typos.

## Error reading

The endpoint's `success` field can be `true` even with errors — the tool derives
true success = processed AND zero errors. Errors carry `start.line/column` when the
compiler provides them; the tool normalizes both inline (`line N col M ...`) and
structured positions.

## References

- `references/pine-notes.md` — more idioms and traps.
- The endpoint is undocumented and may change; treat compile failures as "recheck
  the endpoint" if the source is clearly valid.