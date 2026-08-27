# Troubleshooting: symptom → cause → fix

Start with `tv_setup_doctor` — it diagnoses prerequisites and returns exact fix
commands. This table covers everything else, per toolset.

## Data (`tv_data_get_bars`)

| Symptom | Cause | Fix |
|---|---|---|
| "npx not found on PATH" | Node.js missing | `winget install OpenJS.NodeJS.LTS`, restart terminal |
| First fetch takes minutes | Dukascopy serves hourly .bi5 files; cold fetch is slow | Expected. Results land in the Parquet cache; repeats are instant |
| "dukascopy-node timed out" | Range too wide for one call | Narrow the date range / lower `count`; fetch in chunks |
| Zero or few bars on a weekend/holiday | FX market closed — no candles exist | Not an error. Use a longer window or accept the gap |
| Bars end before "now" | Cache serves the stored range; market closed since | Pass a `count` reaching past the cached edge to trigger a refetch |
| Stale/corrupt-looking data | Parquet cache issue | Delete the symbol's file under the cache dir (default `~/.tvmcp/cache`) and refetch |
| OANDA errors / missing | `OANDA_API_KEY` unset or practice-vs-live mismatch | Set the key (free practice account) and `OANDA_ENV`; or use `provider="dukascopy"` |

## Screener (`tv_screener_run`, `tv_ta_summary`)

| Symptom | Cause | Fix |
|---|---|---|
| 0 rows for forex/crypto | Upstream library seeds stock-only default filters | Handled internally (filters stripped for non-stock markets) — if you still get 0, check the `market` argument matches the instrument class |
| Unknown field errors | Scanner field name typo | See `market-screening` skill's `references/scanner-fields.md` |
| Rate-limit / HTTP 429 | Too many unauthenticated scanner calls | Back off; the endpoint throttles by IP |

## Scan (`tv_scan_*`)

| Symptom | Cause | Fix |
|---|---|---|
| Detections at the right edge look wrong | Swing detectors classify using future candles | Honor `repaint_note`: ignore signals within the trailing `swing_length` bars |
| Everything detected everywhere / nothing at all | `swing_length` mismatched to timeframe | 5–15 intraday, larger on H4/D1 (library default 50 is too coarse for M5/M15) |
| "Unknown session" | Session name not in the allowed list | Use the exact names the error lists (note: "london close kill zone" is lowercase in the library) |
| Results truncated | 100-result cap | Narrow `count` or the window; check `total_count` vs `returned_count` |

## Chart (`tv_chart_render`)

| Symptom | Cause | Fix |
|---|---|---|
| "Headless Chromium failed to launch" | Playwright browser not installed | `uv run playwright install chromium` |
| "markup ... is outside the loaded bar range" | Markup timestamp not covered by the `count` bars loaded | Enlarge `count` or correct the time (weekends have no bars!) |
| "only N bars available" | Asked to render more than exists | Lower expectations or widen the data range |
| Grid time labels overlap the axis | Known cosmetic issue | Ignore or disable `grid` in markup_json |

## Backtest (`tv_backtest_run`) and strategy (`tv_strategy_run`)

| Symptom | Cause | Fix |
|---|---|---|
| "quote currency X != account currency Y" | Cross-currency P&L needs an explicit rate | Pass `quote_to_account_rate` (e.g. `1/150` for USD account on USDJPY) |
| Zero trades | Strategy conditions never fired on this window | Widen `count`, adjust params — not a bug |
| Results look too good | No slippage/swap modeled; spread charged on entry only | Report with those caveats; verify with a wider window |
| "'name' is not a YAML file directly inside ..." | `tv_strategy_run` takes the spec FILE name | Pass `name="my-spec.yaml"` (with extension); list first via `tv_strategy_list` |
| "unknown strategy" in a spec | YAML `strategy:` must name a vetted built-in | Use one from the error's `available:` list; new primitives require a code change |

## Journal (`tv_journal_scan/parse`)

