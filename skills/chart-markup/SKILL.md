---
name: chart-markup
description: "Render candlestick charts with SMC/ICT markup overlays to PNG files. Use when you have a scan result or a set of levels/timestamps and want a visual for review or for attaching to a note. Triggers: render the chart, draw the FVG, make a PNG of this setup, visualize the scan, markup chart."
---

# Chart rendering with markup

Turn bars + a declarative markup spec into a PNG via `tv_chart_render` (headless
Chromium + Lightweight Charts v5). The chart is an **artifact of the numbers** —
always pair it with the JSON that produced it.

## When to use

- You just ran a scan (the SMC pattern-detection tools) and want to eyeball the structure.
- You have a backtest trade (or a journal entry) and want its chart.
- You need a compact visual to attach to a Notion/journal record.

## Tools

- `tv_chart_render` — the only tool. Everything else feeds it.

## The `markup_json` schema (version 1)

```json
{
  "version": 1,
  "grid": true,
  "markup": [
    {"type": "fvg",  "time": "2026-08-24T17:15:00Z", "direction": "bullish", "top": 1.1662, "bottom": 1.1660},
    {"type": "ob",   "time": "2026-08-24T18:15:00Z", "direction": "bearish", "top": 1.1670, "bottom": 1.1665},
    {"type": "bos",  "time": "2026-08-25T08:00:00Z", "level": 1.1690, "label": "BOS"},
    {"type": "choch","time": "2026-08-25T09:00:00Z", "level": 1.1685, "label": "CHoCH"},
    {"type": "killzone", "start": "2026-08-24T06:00:00Z", "end": "2026-08-24T09:00:00Z", "label": "London"}
  ]
}
```

- `time`/`start`/`end` are ISO-8601 UTC and **must fall inside the loaded bar range**
  (enlarge `count` or fix the time if the tool rejects them).
- `fvg`/`ob`: box between `top` and `bottom` at `time`; `direction` colors it.
- `line`/`bos`/`choch`: horizontal level line at `level` with an optional `label`.
- `killzone`: vertical band from `start` to `end` with an optional `label`.
- `grid: true` stamps a labeled coordinate grid (the vision aid).
- Empty `markup_json` renders candles only.

## Workflow

1. Decide the window: `tv_chart_render(symbol, timeframe, count, markup_json)`.
2. Build the markup from your data (scan results, journal levels, backtest SL/TP).
3. Check the returned `path` — a PNG in the managed chart dir (never a caller-chosen
   path). `markup_count`/`bars` confirm what was drawn.
4. Reference the PNG path (e.g. attach to a note); keep the JSON alongside it.

## Behavior notes

- `provider` in the result names the data feed; the chart inherits it.
- Bounding: `count` is capped (default 150, max 500) to keep renders deterministic.
- The render is deterministic (fixed viewport/DPR/UTC/Arial); golden tests guard it.

## References

- `references/markup-schema.md` — full field list and validation rules.