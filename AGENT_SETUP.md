# AGENT_SETUP.md — install and health-check this MCP (for AI agents)

You are an AI agent (Claude Code, Codex, Cursor, Gemini CLI, ...) and a human
told you: *"install the TradingView MCP from this repository and check it
works"*. Follow this file top to bottom. Every step has a verification command;
do not skip verifications, and do not invent credentials — the human supplies
them when a step says so.

Result when you are done: two MCP servers registered in the human's client —
`tradingview` (this repo, local, stdio) and `mcp-tradingview` (TradingView's
official remote server, HTTP + OAuth) — plus one green health report.

---

## 0. What you are installing (30-second orientation)

| Server | What it does | Needs |
|---|---|---|
| `tradingview` (this repo) | free OHLCV history (Dukascopy/OANDA), SMC/ICT scans (FVG/OB/structure/liquidity), chart PNG rendering, backtests, declarative strategies, Pine compile, FX Replay journal, opt-in TradingView Desktop control | Python 3.12+, uv, Node 22+ (Dukascopy), Chromium (charts) |
| `mcp-tradingview` (official) | quotes, screener, symbol search, news, economic/earnings calendars, fundamentals, price alerts, watchlists | TradingView account with **Essential+** plan, OAuth in the human's browser |

They are independent. If the human has no Essential+ plan, this repo alone
still works (use `TV_TOOLSETS=default,...` — see step 6).

Hard rules for you, the agent:
- Never try to obtain, read, or guess credentials (`TV_SESSIONID`, `OANDA_API_KEY`,
  OAuth tokens). Ask the human, once, only when a step says a credential is needed.
- Never scrape tradingview.com. The server does not, and neither do you.
- Do not enable `session` / `desktop` toolsets unless the human asks: they use the
  human's own TradingView account and may violate TradingView's ToS.

---

## 1. Prerequisites

Check each; install only what is missing.

```bash
python --version        # need 3.12 or newer
uv --version            # need uv (https://docs.astral.sh/uv/)
node --version          # need 22+ (Dukascopy data provider runs `npx dukascopy-node`)
git --version
```

Install hints (pick the platform):

| Tool | Windows (PowerShell) | macOS | Linux |
|---|---|---|---|
| uv | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | same as macOS |
| Python 3.12 | `uv python install 3.12` | `uv python install 3.12` | `uv python install 3.12` |
| Node 22 | `winget install OpenJS.NodeJS.LTS` | `brew install node` | distro package or nvm |

After installing uv, open a **new** shell so it is on PATH.

---

## 2. Get the code

```bash
git clone https://github.com/Miha21222/tradingview-mcp.git
cd tradingview-mcp
uv sync                              # creates .venv with pinned dependencies
uv run playwright install chromium   # headless Chromium for chart rendering (~150 MB, once)
```

Verify:

```bash
uv run python -m tvmcp --check
```

Expected: `tradingview-mcp: N tools registered` followed by tool names. With no
env set, N = 6 (`public,data` default). Any traceback here = stop and fix
(`uv sync` again, check Python version).

---

## 3. Run the health check (one command)

```bash
uv run python scripts/healthcheck.py
```

It prints a table: Python, uv, Node, Chromium, registered tool count, the
server's own `tv_setup_doctor` findings, and whether the two MCP servers are
registered in Claude Code (if the `claude` CLI is on PATH). Each failing line
carries the exact fix command. Run the fixes for `required` items, rerun until
`HEALTH: OK`. `optional` items (OANDA key, TV cookie, Desktop CDP, journal /
strategy folders) may stay red — say so in your report, do not "fix" them by
inventing values.

`--json` gives machine-readable output; exit code 0 = healthy, 1 = a required
check failed.

---

## 4. Register the servers in the human's MCP client

### Claude Code (preferred: plugin, both servers in one step)

```bash
claude plugin marketplace add Miha21222/tradingview-mcp
claude plugin install tradingview-mcp@tradingview-mcp-marketplace
```

The plugin registers **both** servers and prompts for optional settings. Defaults
are fine: `TV_TOOLSETS=hybrid` (everything the official server lacks). Leave
credential prompts empty unless the human gives values.

### Claude Code (manual, when the plugin route is unavailable)

Local server — run from anywhere, use the absolute clone path:

```bash
# Windows
claude mcp add -s user tradingview -e TV_TOOLSETS=hybrid -- uv --directory "C:\path\to\tradingview-mcp" run python -m tvmcp
# macOS / Linux
claude mcp add -s user tradingview -e TV_TOOLSETS=hybrid -- uv --directory /path/to/tradingview-mcp run python -m tvmcp
```

