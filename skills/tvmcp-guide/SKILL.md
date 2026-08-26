---
name: tvmcp-guide
description: "Orientation and troubleshooting guide for the tradingview-mcp server: what every toolset does, how to set the server up from zero, how to fix any error it raises, and how to answer user questions about capabilities, data feeds, and TradingView ToS risk. Use when onboarding a user to this server, choosing which tool fits a job, diagnosing a failing tool call, or explaining what this MCP can and cannot do. Triggers: how does this work, set up tradingview mcp, which tool should I use, tool errored, no data returned, getting started, what can you do with charts/backtests/scans."
---

# tradingview-mcp: the complete operator's guide

You are driving a TradingView-centric trading toolkit. This skill is the map: every
toolset, the setup ladder, the output conventions, and the fix for every failure
mode. `references/troubleshooting.md` holds the full symptom→fix table — consult it
before declaring anything broken.

## First move on any new install

Call `tv_setup_doctor`. It checks every prerequisite (Node, Chromium, credentials,
CDP, folders) and returns the **exact shell command** to fix anything broken. Run
the fixes for `optional: false` items, rerun, then proceed. Never try to obtain
credentials yourself — `TV_SESSIONID` and `OANDA_API_KEY` are the human's move,
always.

## The toolset ladder (risk-tiered by design)

| Toolset | Tools | Needs | TV ToS risk |
|---|---|---|---|
| `public` (default) | `tv_screener_run`, `tv_ta_summary`, `tv_symbol_search`, `tv_setup_doctor` | nothing | none (no account) |
| `data` (default) | `tv_data_get_bars`, `tv_data_providers_status` | Node for Dukascopy; OANDA key optional | none (not TV data) |
| `scan` | `tv_scan_fvg/ob/structure/liquidity/sessions/prev_hl` | bars available | none |
| `chart` | `tv_chart_render` | `playwright install chromium` | none (own engine) |
| `backtest` | `tv_backtest_run` | bars | none |
| `strategy` | `tv_strategy_list/run` | YAML specs in strategy dir | none |
| `journal` | `tv_journal_scan/parse` | FX Replay CSV exports | none |
| `pine` | `tv_pine_compile` | network | low (undocumented endpoint) |
| `session` | `tv_session_status/ohlcv/realtime` | `TV_SESSIONID` cookie | **yes — user's account** |
| `desktop` | `tv_desktop_status/screenshot/set_symbol/set_timeframe` | app running with CDP | **yes — user's account** |

Enable via `TV_TOOLSETS` env (comma list; `default` = public+data; `all` = everything).
`TV_READ_ONLY=1` strips every workspace-mutating tool regardless of toolsets.
For `session`/`desktop` details, load the `tradingview-tiers` skill.

## Output conventions (hold these in every answer)

- **Bars are arrays** `[time_iso, open, high, low, close, volume]`, UTC, oldest first.
- **`provider` names the feed** on every price/level (dukascopy / oanda / session /
  desktop). Feeds disagree — never quote a level as feed-independent.
- **`truncated` / `total_count`** — output is capped; you did not necessarily see
  everything. Narrow the window rather than assuming.
- **`repaint_note`** on swing-based scans — the trailing `swing_length` bars are
  classified using future candles; treat them as unconfirmed.
- Symbols accept any alias (`EURUSD`, `OANDA:EURUSD`, `EUR_USD`, `eurusd`);
  timeframes are `M1 M5 M15 M30 H1 H4 D1`.

## Choosing the right tool

- "What's the market doing / find candidates" → `tv_screener_run` / `tv_ta_summary`.
- "Get me price history" → `tv_data_get_bars` (free feeds). TV-chart-parity candles
  specifically → `tv_session_ohlcv` (opt-in, cookie).
- "Find setups / structure / liquidity" → the scan tools (`tv_scan_fvg`, `tv_scan_ob`, `tv_scan_structure`, `tv_scan_liquidity`, `tv_scan_sessions`, `tv_scan_prev_hl`), then `tv_chart_render` to show it.
- "Does this idea make money" → `tv_backtest_run`; reusable parameterization → a YAML
  spec + `tv_strategy_run`.
- "Check my Pine script" → `tv_pine_compile` in a write-compile-fix loop.
- "What did I trade" → `tv_journal_scan` → `tv_journal_parse`.
- "Show/drive my actual TradingView" → the desktop tools (`tv_desktop_status`, `tv_desktop_screenshot`, `tv_desktop_set_symbol`, `tv_desktop_set_timeframe`; opt-in).

## Guiding a user from zero (the full path)

1. `tv_setup_doctor` → run fixes → healthy.
2. Demonstrate zero-config value first: screener query, then bars, then a scan.
3. Offer opt-ins by need, stating cost honestly: chart (one Chromium install),
   backtest (nothing), session/desktop (credentials + ToS risk — user decides,
   never push).
4. On any error: read the message — every error in this server states what to
   configure or retry; `references/troubleshooting.md` maps the rest.

## Honest limits (say these when asked)

- Scan detectors wrap a pinned `smartmoneyconcepts` library; synthetic regression
  fixtures pin behavior, and detection "correctness" is only as good as its rules.
- Vision on charts is verification-grade, not detection-grade — numbers are the
  authority, the chart illustrates.
- Backtests fill at next-bar open (or close with `trade_on_close`), charge spread
  on entry only, and model no slippage/swap — say so when reporting results.
- `pine`/`session`/`desktop` ride undocumented TradingView surfaces that can break
  or carry account risk; the server warns, you should too.

## References

- `references/troubleshooting.md` — symptom → cause → exact fix, for every tool.
