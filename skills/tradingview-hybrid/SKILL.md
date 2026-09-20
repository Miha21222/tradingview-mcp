---
name: tradingview-hybrid
description: "Routing guide for running tradingview-mcp side by side with the official TradingView MCP (mcp.tradingview.com): which server answers which job, what the official beta really does and where it is thin (delayed bars, no deep intraday history, no CFD-broker symbols in the screener, price-only alerts), and how to set the pair up. Use when both servers are registered, when a quote/screener/news/calendar/alert/watchlist question comes up, when deciding between tv_data_get_bars / tv_session_ohlcv and the official get_ohlcv, or when a user asks whether they need both. Triggers: official TradingView MCP, mcp-tradingview, which server, hybrid, do I need both, economic calendar, news, alerts, watchlist."
---

# Hybrid: tradingview-mcp + the official TradingView MCP

Two servers, one toolkit. The official server (`mcp-tradingview`, HTTP + OAuth,
public beta since 2026-09-16) owns the **account and research surface**. This
server (`tradingview`, `TV_TOOLSETS=hybrid`) owns **analysis, charts, engines and
the desktop app**. Nothing is bridged in code: pick the right server per call.

## Routing table (decide per job, not per session)

| Job | Use | Why |
|---|---|---|
| Latest quote, % change, volume for a listed symbol | official `get_symbol_data` / `get_symbol_data_batch` | account-grade screener columns, one call |
| Rank / filter a market (RSI, ADX, change, fundamentals) | official `run_screener` (+ `get_screener_columns`) | same engine as `tv_screener_run`, maintained by TradingView |
| Resolve a ticker / company name | official `search_symbols`; fall back to `tv_symbol_search` for CFD-broker feeds | official search misses broker CFDs (see limits) |
| News headlines, earnings/dividends, macro series, fundamentals, filings, forecasts | official only | this server has no research surface |
| Economic events for a day or week | either: official `get_economic_calendar` for depth (history since 2003, ~31 days forward); `tv_calendar_check` when holidays/rollover belong in the same answer or the official server is not registered | ours adds exchange holidays, half-days and futures rollover, and degrades to a static table instead of failing |
| Is the exchange closed or on a half-day, when does this futures contract roll | `tv_calendar_check` (pass `symbol` for rollover) | the official calendar carries neither |
| Price alerts (create/list/update/delete), watchlists (CRUD) | official only | this server never writes to a TradingView account |
| Daily/weekly bars for bias, quick context | either; official `get_ohlcv` is simplest | both fine; official is delayed 15+ min |
| Intraday bars older than ~3 sessions at 1m, any bar-exact replay | `tv_data_get_bars` (Dukascopy/OANDA, cached) | official has no `end_time`/paging: max 5000 bars back from now |
| Bars as the TradingView chart shows them, realtime feed (CFD, broker symbols) | `tv_session_ohlcv` / `tv_session_realtime` (opt-in cookie) | official bars are delayed and not tick-live |
| SMC/ICT detection (FVG, OB, structure, liquidity, sessions, prev H/L) | `tv_scan_fvg`, `tv_scan_ob`, `tv_scan_structure`, `tv_scan_liquidity`, `tv_scan_sessions`, `tv_scan_prev_hl` | official has no pattern tools |
| Pre-open level set: prev day/week/month, session H/L/O/C, ADR + projections, opening range + extensions, gaps | `tv_scan_levels` | official has no level computation |
| Chart images with markup, backtest trade renders | `tv_chart_render`, `tv_backtest_render_trades` | official has no charting |
| Backtests, declarative strategies, Pine compile, FX Replay journal | `tv_backtest_run`, `tv_strategy_list` / `tv_strategy_run`, `tv_pine_compile`, `tv_journal_scan` / `tv_journal_parse` | official has none of these |
| Read the user's own indicators / draw on the live chart / screenshots of the real app | `tv_desktop_screenshot`, `tv_desktop_list_studies`, `tv_desktop_read_study_plots`, `tv_desktop_draw` and siblings (opt-in CDP) | official has no desktop access |
| Diagnose this server's install | `tv_setup_doctor` (always registered, even in `hybrid`) | — |

