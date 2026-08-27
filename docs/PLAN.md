# tradingview-mcp — architecture & build plan

Source: deep research 2026-08-25 (3 parallel research agents). Full report (Russian): owner's Obsidian vault, note `07 - TradingView MCP - исследование и дизайн.md` in `Trading\Trading Strategy Assistant`. This file is the English working copy for agents building the project.

## Context

Owner is an SMC/ICT-style forex trader (FVG, order blocks, BOS/CHoCH, liquidity, killzones; backtests in FX Replay, journals in Notion, TradingView Essential/Plus plan). Goal: a TradingView MCP server any AI agent can pick up from the get-go — connect to TradingView, read historical data, view/draw on/interact with charts, scan for patterns, backtest strategies — customizable, extensible, shipped with a bundle of reusable Agent Skills.

Owner decisions: unofficial TV access acceptable (personal use) · chart route = BOTH own engine (primary) and optional real-TV automation · personal tool first, publishable later.

## Core design stance

1. **Compute-first.** Pattern detection is algorithmic (OHLCV in, JSON out). Vision LLMs score ~chance (51%) at raw chart pattern detection — vision is a verification layer only ("does this rendered markup match this JSON?"), never the detector.
2. **Render our own charts.** Lightweight Charts v5 (Apache-2.0, TradingView's own OSS) + custom Series/Pane Primitives (FVG boxes, OB rects, BOS/CHoCH lines, killzone shading) in headless Chromium → PNG with a coordinate grid stamped in (the one vision aid with measured effect). TradingView's real UI is an optional opt-in module, never the backbone (ToS + brittleness: the leading CDP-based MCP has 218 open issues at 4 months old).
3. **Risk-tiered TradingView access** (see Toolsets): unauthenticated scanner by default (no account = no ban surface) → session cookie → desktop CDP → webhooks. TV ToS prohibits scraping "regardless of purposes"; riskier tiers are explicit opt-in with warnings.
4. **Numbers are the authority.** Every published level traces to a named detector on a named feed with stated parameters; the chart is an artifact of the computation.

## Architecture

### Stack

- **Server:** Python 3.12, `fastmcp` (PrefectHQ standalone; the only layer serving both MCP protocol generations — Claude Code stdio clients default to the pre-2026-07-28 protocol). Pin `>=3.4,<4`; migrate to 4.x at GA.
- **Transport:** stdio. Long jobs (backtest sweeps) → MCP tasks pattern, not held streams.
- **State:** explicit handles as tool args (`backtest_id`, `chart_id`) — protocol is stateless.

### Toolsets (registration-gated via `TV_TOOLSETS`, default `public,data`)

| Toolset | Default | Contents | TV account needed |
|---|---|---|---|
| `public` | yes | screener queries, TA summaries, symbol search (`tradingview-screener`, unauth scanner) | no |
| `data` | yes | OHLCV via Dukascopy (free tick history, `npx dukascopy-node`) + OANDA v20 (practice account, real spreads); Parquet cache; provider status | no |
| `scan` | M1 | `tv_scan_*`: FVG / OB / BOS-CHoCH / liquidity+sweeps / sessions-killzones / prev H-L (wraps pinned `smartmoneyconcepts` + own IFVG/breaker/SMT layer) | no |
| `chart` | M2 | render + markup + PNG export (LWC v5 headless) | no |
| `backtest` | M3 | `backtesting.py` + forex adapter (pip value, JPY quirk, R-sizing, spread, session tags) | no |
| `pine` | M4 | `tv_pine_compile` via `pine-facade/translate_light` (undocumented compile/typecheck endpoint — compiler-errors-in-a-loop) | no |
| `session` | M4, opt-in | realtime scanner + `tvdatafeed-enhanced` OHLCV (5k bars/req wire cap; owner plan: 10k intraday bars, 180/365d minute history) + chart-img layout snapshots | sessionid cookie |
| `desktop` | M4, opt-in | CDP → TradingView **Desktop** (`--remote-debugging-port=9222`, Playwright `connect_over_cdp`): real chart control, drawings, Pine editor, Strategy Tester, bar replay | paid TV + Desktop app |
| `journal` | M3+ | FX Replay CSV watch-folder ingest → schema-sniffing normalizer → structured JSON (Notion write via user's Notion MCP; conventions from vault note 06) | no |
| `strategy` | M5 | user plugin dirs (`strategies/`, `indicators/`) via FastMCP Provider; default surface = `tv_strategy_list` + `tv_strategy_run(name, params)` dispatcher only | no |
| `webhook` | later | local receiver for TV alert webhooks (`{{plot()}}`/`{{strategy.*}}` placeholders — the only sanctioned push channel out of TV) | Essential+ & 2FA |

Env config: `TV_TOOLSETS` (comma list; `default`, `all` special), `TV_TOOLS` (additive singles), `TV_READ_ONLY=1` (hard-wins), `TV_CACHE_DIR`, `TV_MAX_BARS` (default 5000), `OANDA_API_KEY`, `OANDA_ENV=practice|live`, later `TV_SESSIONID`, `TV_CDP_URL`, `TV_STRATEGY_DIR`, `TV_ALLOW_CODE_EXEC`.

### Key library choices (with reasons)

| Concern | Choice | Why / rejected alternatives |
|---|---|---|
| Screener | `tradingview-screener` (shner-elmo, MIT) | Best-maintained TV lib (pushed Aug 2026); pure JSON POST, no scraping. `tradingview-ta` abandoned — reimplement TA summary via scanner fields. |
| TV OHLCV (opt-in) | `tvdatafeed-enhanced` | Live fork; async multi-fetch, token cache. Original `tvdatafeed` dead since 2024. `@mathieuc/tradingview` (Node) has NO license — reference only, never vendor. |
| Backtest data | Dukascopy (`npx dukascopy-node`) + OANDA v20 | Free tick history to ~2000s / best FX REST with real spreads. yfinance disqualified (FX intraday ≤60 days). |
| SMC detection | `smartmoneyconcepts` (joshyattridge, MIT) — **pin exact version** | Covers fvg/swings/bos_choch/ob/liquidity/sessions/retracements. 4+ active forks = contested logic → M1 regression gate vs hand-labeled setups is mandatory. Missing (write ourselves): IFVG, breaker blocks, SMT divergence, PD arrays, silver-bullet windows. `swing_length` default 50 too coarse for M5/M15 — expose, tune 5–15. |
| Candle patterns | `pandas-ta-classic` | 62 patterns without TA-Lib C dependency; original pandas-ta unmaintained. |
| Backtesting | `backtesting.py` | Best LLM-codegen ergonomics (15-line Strategy class; `stats['_trades']` DataFrame → CSV/JSON free; built-in optimize). Caveat: fills at next bar open unless `trade_on_close=True`. vectorbt OSS = maintenance mode (sweeps only); backtrader frozen; zipline wrong asset class; nautilus_trader only if strategies go live. |
| Charts | Lightweight Charts v5 + custom primitives, headless Chromium → PNG | Apache-2.0, TV's own visual language, unlimited shapes. chart-img.com caps studies+drawings at 3–25/plan — one SMC markup needs 20–40 shapes → prototype only. `deepentropy/lightweight-charts-drawing` (68 tools) has NO license — read, don't copy. |
| Pine | `pine-facade/translate_light` (compile/typecheck); PyneCore optional (Pine→Python execution model, Apache-2.0) | No official Pine execution/backtest API exists. |

### Skills bundle (M5, `skills/`, spec-clean Agent Skills)

`smc-scanning` · `chart-markup` · `strategy-backtesting` (walk-forward, look-ahead traps, min-sample rules) · `pine-authoring` · `risk-sizing` (pure judgment, zero tools) · `market-screening` · `strategy-review` (adversarial checklist sourced from the owner's own framework: 5 blocks + 27 checklist items, vault note `02 - Каркас 5 блоков и чек-лист` — do NOT invent a generic one) · `journal-sync` (FX Replay→Notion; mapping conventions from vault note 06: Buy/Sell→Side relation, day-code→Дни недели page ids, UTC-hour→Сессии bands).

Companion-skills pattern is the market differentiator — no existing TradingView MCP ships skills. SKILL.md ≤500 lines; description = what + when + trigger words + tools fronted; detail into `references/`; helpers into `scripts/`.

### Extensibility (M5)

User dirs `strategies/` + `indicators/` (under plugin data dir), scanned by a custom FastMCP Provider, validated against a Pydantic spec, hot-reload + `tools/list_changed`. Declarative YAML specs first (composed from vetted primitives — no sandbox needed); Python escape hatch runs in a sandboxed subprocess: no network, no credentials in env, CPU/mem/wall caps, JSON-only results. Never `exec()` in the server process.

### Security

- MCPTox benchmark: >60% attack success via poisoned tool metadata → all TV-sourced strings are untrusted data.
- Two-step prepare/confirm for every write tool (alerts, Pine inject): `*_prepare` returns draft + `confirmation_token`; `*_submit(confirmation_token)` validates the draft still matches.
- `TV_READ_ONLY=1` default posture for account-touching toolsets.
- README + startup banner + session/desktop tool descriptions all carry the ToS disclaimer and "not affiliated with TradingView, Inc."

### Distribution (post-M5, when publishing)

Claude Code plugin in own marketplace repo (bundles server + skills + hooks + `userConfig` credential prompts) → `.mcpb` bundle for Claude Desktop → `server.json` on registry.modelcontextprotocol.io. Optional high-leverage: MCP Apps (`ui://` iframe) for interactive equity curves / screener grids.

## Milestones

- [x] **M0 — scaffold + public data backbone** (2026-08-25)
  - [x] Repo skeleton: pyproject (uv), CLAUDE.md, docs/PLAN.md, .mcp.json, plugin.json stub, tests
  - [x] fastmcp server boots on stdio; `--check` mode lists registered tools
  - [x] `public` toolset: `tv_screener_run`, `tv_ta_summary`, `tv_symbol_search`
  - [x] `data` toolset: `tv_data_get_bars` (Dukascopy + OANDA providers), `tv_data_providers_status`
  - [x] Symbol-mapping layer (`symbols.py`), Parquet cache (`cache.py`)
  - [x] Unit tests green (no-network); network smoke test of screener + Dukascopy
- [~] **M1 — SMC scan tools + trust gate** (scan toolset + regression harness landed; own-layer detectors and the trust gate's real hand-labeled fixtures are pending owner data)
  - [x] `scan` toolset wrapping pinned `smartmoneyconcepts` (0.0.27): `tv_scan_fvg`, `tv_scan_ob`, `tv_scan_structure`, `tv_scan_liquidity`, `tv_scan_sessions`, `tv_scan_prev_hl` (consolidated args: symbol, timeframe, count, provider, swing_length; swing-based detectors emit a `repaint_note`)
  - [~] Regression fixtures in `tests/fixtures/labeled_setups/` + suite (`tests/test_scan_regression.py`) gating the pinned lib. Seeded with synthetic + edge fixtures (fvg bull/bear, ob, structure, liquidity, sessions, prev_hl, flat/equal, nan-volume). **Pending:** 20–30 real hand-labeled setups from owner's charts backfilled as `kind: "labeled"`.
  - [ ] Own-layer detectors where lib fails or lacks: IFVG, breakers, SMT, killzone-scoped setups (blocked on real labeled regressions to prove the gap)
  - [x] `smc_h4_m15` vetted backtest strategy (2026-08-26) — first end-to-end SMC verification vehicle toward the trust gate: H4 bias from resampled M15 via `smc.bos_choch` (bias active only from the CLOSE of the breaking H4 bar), M15 FVG retrace entries (zone live only after the third candle closes, retrace strictly later), SL at far gap edge, TP at `rr`, risk-sized like breakout. Params: swing_length/rr/risk_amount/expiry_bars/use_choch/min_stop_frac; tool guards timeframe < H4. Verified live on EURUSD M15 3000 bars (66 trades, stop-outs ≈ −1.0R — engine math coherent; raw v1 unprofitable, WR 22.7% at 2R — needs killzone/sweep filters, NOT a tradeable claim). No-look-ahead proven by prefix-stability regression test (truncating history must not change earlier trades). **Landmines: `smc.fvg` flags the MIDDLE candle — the zone exists only once candle i+1 closes (Top = third-candle low, Bottom = first-candle high for bullish); backtesting.py raises ValueError when the next-open fill (plus spread) lands outside the SL/TP bracket — catch and skip, and floor tiny stops (`min_stop_frac`) or tight-TP orders die at fill**
- [x] **M2 — chart rendering**
  - [x] `chart/` LWC v5 page + primitives (FVG box, OB rect, BOS/CHoCH line, killzone band, coordinate grid) built to a static bundle (vendored `lightweight-charts` 5.2.1, overlay primitives in headless Chromium)
  - [x] `chart` toolset: `tv_chart_render(symbol, timeframe, markup_json) → PNG path`, golden-image tests (deterministic, DPR=1/UTC/Arial, byte-match hash)
- [x] **M3 — backtesting + journal**
  - [x] Forex adapter (`src/tvmcp/backtest/forex.py`): pip size incl. JPY, risk-based position sizing, R-multiple, spread-as-relative (exact at a reference price; backtesting.py charges spread once at entry), UTC session tags; explicit quote→account rate required when currencies differ (table-tested)
  - [x] `backtest` toolset (`tv_backtest_run`) on pinned `backtesting.py` 0.6.6 with vetted strategies only (sma_cross, breakout); read-only; risk sizing from the actual stop distance; account-currency/quote-rate/spread/fill-model exposed; bounded stats + capped trades with R/physical-units/session. Deferred (non-gating): trade JSON/CSV export as a separately-gated write tool honoring `TV_READ_ONLY`; long-run task pattern until sweeps exist
  - [x] `tv_backtest_render_trades` (2026-08-27) — visual sanity check of backtest fills: re-runs the (deterministic) backtest and renders the most recent N closed trades to PNGs in the managed chart_dir via the chart module's Lightweight Charts renderer. Each image: context bars before entry, entry line (actual fill price), SL (red) + TP (green) lines, exit line labeled with the R-multiple, killzone band shading the trade's lifetime; `extra_markup_json` layers any tv_chart_render markup onto every image. Trades now carry `tp` alongside `sl`. Verified live on smc_h4_m15 EURUSD M15: winner exits at TP but realized R < rr (next-open fill + spread — expected), −1R losers visibly tag the SL. Renderer injectable for tests (no Chromium in CI path)
  - [x] Screenshot customization layer (2026-08-27) — closes the "users teach their own look" gap: markup_json v1 extended additively (optional hex `color` on every primitive, `label` on boxes, new `text` (time+price+1-80 chars) and `marker` (up/down arrow at time+price) primitives — schema in `chart/markup.py`, rendering in `chart/static/app.js`); `tv_chart_render` gained `end_time` (ISO UTC anchor: charts the `count` bars ENDING there, future rejected, result echoes actual last bar) so any historical trade can be windowed. Loader signature grew an optional trailing `end_ts=None` (chart toolset only). Custom style lives in user skill files (agent composes markup_json per their conventions) — no code changes needed per user. Verified live: historical window + colored FVG box + text + marker render correctly
  - [x] Validation: reproduced the owner's 10-trade EURUSD FX Replay dataset from a **sanitized** export fixture (`tests/fixtures/journal/fxreplay_sample.csv` — trade IDs and strategy tags scrubbed; prices/pnl/times preserved) — WR=30%, expectancy≈-0.33, total P&L≈-3.26, avg R≈-0.003; per-trade R cross-validates the engine's R-math
  - [x] `journal` toolset: on-demand `tv_journal_scan` watch-folder scanner + `tv_journal_parse` normalizer built against a genuine FX Replay export (buy/sell→side, day-code→weekday, UTC-hour→session, tags; symbols resolved). Notion writes stay external (owner's own Notion MCP, later)
- [x] **M4 — TradingView opt-in tiers + Pine**
  - [x] `session` toolset (`tv_session_status/ohlcv/realtime`): opt-in only, first-use ToS/ban warning to stderr, actionable missing-`TV_SESSIONID` errors, credential-free mocked tests. Live wiring (2026-08-26): `session/client.py` exchanges the `sessionid` cookie for a websocket JWT via the JSON endpoint `https://www.tradingview.com/quote_token/` (`/accounts/current/` is dead — 404) and injects it into `tvdatafeed-enhanced`; verified live (realtime OANDA:EURUSD H1 bars + M1 quote through the owner's account). Deferred (non-gating): realtime screener via session cookie, chart-img snapshots, Parquet cache integration for session bars
  - [x] `pine` toolset (`tv_pine_compile` via `translate_light`; owner authorized the endpoint 2026-08-26 — CLAUDE.md rule 1 updated): POST source → real compiler verdict. **Endpoint quirk: `success: true` even with errors — errors live in `result.errors2[]` with `start.line/column`**; the tool derives success = processed AND no errors. Offline tests on live-captured payloads + `network`-marked live roundtrip (passing)
  - [x] `desktop` toolset first slice (`tv_desktop_status/screenshot/set_symbol/set_timeframe`): opt-in only, first-use ToS warning, navigation tools excluded under `TV_READ_ONLY=1`. **Driver is raw CDP over `websocket-client`, NOT Playwright** — Playwright's `connect_over_cdp` hangs on TradingView's Electron browser target (ws connects, attach never completes); `/json/list` → chart page target → Runtime.evaluate / Input.dispatchKeyEvent / Page.captureScreenshot works. Launch via `scripts/start-tv-desktop.ps1` (Store MSIX app, no shortcut needed; **default port 9223 — 9222 is taken by wmux on this machine and Chromium silently skips a busy port**; set `TV_CDP_URL=http://127.0.0.1:9223`). Verified live in the owner's logged-in app: symbol GBPUSD→EURUSD, interval →1ч, screenshot of the real chart with the owner's indicators. Deferred (next slice): bar replay, Pine editor automation, Strategy Tester reads
  - [x] `desktop` drawings slice (2026-08-26: `tv_desktop_list_drawings` read-only; `tv_desktop_draw`/`tv_desktop_remove_drawing` write-gated): draws on the live chart via the in-page `window.TradingViewApi.activeChart()` charting API (`createShape`/`createMultipointShape`/`getAllShapes`/`removeEntity`) — no UI clicking, exact {time, price} anchors, shapes behave as hand-made (movable, layout-autosaved). Kinds: rectangle/trend_line/ray/horizontal_line/vertical_line/text; hex color + fill opacity; single-entity remove only (no remove-all by design — chart holds the user's own drawings). Verified live: FVG box + trendline + level on the owner's EURUSD chart, listed with points/text, removed cleanly. **Landmines: (1) create* returns an opaque EntityId object that serializes to `{}` over `returnByValue` — get string ids by diffing `getAllShapes()`; (2) `getAllShapes()` lags creation by a tick in the desktop build — the draw JS must poll (awaitPromise) or the diff comes back empty while the shape IS created (phantom shapes on retry); (3) `exportData` is not supported in the desktop build ("Data export is not supported") — price anchors come from `getPanes()[0].getMainSourcePriceScale().getVisiblePriceRange()`, time anchors from `getVisibleRange()`**
  - [x] `desktop` studies slice (2026-08-27: `tv_desktop_list_studies` / `tv_desktop_read_study_plots` / `tv_desktop_read_study_graphics`, all read-only — register under `TV_READ_ONLY=1`): reads the user's own indicators off the live chart. List = id/title/visibility/pane/state/plot declarations/user inputs/graphics counts; plots = numeric series rows `[time, ...]` via `getStudyById(id)._study.data()`; graphics = Pine `box.new`/`line.new`/`label.new` output (FVG/OB zones) with real time+price coords. Verified live on the owner's chart: LuxAlgo Imbalance Detector boxes (505, colors decode to LuxAlgo blue/orange), Dark Trader session labels (Frankfurt/London at correct opens), DXY symbol-overlay OHLCV rows. **Landmines: (1) protected Pine scripts carry their encrypted source as a multi-KB hidden `text` input — inputs must be filtered to visible ones or one study blows the output budget; (2) `graphics()._primitivesCollection` dwg* collections nest Map(name → Map(? → store)), primitives in the store's `_primitivesDataById` Map; (3) box/line `x1/x2` are SERVER graphic indexes — translate via `graphics()._indexes[x]` (−2000000 = before loaded history) then `series().bars().valueAt(ti)[0]` for unix time (mapping through the study's own `data()` gives future-shifted garbage); (4) colors are ARGB uint32 but small values with zero high byte are theme-palette indexes, not colors; (5) hidden (eye-toggled-off) studies are unloaded by TV — `data()` empty, tool says so**
- [x] **M5 — skills + packaging**
  - [x] `strategy` toolset (`tv_strategy_list`, `tv_strategy_run`): declarative YAML specs composed from vetted built-in primitives only (no user/LLM code, no `exec()`); runs through the backtest engine; path-traversal-guarded
  - [x] 10 companion skills (smc-scanning, chart-markup, strategy-backtesting, pine-authoring, risk-sizing, market-screening, strategy-review from the owner's 5-blocks/27-item audit framework, journal-sync from the owner's FX Replay→Notion mapping; added 2026-08-26: tvmcp-guide — full orientation + symptom→fix troubleshooting table distilled from the milestone landmine log, and tradingview-tiers — session/desktop setup, ToS framing, failure recovery); contract-tested (frontmatter, <500 lines, tool-refs valid)
  - [x] `tv` CLI parity (every registered tool = a subcommand emitting JSON on stdout; `tv --check`); console script in the wheel, verified from a clean install
  - [x] Plugin packaging: plugin.json userConfig (TV_SESSIONID/OANDA_API_KEY sensitive, no defaults; operator settings TV_TOOLSETS/TV_READ_ONLY/TV_MAX_BARS/OANDA_ENV/TV_CHART_DIR/TV_JOURNAL_DIR/TV_STRATEGY_DIR/TV_CDP_URL), inline mcpServers bridging userConfig→env, marketplace.json; `claude plugin validate .` passes; packaged skills carry no private UUIDs/paths (contract-tested)
  - [x] Setup self-diagnosis (2026-08-26): `tv_setup_doctor` in the default `public` surface — checks node/npx, Playwright Chromium, OANDA key, TV_SESSIONID, desktop CDP (verifies the listener actually has a tradingview.com tab — any CDP app can own the port, e.g. wmux on 9222; when the port is dead but a TradingView.exe process exists, the check says the app runs WITHOUT the debug flag and the fix is Stop-Process + flagged relaunch with an ask-the-user-first note — the app is single-instance, a plain relaunch only focuses the flagless copy; launcher path in the fix is absolute), journal/strategy dirs; every broken check carries the exact `fix` shell command so an agent self-heals. Server never auto-installs; credential fixes are marked manual by policy. Failure paths (Dukascopy missing npx, chart missing Chromium) raise actionable errors pointing at the fix + the doctor
  - [ ] Optional (deferred): webhook receiver, MCP Apps, M5 `strategy` sandboxed Python escape hatch

## Verification per milestone

Each milestone lands with: unit tests green (`uv run pytest`), boot check (`uv run python -m tvmcp --check`) listing the expected tool surface, and a live smoke test through a real MCP client where feasible. M1 additionally gates on the labeled-setup regression suite; M3 gates on reproducing the known FX Replay dataset stats.
