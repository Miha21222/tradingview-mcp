# Pine v5 idiom notes

- `ta.crossover(a, b)` / `ta.crossunder(a, b)` replace the deprecated forms.
- Strategy orders: `strategy.entry(id, direction, qty, when)`,
  `strategy.exit(id, from_entry, stop, limit)`, `strategy.close_all(when)`.
- Draw on chart with `line.new(x1, y1, x2, y2, color=...)`, `box.new(...)`,
  `label.new(...)`; manage with `*[1]` references and `line.set_*`/`line.delete`.
- Higher timeframes: `request.security(syminfo.tickerid, "240", ta.highest(high, 20))`
  — pass an *expression*, not a series that changes bar-to-bar.
- Time filters: `hour(time)`/`dayofweek(time)` for session/killzone logic.
- Avoid look-ahead: do not reference `close[1]`-style values that the current bar
  cannot know yet; `barstate.isconfirmed` gates order-on-close logic.
- Repainting: indicators that use `request.security` with `lookahead_on` are
  suspect for signals — prefer `lookahead_off` (default) for entries.

## Minimal skeleton

```pine
//@version=5
indicator("My indicator", overlay=true, max_bars_back=1000)

len = input.int(20, "Length")
sma = ta.sma(close, len)
plot(sma, "SMA", color=color.blue)

longCond = ta.crossover(close, sma)
shortCond = ta.crossunder(close, sma)
plotshape(longCond, style=shape.triangleup, location=location.belowbar)
```