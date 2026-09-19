---
name: pine-authoring
description: "Author, compile and deploy Pine Script: server-side syntax checks with tv_pine_compile, then the one-call build pipeline tv_desktop_pine_build_and_backtest on the user's chart. Use when writing or fixing Pine, turning an idea into a strategy, deploying or backtesting one, or debugging a compile error. Triggers: write Pine, compile Pine, fix Pine error, build a strategy, backtest this strategy, deploy to TradingView."
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

## Build pipeline on the user's live chart (opt-in desktop tier)

One call does the whole deploy:

`tv_desktop_pine_build_and_backtest(source, name, symbol?, timeframe?, clear_studies="strategies")`

It prepares the workspace, protects the user's own scripts, injects the source,
compiles, and reads the Strategy Tester report. It does not raise once connected —
read `ok` and `stage` (`prepare | own_copy | set_source | compile | markers |
results`) and act:

- `stage: "markers"` — `compile_errors` carry line/column, `hints` carry up to
  three landmine-derived fixes. Fix the source and call again **with the same
  `name`**; the rerun is safe. `compile_warnings` never stop a build — never loop
  on a warning.
- `results: null` with a note — the source declares `indicator()`, so there is no
  backtest to read. An `ok: true` with empty results and a `fragile` note means
  the strategy did not attach; say so instead of inventing numbers.
- Always quote `buy_hold_return` next to `net_profit` from `results`.

Rules that the tools enforce, and you should not fight:

1. **Never overwrite the user's saved script.** When a different saved script is
   open, the pipeline makes a verified copy (header switched *and* the script
   present in the account's saved list) or starts a new one. A blocked
   `set_source` means exactly that — do not pass `overwrite_saved=true` to get
   past it without the user's explicit yes.
2. **Parameter changes are not rebuilds.** Use `tv_desktop_set_study_inputs`
   (read back after writing), rebuild only for logic changes.
3. **Small edits on a long script are surgical**:
   `tv_desktop_pine_find_exact(needle)` → check `occurrences` →
   `tv_desktop_pine_replace_exact(needle, replacement, expected_occurrences,
   expect_source_sha256)` → compile. The tools normalize line endings to the
   buffer's own and verify byte-exactly; a mismatch changes nothing.
4. `tv_pine_compile` (server-side, no chart) is the cheap syntax check — do not
   burn the user's editor on typos. `tv_desktop_pine_get_errors` re-reads the
   markers without clicking anything; never click the editor's error widget.
5. `clear_studies` defaults to `strategies` — the user's indicators stay. Pass
   `all` only when they asked for a clean chart.

## Error reading

The endpoint's `success` field can be `true` even with errors — the tool derives
true success = processed AND zero errors. Errors carry `start.line/column` when the
compiler provides them; the tool normalizes both inline (`line N col M ...`) and
structured positions.

## References

- `references/pine-notes.md` — more idioms and traps.
- The endpoint is undocumented and may change; treat compile failures as "recheck
  the endpoint" if the source is clearly valid.