# Notion mapping template

Fill in your own workspace's identifiers. The conventions below are the documented,
empirically-verified defaults; replace every `<...>` placeholder with your actual
database/page IDs before use. Keep this file out of public distribution.

## Databases (data source ids)

| purpose | id |
|---|---|
| live journal | `<DB_LIVE_JOURNAL>` |
| active setups | `<DB_ACTIVE_SETUPS>` |
| expectancy calculator | `<DB_EXPECTANCY>` |
| **backtest journal** (FX Replay format) | `<DB_BACKTEST_JOURNAL>` |
| draft setups | `<DB_DRAFT_SETUPS>` |
| days of week | `<DB_DAYS>` |
| full pair list | `<DB_PAIRS>` |

## Relation maps (page ids)

**Side**: `Buy → Long` (`<PAGE_LONG>`), `Sell → Short` (`<PAGE_SHORT>`).

**Day** (from the raw weekday code 1..5):
`1=<PAGE_MON>`, `2=<PAGE_TUE>`, `3=<PAGE_WED>`, `4=<PAGE_THU>`, `5=<PAGE_FRI>`.

**Session** (by UTC hour of the open time):

| band (UTC) | page id |
|---|---|
| 00:00–07:00 | Asia `<PAGE_ASIA>` |
| 07:00–08:00 | Frankfurt `<PAGE_FRANKFURT>` |
| 08:00–13:00 | London `<PAGE_LONDON>` |
| 13:00–16:00 | Overlap `<PAGE_OVERLAP>` |
| 16:00–22:00 | New York `<PAGE_NEW_YORK>` |
| 22:00–24:00 | Asia (wrap) |

## Property conventions

- Keep raw audit columns (`Пара (FX Replay)`, `День (номер)`, `userDefined:ID`,
  `Тэги (FX Replay)`) for de-duplication on re-import.
- The backtest journal's status is an auto formula on the sign of realized R; the
  live journal's status is a manual workflow — do not cross-write.
- Backtest pair relation points at the full pair list; the live journal points at
  the active-pairs list only.

## Import traps

- Notion API rejects `BUTTON` property updates and Number-format/Ring/visibility
  settings — those are UI-only. Data writes go through the API fine.
- Multi-select options must pre-exist (ALTER COLUMN first) or page creation fails.
- Formula/rollup verification is eye-only via the browser.