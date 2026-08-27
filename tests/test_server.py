import asyncio
from pathlib import Path

from tvmcp.config import Settings
from tvmcp.server import build_server


def _settings(toolsets, tmp_path: Path, **kw) -> Settings:
    return Settings(
        toolsets=frozenset(toolsets),
        extra_tools=frozenset(),
        read_only=False,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=kw.get("oanda_api_key"),
        oanda_env="practice",
        session_id=None,
    )


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_default_toolsets_register_expected_tools(tmp_path):
    mcp = build_server(_settings({"public", "data"}, tmp_path))
    names = _tool_names(mcp)
    assert {
        "tv_screener_run",
        "tv_ta_summary",
        "tv_symbol_search",
        "tv_data_get_bars",
        "tv_data_providers_status",
    } <= names


def test_toolset_gating_excludes_disabled(tmp_path):
    mcp = build_server(_settings({"public"}, tmp_path))
    names = _tool_names(mcp)
    assert "tv_screener_run" in names
    assert "tv_data_get_bars" not in names


def test_default_surface_small(tmp_path):
    mcp = build_server(_settings({"public", "data"}, tmp_path))
    assert len(_tool_names(mcp)) <= 25


def test_scan_toolset_registers_scan_tools(tmp_path):
    mcp = build_server(_settings({"public", "data", "scan"}, tmp_path))
    names = _tool_names(mcp)
    assert {
        "tv_scan_fvg",
        "tv_scan_ob",
        "tv_scan_structure",
        "tv_scan_liquidity",
        "tv_scan_sessions",
        "tv_scan_prev_hl",
    } <= names


def test_scan_gated_off_by_default(tmp_path):
    mcp = build_server(_settings({"public", "data"}, tmp_path))
    names = _tool_names(mcp)
    assert "tv_scan_fvg" not in names


def test_chart_toolset_registers_render(tmp_path):
    mcp = build_server(_settings({"public", "data", "chart"}, tmp_path))
    names = _tool_names(mcp)
    assert "tv_chart_render" in names


def test_chart_gated_off_by_default(tmp_path):
    mcp = build_server(_settings({"public", "data"}, tmp_path))
    names = _tool_names(mcp)
    assert "tv_chart_render" not in names


def test_backtest_toolset_registers_run(tmp_path):
    mcp = build_server(_settings({"public", "data", "backtest"}, tmp_path))
    names = _tool_names(mcp)
    assert {"tv_backtest_run", "tv_backtest_render_trades"} <= names


def test_backtest_gated_off_by_default(tmp_path):
    mcp = build_server(_settings({"public", "data"}, tmp_path))
    names = _tool_names(mcp)
    assert "tv_backtest_run" not in names


def test_journal_toolset_registers_scan(tmp_path):
    mcp = build_server(_settings({"public", "data", "journal"}, tmp_path))
    names = _tool_names(mcp)
    assert {"tv_journal_scan", "tv_journal_parse"} <= names


def test_strategy_toolset_registers_list_run(tmp_path):
    mcp = build_server(_settings({"public", "data", "strategy"}, tmp_path))
    names = _tool_names(mcp)
    assert {"tv_strategy_list", "tv_strategy_run"} <= names


def test_strategy_gated_off_by_default(tmp_path):
    mcp = build_server(_settings({"public", "data"}, tmp_path))
    names = _tool_names(mcp)
    assert "tv_strategy_run" not in names


def test_config_parsing(monkeypatch):
    from tvmcp.config import load_settings

    s = load_settings({"TV_TOOLSETS": "default,scan", "TV_READ_ONLY": "1"})
    assert s.toolsets == frozenset({"public", "data", "scan"})
    assert s.read_only is True

    s2 = load_settings({})
    assert s2.toolsets == frozenset({"public", "data"})






