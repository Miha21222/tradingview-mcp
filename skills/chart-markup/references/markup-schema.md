# markup_json full reference

## Version

`version` must equal `1`. Any other value is rejected.

## Top-level

| key | type | default | meaning |
|---|---|---|---|
| `version` | int | — (required) | schema version |
| `grid` | bool | `true` | stamp the labeled coordinate grid |
| `markup` | list | `[]` | primitive items (see below) |

## Primitives

### fvg / ob (box)
| key | type | rule |
|---|---|---|
| `type` | `"fvg"` / `"ob"` | required |
| `time` | ISO-8601 UTC | inside the bar range |
| `direction` | `"bullish"` / `"bearish"` | colors the box |
| `top` | float | `> 0`, `>= bottom` |
| `bottom` | float | `> 0` |

### line / bos / choch
| key | type | rule |
|---|---|---|
| `type` | `"line"` / `"bos"` / `"choch"` | required |
| `time` | ISO-8601 UTC | inside the bar range |
| `level` | float | `> 0` |
| `label` | string, optional | drawn next to the line |

### killzone
| key | type | rule |
|---|---|---|
| `type` | `"killzone"` | required |
| `start` / `end` | ISO-8601 UTC | inside the bar range |
| `label` | string, optional | drawn at the top of the band |

## Validation errors

Invalid JSON, unknown `type`, `version != 1`, `top < bottom`, non-positive
prices/levels, or a time outside the bar range all raise an actionable `ToolError` —
fix the spec, don't guess.