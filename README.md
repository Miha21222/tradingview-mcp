# tradingview-mcp

MCP server giving AI agents a TradingView-centric trading toolkit: screener queries, historical FX data, and — as milestones land — SMC/ICT pattern scanning, chart rendering with markup, backtesting, Pine tooling, and a bundle of companion Agent Skills.

**Not affiliated with TradingView, Inc.** "TradingView", "Pine Script" and "Lightweight Charts" are trademarks of TradingView, Inc.

## Status

M0-M5 complete: server boots with `public`/`data` defaults; opt-in toolsets for SMC scanning, chart rendering, backtesting, FX Replay journal sync, TradingView-account data (`session`), Pine compile, Desktop CDP, and declarative strategies. Ships a `tv` CLI (every tool = a subcommand emitting JSON), 8 companion skills, and a validated Claude Code plugin manifest. Open items: M1's trust gate still awaits the owner's real hand-labeled SMC setups; webhook receiver, MCP Apps, and the sandboxed Python strategy escape hatch are deferred. Roadmap: `docs/PLAN.md`.

## Quick start

```powershell
uv sync
uv run python -m tvmcp --check    # list registered tools
uv run python -m tvmcp            # run on stdio
uv run tv --check                 # CLI: list tools
uv run tv tv_data_get_bars '{"symbol":"EURUSD","count":10}'   # CLI: call any tool -> JSON
```

The opt-in `chart` toolset renders via headless Chromium; run `uv run playwright install chromium` once if you enable it. Enable opt-in toolsets via `TV_TOOLSETS=default,scan,chart,backtest,journal`.

Claude Code: the repo's `.mcp.json` registers the server automatically when you trust the project.

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `TV_TOOLSETS` | `default` (= `public,data`) | Comma list; `all` enables everything. Opt-ins: `scan` (SMC scanning), `chart` (PNG rendering), `backtest`, `journal`, `pine` (compile/typecheck), `strategy` (declarative YAML specs), `session` (TV-account data, ToS risk), `desktop` (CDP to TradingView Desktop, ToS risk) |
| `TV_READ_ONLY` | off | `1` = write-capable tools never register (wins over everything) |
| `TV_CACHE_DIR` | `~/.tvmcp/cache` | Parquet OHLCV cache |
| `TV_CHART_DIR` | `~/.tvmcp/charts` | Rendered PNG output (managed, collision-safe filenames) |
| `TV_JOURNAL_DIR` | `~/.tvmcp/journal` | FX Replay CSV watch-folder for `tv_journal_scan` / `tv_journal_parse` |
| `TV_STRATEGY_DIR` | `~/.tvmcp/strategies` | YAML strategy specs for `tv_strategy_list` / `tv_strategy_run` |
| `TV_SESSIONID` | — | Your TradingView `sessionid` cookie gates the opt-in `session` toolset (ToS risk; see below). Live fetch works: the cookie is exchanged for a websocket JWT and fed to tvdatafeed-enhanced |
| `TV_CDP_URL` | `http://127.0.0.1:9222` | CDP address of TradingView Desktop for the opt-in `desktop` toolset (launch via `scripts/start-tv-desktop.ps1`, which defaults to port **9223** — set this var to match; another CDP app may own 9222) |
| `TV_MAX_BARS` | `5000` | Hard cap on bars per response |
| `OANDA_API_KEY` | — | Free practice-account key enables the OANDA provider |
| `OANDA_ENV` | `practice` | `practice` or `live` |

Run `uv run tv tv_setup_doctor` to check every prerequisite - each broken check returns the exact fix command.

## TradingView Terms of Service

The default toolsets use no TradingView account: the `public` toolset talks to TradingView's public scanner endpoint, and market data comes from Dukascopy/OANDA. TradingView's ToS prohibits automated data collection and non-display use of its data; the optional `session` and `desktop` toolsets (off by default) interact with **your own** TradingView account/desktop app at **your own risk** — enabling them is your explicit choice and may violate TradingView's ToS and risk account restrictions. The `session` toolset prints a ToS/ban warning to stderr on first use and requires `TV_SESSIONID`. This project is for personal research/education.
