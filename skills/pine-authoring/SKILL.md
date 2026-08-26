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

## Error reading

The endpoint's `success` field can be `true` even with errors — the tool derives
true success = processed AND zero errors. Errors carry `start.line/column` when the
compiler provides them; the tool normalizes both inline (`line N col M ...`) and
structured positions.

## References

- `references/pine-notes.md` — more idioms and traps.
- The endpoint is undocumented and may change; treat compile failures as "recheck
  the endpoint" if the source is clearly valid.