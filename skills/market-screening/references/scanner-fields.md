# Common scanner fields

Fields are passed by name to `tv_screener_run` (columns / filters / order_by).
Timeframe-scoped fields take a suffix, e.g. `RSI|15` for M15, `RSI|60` for H1,
`RSI|1D` for daily (or leave unsuffixed for the scanner default).

## Quote / volume

- `name`, `description`, `close`, `open`, `high`, `low`, `change`,
  `change_abs`, `volume`, `market_cap_basic`, `average_volume_10d_calc`

## Momentum / oscillators

- `RSI`, `Stoch.K`, `Stoch.D`, `MACD.macd`, `MACD.signal`, `MACD.hist`,
  `ADX`, `CCI20`, `Momentum`, `ROC`, `WilliamsR`, `W.R`

## Moving averages / trend

- `SMA20`, `SMA50`, `SMA100`, `SMA200`, `EMA20`, `EMA50`
- `ADX`, `Aroon.Osc`, `BB.upper`, `BB.lower`, `BB.%(width)`

## Fundamentals (stocks)

- `P/E`, `EPS`, `P/S`, `P/B`, `market_cap_basic`, `dividend_yield_indicated`,
  `return_on_assets`, `return_on_equity`

## Recommend (TA ratings)

- `Recommend.All`, `Recommend.MA`, `Recommend.Other` (+ suffix)

## Example

Find high-momentum forex pairs:

```json
{"market": "forex",
 "columns": ["name", "close", "change", "RSI|15", "ADX|15", "volume"],
 "filters": [["ADX|15", ">", 25], ["RSI|15", "between", [45, 70]]],
 "order_by": "change", "limit": 25}
```