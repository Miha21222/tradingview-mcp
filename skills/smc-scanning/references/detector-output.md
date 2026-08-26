# Detector output reference

All scan tools return `{symbol, tv_symbol, provider, timeframe, bars_scanned,
detector, params, ...}`. Timestamps are UTC ISO strings; `index` is the 0-based bar
position in the loaded window. `total_count`/`returned_count`/`truncated` report
output bounding (the last `total_count`-of-`returned_count` results are shown).

## tv_scan_fvg

| field | meaning |
|---|---|
| `time` | bar that the gap formed on |
| `direction` | bullish (prev high < next low) or bearish (prev low > next high) |
| `top`/`bottom` | gap bounds; price traded through these when mitigated |
| `mitigated_time` | when price re-entered the gap (null = not yet) |

## tv_scan_ob

| field | meaning |
|---|---|
| `direction` | order-block side (the move that broke the swing) |
| `top`/`bottom` | block zone |
| `strength_pct` | block strength (volume imbalance) |
| `mitigated_time` | when price traded through the block |

## tv_scan_structure

| field | meaning |
|---|---|
| `kind` | `bos` (break of structure) or `choch` (change of character) |
| `direction` | bullish/bearish |
| `level` | the broken swing level |
| `broken_time` | when price broke through (null = unconfirmed) |

## tv_scan_liquidity

| field | meaning |
|---|---|
| `direction` | bullish (clustered highs) or bearish (clustered lows) |
| `level` | average of the clustered swings |
| `end_time` | last bar of the pool |
| `swept_time` | when price swept the pool (null = not swept) |

## tv_scan_sessions

Returns `blocks[]`, one per contiguous killzone occurrence: `start_time`,
`end_time`, `count` (bars), `high`, `low` — the block's dealing range (liquidity).

## tv_scan_prev_hl

`previous_high`/`previous_low` of the requested `time_frame`, plus `broken_high`/
`broken_low` flags and `as_of` (the last bar evaluated).

## Repainting

`swing_length`-based detectors (ob, structure, liquidity) use future candles to
label swings. Results whose `index` falls in the last `swing_length` bars are
unconfirmed; the `repaint_note` field states this explicitly.