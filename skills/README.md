# skills/

Companion Agent Skills for the tradingview-mcp plugin (M5). One folder per skill
with a spec-clean `SKILL.md` (agentskills.io frontmatter), plus `references/` as
needed. Contract-tested: frontmatter validity, `<500` lines, and every `tv_*` tool
reference must exist in the registered tool surface (`tests/test_skills.py`).

| Skill | Fronts | Notes |
|---|---|---|
| `smc-scanning` | `tv_scan_*` | SMC/ICT detection workflow, repaint/session semantics |
| `chart-markup` | `tv_chart_render` | markup_json schema + render workflow |
| `strategy-backtesting` | `tv_backtest_run`, `tv_strategy_*` | honest stat reading, walk-forward |
| `pine-authoring` | `tv_pine_compile` | compiler-loop authoring |
| `risk-sizing` | none (judgment) | pip/JPY/R math, risk budget |
| `market-screening` | `tv_screener_run`, `tv_ta_summary`, `tv_symbol_search` | scanner fields/suffixes |
| `strategy-review` | none (judgment) | owner's 5-block framework + 27-item checklist (vault note 02) |
| `journal-sync` | `tv_journal_scan`, `tv_journal_parse` + Notion MCP | FX Replay→Notion mapping (vault note 06) |
| `tvmcp-guide` | all (orientation) | full toolset map, setup ladder, output conventions, symptom→fix troubleshooting table |
| `tradingview-tiers` | `tv_session_*`, `tv_desktop_*` | opt-in account tiers: setup, ToS framing, cookie/CDP failure recovery |