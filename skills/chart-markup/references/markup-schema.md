# markup_json full reference

## Version

`version` must equal `1`. Any other value is rejected.

## Top-level

| key | type | default | meaning |
|---|---|---|---|
| `version` | int | — (required) | schema version |
| `grid` | bool | `true` | stamp the labeled coordinate grid |
| `markup` | list | `[]` | primitive items (see below) |

## Common optional keys

| key | type | rule |
|---|---|---|
| `color` | hex string | `#rrggbb` (e.g. `#ff9800`); overrides the primitive's default color |
| `label` | string | drawn next to the primitive (all primitives except `text`, which IS a label) |

## Primitives

### fvg / ob (box)
| key | type | rule |
|---|---|---|
| `type` | `"fvg"` / `"ob"` | required |
| `time` | ISO-8601 UTC | inside the bar range |
| `direction` | `"bullish"` / `"bearish"` | default color (green/red) when no `color` |
| `top` | float | `> 0`, `>= bottom` |
| `bottom` | float | `> 0` |

### line / bos / choch
| key | type | rule |
|---|---|---|
| `type` | `"line"` / `"bos"` / `"choch"` | required; default colors: dark / blue / purple |
| `time` | ISO-8601 UTC | inside the bar range |
| `level` | float | `> 0` |

### killzone
| key | type | rule |
|---|---|---|
| `type` | `"killzone"` | required; default blue band |
| `start` / `end` | ISO-8601 UTC | inside the bar range |

### text
| key | type | rule |
|---|---|---|
| `type` | `"text"` | required |
| `time` | ISO-8601 UTC | inside the bar range |
| `price` | float | `> 0`; vertical anchor |
| `text` | string | 1–80 chars |

### marker
| key | type | rule |
|---|---|---|
| `type` | `"marker"` | required |
| `time` | ISO-8601 UTC | inside the bar range |
| `price` | float | `> 0` |
| `direction` | `"up"` / `"down"` | up arrow sits below the price, down arrow above; default green/red |

## Windowing (tool arg, not markup)

`tv_chart_render(end_time=...)` — ISO-8601 UTC — charts the `count` bars ENDING at
that time instead of now. Future times are rejected. Use it to zoom historical
moments (backtest trades, journal entries).

## Validation errors

Invalid JSON, unknown `type`, `version != 1`, `top < bottom`, non-positive
prices/levels, a malformed `color`, or a time outside the bar range all raise an
actionable `ToolError` — fix the spec, don't guess.