| Symptom | Cause | Fix |
|---|---|---|
| "Journal folder ... does not exist" | Watch folder missing | Set `TV_JOURNAL_DIR` to the FX Replay export folder (or create it) |
| "missing required FX Replay columns" | CSV is not an FX Replay trade export (or format changed) | Check the listed missing columns; only genuine exports parse |
| Times look shifted | Export not in UTC | Pass `utc_offset_hours` to `tv_journal_parse` |

## Pine (`tv_pine_compile`)

| Symptom | Cause | Fix |
|---|---|---|
| success=false with errors[] | Real compiler errors — that is the feature | Fix the reported line/column, recompile (loop) |
| HTTP error / non-JSON | Undocumented endpoint changed or is down | Retry later; if persistent, the endpoint may be gone — report upstream, do not scrape alternatives |

## Session (`tv_session_*`)

| Symptom | Cause | Fix |
|---|---|---|
| "TV_SESSIONID not set" | Cookie not configured | Human copies the `sessionid` cookie from a logged-in tradingview.com tab into the env var. Never fetch credentials yourself |
| "TradingView rejected the session cookie (HTTP ...)" | Cookie expired/invalidated | Human re-copies a fresh cookie; cookies rot on logout/password change |
| "Using anonymous access" in stderr | Library log before the JWT is injected | Harmless — the cookie-derived token replaces it before any data call |
| Fewer bars than asked | 5000/request wire cap and `TV_MAX_BARS` | Expected ceiling |

## Desktop (`tv_desktop_*`)

| Symptom | Cause | Fix |
|---|---|---|
| "Cannot reach TradingView Desktop CDP" | App not running, or running WITHOUT the debug flag | Launch via `scripts/start-tv-desktop.ps1`; an instance started normally must be closed first (single-instance: a second launch only focuses it) |
| CDP answers but "NOT TradingView Desktop" | Another CDP app owns the port (common: 9222) | Use the launcher's port (9223) and set `TV_CDP_URL` to match |
| "no TradingView chart tab" | App open but no chart / not logged in | Human opens a chart layout in the app |
| set_symbol picked the wrong listing | TV quick-search matched another exchange | Pass the exchange-qualified form (`OANDA:EURUSD`); verify with `tv_desktop_screenshot` |
| Tools broke after a TradingView update | UI selectors/keyboard flows changed | Known brittleness of the desktop tier; report it — the driver lives in `src/tvmcp/desktop/driver.py` |
| tv_desktop_draw errors "no new drawing appeared" | Shape kind unsupported by this app build, or `getAllShapes()` lag (the draw JS polls ~2s) | Try another kind; verify with `tv_desktop_screenshot` — the shape may exist despite the error, so `tv_desktop_list_drawings` before retrying (avoids phantom duplicates) |
| tv_desktop_draw "page does not expose TradingViewApi" | Chart still loading, or a non-chart tab won target selection | Wait for the chart to render and retry |
| tv_desktop_read_study_plots: "study has no data" | The study is hidden (eye toggled off) — TV unloads hidden studies | Ask the user to toggle it visible, then retry |
| Study query "matches no study" / "is ambiguous" | Wrong id or too-loose title substring | The error lists all present studies; use `tv_desktop_list_studies` and pass the id or a longer substring |
| read_study_graphics times look off for old objects | Objects before loaded history get extrapolated times (bar-spacing based, session gaps ignored) | Scroll the chart left to load more history, or treat pre-history times as approximate |
| read_study_graphics empty for an indicator that clearly plots | Indicator uses plot()/plotshape(), not Pine box/line/label objects | Use `tv_desktop_read_study_plots` (with `nonempty_only=true` for sparse signals) |

## Everywhere

| Symptom | Cause | Fix |
|---|---|---|
| A tool is missing entirely | Its toolset is not enabled | Set `TV_TOOLSETS` (e.g. `default,scan,chart`); `all` enables everything |
| Write-ish tools missing | `TV_READ_ONLY=1` | Deliberate; unset only if the human wants mutation |
| stderr ToS warning on session/desktop | First-use warning, by design | Acknowledge it to the user once; it prints once per process |