Rule of thumb: **research and account → official; numbers you will act on
intraday, anything visual, anything computed → this server.**

## What the official server really does (verified 2026-09-19, beta)

- ~40 tools, prefixed `mcp-tv-*` / `mcp-watchlist-*` in Claude Code. Needs a
  TradingView **Essential or higher** plan (trial excluded), OAuth in the user's
  browser via `/mcp`. Rate limit about 100 requests/min.
- `get_ohlcv`: intervals 1m…1M, `count` ≤ 5000, `summary` mode. Works for
  futures (`CME_MINI:ES1!`) and broker CFDs (`PEPPERSTONE:US500`).
- Economic calendar: countries/currencies/category/importance filters,
  forecast + previous, history since 2003, forward ~31 days.
- News: up to 200 headlines per symbol, `paywall` flag, pagination, story body
  via `get_news_story`.
- Alerts: create/update/delete/list/log/stop/restart. Push, popup, email,
  webhook (webhook needs plan + 2FA).
- Watchlists: list/get/create/update(rename)/delete/add/remove, colored lists.

## Where it is thin (say so instead of retrying)

- **Bars are delayed 15+ minutes** and the last bar "may still change" (the tool
  says so in its `notice`). Never treat official bars as live for entries or
  sentinels; use `tv_session_realtime` or the broker.
- **No `end_time`, no paging** in `get_ohlcv`: only the most recent 5000 bars
  per interval. At 1m that is ~3.6 sessions of ES. Replaying a past day at 1m
  or building ADR from weeks of minute data → `tv_data_get_bars`.
- **Screener universe lacks broker CFDs**: `get_symbol_data`, `run_screener`
  and `search_symbols` return "no data" / nothing for `PEPPERSTONE:US500` while
  `get_ohlcv` serves it. Quote such symbols via `get_ohlcv(count=1)` or the
  session tools.
- **`get_technicals_rating` returns nulls on futures** (checked on ES1! 15m).
  Use `tv_ta_summary` (if `public` is enabled) or compute from bars.
- **Alerts are price-only** (cross / cross_up / cross_down / greater / less);
  indicator and multi-condition alerts are rejected. `get_alerts_log` came back
  empty over 60 days despite alerts that fired — treat the log as unreliable in
  beta.
- **No pre/post-market bars, split-adjusted only**, some exchanges served from
  end-of-day feeds. Provider is TradingView's — still name it as the feed.
- Nothing for charts, indicators, Pine, drawings, bar replay, backtests,
  Strategy Tester, or the desktop app.

## Setup for the pair

1. Install this plugin. The manifest registers **both** servers: `tradingview`
   (stdio, `TV_TOOLSETS` default `hybrid`) and `mcp-tradingview`
   (`https://mcp.tradingview.com/mcp`, HTTP).
2. Run `/mcp`, pick `mcp-tradingview`, authenticate in the browser (TradingView
   account with Essential+). Without a qualifying plan the official server just
   stays "needs auth" — this server keeps working on its own.
3. Optional tiers: set `TV_TOOLSETS=hybrid,session,desktop` plus `TV_SESSIONID` /
   `TV_CDP_URL` (see the `tradingview-tiers` skill). Optional free feeds:
   `OANDA_API_KEY`.
4. No official server available (plan too low, offline)? Set
   `TV_TOOLSETS=default,scan,chart,...` to bring back `public` — the unauth
   screener, TA summary and symbol search — and route everything here.

## Answering "do I need both?"

- Only research/quotes/alerts/watchlists → official alone (no install at all).
- Only scanning/charts/backtests/desktop → this server alone with
  `TV_TOOLSETS=default,...` (its own `public` screener stands in).
- Both jobs, which is the normal trading workflow → both, from this one plugin
  install plus one OAuth click. Each server is independent: if one is down the
  other keeps serving its half of the table above.