Gotcha: the server name (`tradingview`) must come **before** `-e` options —
`-e` is variadic and swallows the name otherwise ("Invalid environment variable
format").

Official server:

```bash
claude mcp add -s user --transport http mcp-tradingview https://mcp.tradingview.com/mcp
```

### Other clients (Cursor, VS Code, Codex, Gemini CLI, generic MCP JSON)

Add to the client's MCP config:

```json
{
  "mcpServers": {
    "tradingview": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/tradingview-mcp", "run", "python", "-m", "tvmcp"],
      "env": { "TV_TOOLSETS": "hybrid" }
    },
    "mcp-tradingview": {
      "type": "http",
      "url": "https://mcp.tradingview.com/mcp"
    }
  }
}
```

If the client cannot do OAuth for HTTP servers, skip `mcp-tradingview` and tell
the human; this repo works alone.

Verify registration (Claude Code):

```bash
claude mcp list
```

Expected: `tradingview: ... - ✔ Connected` and `mcp-tradingview: https://mcp.tradingview.com/mcp (HTTP) - ✔ Connected` or `needs authentication`.

---

## 5. Authenticate the official server (human action)

Tell the human, in one line: *run `/mcp` in Claude Code, pick `mcp-tradingview`,
sign in with your TradingView account (Essential+ plan) in the browser.* You
cannot do this for them — it is their browser session. Without a qualifying
plan the server stays "needs auth"; everything from this repo keeps working.

---

## 6. Choose toolsets (only if the default does not fit)

`TV_TOOLSETS` env on the `tradingview` server (comma list):

| Value | Loads | Use when |
|---|---|---|
| `hybrid` (plugin default) | data, scan, chart, backtest, pine, journal, strategy | official server is registered (it replaces the `public` screener) |
| `default` | public, data | no official server / no Essential+ plan — keeps the unauth screener, TA summary, symbol search |
| `default,scan,chart,backtest` | add opt-ins to default | no official server, want analysis tools |
| `all` | everything incl. session, desktop | human explicitly accepts ToS risk and supplies `TV_SESSIONID` / Desktop CDP |
| `hybrid,session,desktop` | hybrid + account tiers | same, with the official server alongside |

`tv_setup_doctor` is always registered. `TV_READ_ONLY=1` removes every
write-capable tool regardless of toolsets. Other env vars: README "Configuration".

Change env on a registered server by re-adding it (`claude mcp remove tradingview -s user`
then the `claude mcp add` line again), then restart the client session.

---

## 6b. Optional: the `desktop` tier (only when the human asks for it)

The `desktop` toolset drives the human's **logged-in TradingView Desktop app**
over Chrome DevTools Protocol: 25 `tv_desktop_*` tools — status/screenshot,
symbol/timeframe/viewport navigation, drawings, reading and configuring their
indicators, Strategy Tester report, bar replay, and the Pine Editor
(get/set/compile/save/open scripts). It may violate TradingView's ToS and the
server prints a ban-risk warning on first use. Enable it only on an explicit
request, and say that in your report.

Setup (Windows; the app is a Microsoft Store package):

1. The app must run with a debug port. Once `desktop` is enabled, the
   `tv_desktop_launch` tool does this for you (finds the exe, starts it with
   the flag, waits for CDP and a chart tab). It refuses — with the fix — when
   TradingView is already open **without** the flag: ask the human before
   closing it (the app is single-instance; a plain relaunch only focuses the
   flagless copy). Manual equivalent:
   ```powershell
   Stop-Process -Name TradingView -Force -ErrorAction SilentlyContinue
   powershell -File scripts\start-tv-desktop.ps1 -Port 9223
   ```
   Port **9223** on purpose: 9222 is often owned by another CDP tool and
   Chromium silently skips a busy port. `tv_desktop_launch` also refuses when
   the port answers but belongs to another app — pick another `TV_CDP_URL`.
2. Add `desktop` to `TV_TOOLSETS` and set `TV_CDP_URL=http://127.0.0.1:9223`
   on the `tradingview` server (re-add it, restart the client session).
3. The human must be logged in with a chart open.

Verify (in order; each call answers in a few seconds):

1. `tv_setup_doctor` → the desktop check is green (it confirms the listener
   really is TradingView).
2. `tv_desktop_status` → `connected: true` plus the current symbol/interval.
3. `tv_desktop_set_timeframe` with `timeframe="M15"` → `method: "api"`,
   `ready: true`. Put the previous interval back afterwards.
4. `tv_desktop_list_studies` → the human's indicators; if any is listed,
   `tv_desktop_read_study_graphics` with `compact=true` → `levels`/`zones`.
5. `tv_desktop_replay_status` → `available: true` (do not start replay in a
   smoke test — it changes what the human sees).

Rules for this tier:

- Everything you change is the human's live workspace and auto-saves. Put
  symbol/timeframe back, remove only drawings you made, never remove-all.
- `tv_desktop_pine_set_source` on a saved script is auto-saved to the human's
  account as a new version — the tool refuses unless `overwrite_saved=true`;
  pass it only after the human said yes.
- Do not loop bulk actions through the account (alerts, watchlists, dozens of
  symbol switches): community reports include TradingView account bans for
  account-driving automation. Read-mostly, on request.
- `TV_READ_ONLY=1` keeps the 10 read tools only (status, screenshot, list
  drawings/studies, read plots/graphics/strategy, replay status, Pine get
  source/list scripts).

---

## 7. Prove it works end to end (after the client restarts and tools appear)

Call these MCP tools from the client, in order, and report each result:

1. `tv_setup_doctor` → `healthy: true` or a list with `fix` commands.
2. `tv_data_get_bars` with `symbol="EURUSD", timeframe="H1", count=5` → 5 bars,
   `provider: "dukascopy"`. First call downloads and caches; give it up to a minute.
3. `tv_scan_fvg` with `symbol="EURUSD", timeframe="M15", count=300` → a list of
   zones (may be empty on a quiet range — that is still a pass).
4. `tv_chart_render` with `symbol="EURUSD", timeframe="H1", count=120` → a PNG
   path under `~/.tvmcp/charts`.
5. Official server (if authenticated): `mcp-tv-get-ohlcv` with
   `symbol="CME_MINI:ES1!", interval="1D", count=3` → 3 bars with a
   "delayed" notice. Delay is expected, not a failure.

Any of 1–4 failing: read the error — every error from this server states what to
configure — then rerun `scripts/healthcheck.py`.

---

## 8. Report to the human (template)

```
TradingView MCP install — <date>
- repo: <path>, commit <sha>, uv sync OK, Chromium OK
- healthcheck: OK (optional missing: OANDA key, TV cookie, Desktop CDP)
- registered: tradingview (hybrid, 17 tools; +35 desktop tools if the tier was requested) ✔ · mcp-tradingview ✔ / needs your /mcp sign-in
- smoke tests: doctor ✔ · bars ✔ · fvg ✔ · chart ✔ · official ohlcv ✔/skipped
- next for you: <one line: e.g. "run /mcp → mcp-tradingview → sign in">
```

---

## 9. Troubleshooting (symptom → fix)

| Symptom | Cause | Fix |
|---|---|---|
| `uv: command not found` after install | PATH not refreshed | open a new shell; on Windows also check `%USERPROFILE%\.local\bin` is on PATH |
| `--check` shows only 6 tools | `TV_TOOLSETS` not set → default | set `TV_TOOLSETS=hybrid` (or the list you need) in the server env |
| `tv_data_get_bars` → "npx not found" | Node missing | install Node 22+, restart client |
| `tv_chart_render` → "Chromium not installed" | Playwright browsers missing | `uv run playwright install chromium` |
| `claude mcp add` → "Invalid environment variable format" | name placed after `-e` | put the server name before `-e` (step 4) |
| `mcp-tradingview` stuck on "needs authentication" | human has not signed in / plan below Essential | human runs `/mcp` and signs in; otherwise use `TV_TOOLSETS=default,...` |
| Official `get_symbol_data` → "no data" for a broker CFD (e.g. `PEPPERSTONE:US500`) | official screener universe lacks broker CFDs | use official `get_ohlcv` or this repo's `tv_session_*`; see `skills/tradingview-hybrid` |
| `tv_session_*` → "TV_SESSIONID is not set" | opt-in tier without cookie | expected; only the human can supply it (ToS risk — do not push) |
| Desktop tools → "no CDP listener" | TradingView Desktop not started with the debug flag | `scripts/start-tv-desktop.ps1` (Windows), port 9223, `TV_CDP_URL=http://127.0.0.1:9223`; ask before closing the human's app |
| `tv_desktop_pine_set_source` → "holds one of the user's SAVED scripts" | a saved script is open; editing it auto-saves a new version to the account | ask the human; then `overwrite_saved=true`, or have them open a new blank script (Pine Editor → Open → New) |
| `tv_desktop_read_strategy` → "report is not computed" / "Hidden strategies never compute" | Strategy Tester panel never shown, or the strategy is eye-toggled off | the tool opens the panel itself; a hidden strategy must be made visible by the human |
| `tv_desktop_scroll_to_date` returns `clamped: true` | the feed's history ended before the date (plan limits) | use a higher timeframe or a later date; `earliest_loaded` says how far it got |
| Tools missing in the client after registration | session started before registration | restart the client session |

More: `skills/tvmcp-guide/references/troubleshooting.md` (every tool's failure
modes), `skills/tradingview-hybrid/SKILL.md` (which server for which job).
