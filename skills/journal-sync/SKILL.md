---
name: journal-sync
description: "Import FX Replay CSV exports into a Notion trade journal using the documented FX Replay mapping (side, weekday, session, pair). Use when ingesting a backtest/analytics export, de-duplicating re-imports, or syncing trades. Triggers: import FX Replay, sync journal, push trades to Notion, backtest-analytics, map the CSV."
---

# Journal sync: FX Replay CSV → Notion

Import FX Replay CSV exports into a Notion trade journal using the **documented
mapping** (empirically verified, not guessed). Parse + normalize the CSV with the
journal tools here, then write through your own **Notion MCP**.

## When to use

- A new `backtesting-analytics.csv` (or similar FX Replay export) needs to land in
  the backtest journal.
- Re-importing / de-duplicating a previously imported export.
- Checking whether an import is consistent (audit via the raw-code columns).

## Tools

- `tv_journal_scan` — list CSV files in the journal watch-folder and sniff them.
- `tv_journal_parse` — normalize one export into records + summary (side/day/session/
  tags mapping, symbols resolved, R computed).
- Your **Notion MCP** (`notion-create-pages`, `notion-update-data-source`,
  `notion-query-data-sources`, ...) — the write side (external, out of scope here).

## FX Replay CSV columns (genuine export)

`id, dateStart, dateEnd, pair ("OANDA:EURUSD"), uPnL, rPnL, side, entryPrice,
initialSL, maxTP, idealTP, amount, amountClosed, status, day, tags, avgClosePrice,
avgRiskReward, maxRiskReward, exchangeRate, initialBalance, currentRealizedBalance`.

`avgRiskReward` is the realized R-multiple (loss = exactly -1, win = rPnL/risk);
`tv_journal_parse` recomputes `r` the same way and cross-checks.

## Import procedure (proven path)

1. `tv_journal_scan` to locate the file; `tv_journal_parse` to normalize + get the
   summary (win rate, expectancy, per-session/day/side).
2. **Pre-create multi-select options**: Notion's `notion-create-pages` cannot create
   new multi-select options on the fly — first `notion-update-data-source` with
   `ALTER COLUMN "<col>" SET MULTI_SELECT(...)` listing every unique tag/pair value.
3. Create rows with `notion-create-pages` under the **backtest journal**
   `data_source_id`, resolving relations programmatically (pair→pair page, setup→
   draft-setup page, side/day/session→the mapped pages). Do NOT use the Notion UI
   CSV importer (it can't resolve relations/multi-select).
4. Verify with `notion-query-data-sources`; formulas/rollups are opaque via API
   (`formulaResult://...`), so confirm WR/expectancy **by eye** (browser get-text on
   the setup/trade page).
5. Keep the raw audit columns (`День (номер)`, `Пара (FX Replay)`, `userDefined:ID`,
   `Тэги (FX Replay)`) for de-dup on re-import.

## Mapping (documented conventions)

Fill in your workspace's page IDs in `references/notion-mapping.template.md`.

- **Side**: `Buy → Long`, `Sell → Short`.
- **Day**: ISO weekday `1=Mon … 5=Fri` → a `Дни недели`-style day page per code.
- **Session** (by UTC hour of `dateStart`): `00:00–07:00 Asia`, `07:00–08:00
  Frankfurt`, `08:00–13:00 London`, `13:00–16:00 Overlap`, `16:00–22:00 New York`,
  `22:00–24:00 Asia` (wrap). Note: session is computed from the exported time; if
  session bands look shifted on the chart, the export may be in broker-local time,
  not UTC — adjust `utc_offset_hours` on `tv_journal_parse` and re-check.

## Rules

- **Keep the backtest journal structurally isolated** from the live journal — never
  write backtest rows into the live journal.
- Setup promotions go through the Draft Setups ⇄ Active Setups buttons, not a direct
  DB write.
- Write nothing back to the CSV; the export is read-only input.

## References

- `references/notion-mapping.template.md` — DB/page ID placeholders + conventions.