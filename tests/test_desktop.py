"""CDP-free tests for the opt-in `desktop` toolset (fake page, no app, no network).

Verifies: gating (off by default), first-use ToS warning, read-only exclusion of
navigation tools, status/screenshot plumbing, symbol mismatch detection, and the
timeframe quick-key map.
"""

import asyncio
import json
from contextlib import contextmanager

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import desktop
from tvmcp.config import Settings

from desktop_fake import FakePage


def _settings(tmp_path, read_only=False) -> Settings:
    return Settings(
        toolsets=frozenset({"desktop"}),
        extra_tools=frozenset(),
        read_only=read_only,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=None,
        oanda_env="practice",
        session_id=None,
    )


@pytest.fixture(autouse=True)
def _reset_warning():
    from tvmcp.desktop import driver, warnings as w

    w._printed = False
    driver._bound = None
    yield
    w._printed = False
    driver._bound = None


def _build(tmp_path, page=None, read_only=False):
    page = page or FakePage()

    @contextmanager
    def factory(cdp_url):
        yield page

    mcp = FastMCP(name="test")
    desktop.register(mcp, _settings(tmp_path, read_only), page_factory=factory)
    return mcp, page


def _data(mcp, name, args=None):
    r = asyncio.run(mcp.call_tool(name, args or {}))
    return json.loads(r.content[0].text)


def test_gating_off_by_default(tmp_path):
    from tvmcp.server import build_server

    s = _settings(tmp_path)
    s = Settings(**{**s.__dict__, "toolsets": frozenset({"public", "data"})})
    names = {t.name for t in asyncio.run(build_server(s).list_tools())}
    assert not any(n.startswith("tv_desktop_") for n in names)


_READ_TOOLS = {
    "tv_desktop_status",
    "tv_desktop_screenshot",
    "tv_desktop_list_drawings",
    "tv_desktop_list_studies",
    "tv_desktop_read_study_plots",
    "tv_desktop_read_study_graphics",
    "tv_desktop_read_strategy",
    "tv_desktop_replay_status",
    "tv_desktop_pine_get_source",
    "tv_desktop_pine_list_scripts",
    "tv_desktop_ui_find_element",
    "tv_desktop_check_levels",
    "tv_desktop_pine_find_exact",
    "tv_desktop_pine_get_errors",
}
_WRITE_TOOLS = {
    "tv_desktop_launch",
    "tv_desktop_ui_click",
    "tv_desktop_set_symbol",
    "tv_desktop_set_timeframe",
    "tv_desktop_draw",
    "tv_desktop_remove_drawing",
    "tv_desktop_scroll_to_date",
    "tv_desktop_set_visible_range",
    "tv_desktop_set_study_inputs",
    "tv_desktop_replay_start",
    "tv_desktop_replay_step",
    "tv_desktop_replay_trade",
    "tv_desktop_replay_stop",
    "tv_desktop_pine_set_source",
    "tv_desktop_pine_compile",
    "tv_desktop_pine_save",
    "tv_desktop_pine_open_script",
    "tv_desktop_workspace_prepare",
    "tv_desktop_pine_replace_exact",
    "tv_desktop_pine_save_as",
    "tv_desktop_pine_build_and_backtest",
}


def test_registers_all_desktop_tools(tmp_path):
    mcp, _ = _build(tmp_path)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == _READ_TOOLS | _WRITE_TOOLS


def test_read_only_excludes_navigation_and_writes(tmp_path):
    mcp, _ = _build(tmp_path, read_only=True)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == _READ_TOOLS


def test_status_reads_ui(tmp_path, capsys):
    mcp, _ = _build(tmp_path)
    data = _data(mcp, "tv_desktop_status")
    assert data["connected"] is True
    assert data["symbol"] == "OANDA:EURUSD"
    assert data["provider"] == "desktop"
    err = capsys.readouterr().err
    assert "WARNING" in err and "opt-in" in err


def test_warning_printed_once(tmp_path, capsys):
    mcp, _ = _build(tmp_path)
    _data(mcp, "tv_desktop_status")
    _data(mcp, "tv_desktop_status")
    assert capsys.readouterr().err.count("WARNING") == 1


def test_screenshot_writes_file(tmp_path):
    mcp, page = _build(tmp_path)
    data = _data(mcp, "tv_desktop_screenshot")
    assert data["path"].endswith(".png")
    assert page.shots and str(page.shots[0]) == data["path"]


def test_set_symbol_uses_chart_api(tmp_path):
    page = FakePage(symbol="EURUSD")
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_set_symbol", {"symbol": "eurusd"})
    assert data["requested"] == "OANDA:EURUSD"
    assert data["method"] == "api" and data["ready"] is True
    assert page.nav[-1] == {"symbol": "OANDA:EURUSD", "resolution": None, "timeout_ms": 8000}
    assert not page.typed  # no keyboard automation when the API is there


def test_set_symbol_falls_back_to_keyboard(tmp_path):
    page = FakePage(symbol="EURUSD")
    page.api = False
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_set_symbol", {"symbol": "eurusd"})
    assert data["method"] == "keyboard"
    assert "OANDA:EURUSD" in page.typed
    assert "Enter" in page.pressed


def test_set_symbol_mismatch_raises(tmp_path):
    page = FakePage(symbol="AAPL")  # app resolved something else
    page.api_symbol_override = "NASDAQ:AAPL"
    mcp, _ = _build(tmp_path, page)
    with pytest.raises(ToolError, match="different listing"):
        asyncio.run(mcp.call_tool("tv_desktop_set_symbol", {"symbol": "EURUSD"}))


def test_set_timeframe_uses_chart_api(tmp_path):
    mcp, page = _build(tmp_path)
    data = _data(mcp, "tv_desktop_set_timeframe", {"timeframe": "H4"})
    assert data["requested"] == "H4"
    assert page.nav[-1]["resolution"] == "240"
    assert data["api_resolution"] == "240"


def test_set_timeframe_keyboard_fallback(tmp_path):
    mcp, page = _build(tmp_path)
    page.api = False
    data = _data(mcp, "tv_desktop_set_timeframe", {"timeframe": "H4"})
    assert data["method"] == "keyboard"
    assert "240" in page.typed


# --- M7: viewport ------------------------------------------------------------

def test_scroll_to_date_windows_by_resolution(tmp_path):
    mcp, page = _build(tmp_path)
    page.interval = "15"
    data = _data(mcp, "tv_desktop_scroll_to_date", {"time": 1_700_000_000, "bars_each_side": 10})
    assert data["resolution_minutes"] == 15
    assert page.viewports[-1]["from"] == 1_700_000_000 - 10 * 15 * 60
    assert page.viewports[-1]["to"] == 1_700_000_000 + 10 * 15 * 60
    assert data["pages_loaded"] == 2 and data["clamped"] is False


def test_set_visible_range_rejects_inverted(tmp_path):
    mcp, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="after from_time"):
        asyncio.run(mcp.call_tool("tv_desktop_set_visible_range",
                                  {"from_time": 10, "to_time": 5}))


def test_resolution_to_minutes():
    from tvmcp.desktop.driver import resolution_to_minutes as r

    assert r("15") == 15 and r("240") == 240
    assert r("D") == 1440 and r("1D") == 1440 and r("W") == 10080 and r("1M") == 43200
    assert r("30S") == 1 and r("") == 60


# --- M7: study inputs ----------------------------------------------------------

_MACD = {"id": "macd1", "title": "MACD", "plots": [], "rows": [],
         "inputs": {"fast_length": 12, "slow_length": 26, "stuck": 9}}


def test_set_study_inputs_reads_back(tmp_path):
    page = FakePage(studies=[_MACD])
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_set_study_inputs",
                 {"study": "macd", "inputs": {"fast_length": 3, "slow_length": 10}})
    assert data["applied"] is True
    assert data["before"] == {"fast_length": 12, "slow_length": 26}
    assert data["after"] == {"fast_length": 3, "slow_length": 10}


def test_set_study_inputs_reports_mismatch(tmp_path):
    page = FakePage(studies=[_MACD])
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_set_study_inputs",
                 {"study": "macd", "inputs": {"stuck": 16}})
    assert data["applied"] is False and data["mismatched"] == ["stuck"]


def test_set_study_inputs_unknown_lists_available(tmp_path):
    page = FakePage(studies=[_MACD])
    mcp, _ = _build(tmp_path, page)
    with pytest.raises(ToolError, match="Unknown input.*fast_length"):
        asyncio.run(mcp.call_tool("tv_desktop_set_study_inputs",
                                  {"study": "macd", "inputs": {"nope": 1}}))


def test_read_study_graphics_compact_flag(tmp_path):
    page = FakePage(studies=[_LUX])
    mcp, _ = _build(tmp_path, page)
    _data(mcp, "tv_desktop_read_study_graphics", {"study": "lux1", "compact": True})
    assert page.study_payloads[-1]["compact"] is True


# --- M7: strategy tester ---------------------------------------------------------

def test_read_strategy_metrics_and_orders(tmp_path):
    page = FakePage()
    page.strategy = {
        "strategies": [{"id": "s1", "title": "SMA Cross", "visible": True}],
        "selected": {"id": "s1", "title": "SMA Cross"}, "currency": "USD",
        "metrics": {"net_profit": 120.5, "profit_factor": 1.4, "buy_hold_return": 300.0},
        "sides": {}, "total_orders": 40,
        "orders": [{"id": 1, "side": "buy", "price": 1.1, "time": 1_700_000_000}],
    }
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_read_strategy", {"orders": 5})
    assert data["metrics"]["buy_hold_return"] == 300.0
    assert data["orders"][0]["side"] == "buy"
    assert page.strategy_payloads[-1] == {"orders": 5, "open_panel": True, "wait_ms": 6000}


def test_read_strategy_no_strategy_raises(tmp_path):
    mcp, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="No strategy"):
        asyncio.run(mcp.call_tool("tv_desktop_read_strategy"))


def test_read_strategy_hidden_hint(tmp_path):
    page = FakePage()
    page.strategy = {"strategies": [{"id": "s1", "title": "SMA Cross", "visible": False}],
                     "report": None}
    mcp, _ = _build(tmp_path, page)
    with pytest.raises(ToolError, match="Hidden strategies never compute.*SMA Cross"):
        asyncio.run(mcp.call_tool("tv_desktop_read_strategy"))


# --- M7: replay ------------------------------------------------------------------

def test_replay_lifecycle(tmp_path):
    mcp, page = _build(tmp_path)
    st = _data(mcp, "tv_desktop_replay_status")
    assert st["started"] is False
    data = _data(mcp, "tv_desktop_replay_start", {"time": 1_700_000_000})
    assert data["action"] == "started"
    assert page.replay_calls[-1]["time_ms"] == 1_700_000_000_000  # seconds -> ms
    data = _data(mcp, "tv_desktop_replay_step", {"count": 3})
    assert data["stepped"] == 3 and data["current_date"] == 1_700_000_000_000 + 180000
    data = _data(mcp, "tv_desktop_replay_trade", {"side": "buy"})
    assert data["position"] == {"side": "buy"}
    data = _data(mcp, "tv_desktop_replay_stop")
    assert data["action"] == "stopped"


def test_replay_step_before_start_raises(tmp_path):
    mcp, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="not started"):
        asyncio.run(mcp.call_tool("tv_desktop_replay_step"))


def test_replay_start_no_data_raises(tmp_path):
    mcp, page = _build(tmp_path)
    with pytest.raises(ToolError, match="did not start"):
        asyncio.run(mcp.call_tool("tv_desktop_replay_start", {"time": 1}))
    assert page.replay_calls[-1]["time_ms"] == 1000


def test_replay_trade_side_validated(tmp_path):
    mcp, _ = _build(tmp_path)
    with pytest.raises(Exception):
        asyncio.run(mcp.call_tool("tv_desktop_replay_trade", {"side": "long"}))


# --- M7: pine editor -------------------------------------------------------------

def test_pine_set_compile_ok(tmp_path):
    mcp, page = _build(tmp_path)
    data = _data(mcp, "tv_desktop_pine_set_source", {"source": "//@version=6\nindicator('x')"})
    assert data["applied"] is True and page.editor.startswith("//@version=6")
    data = _data(mcp, "tv_desktop_pine_compile")
    assert data["ok"] is True and data["clicked"] == "Add to chart"
    assert data["study_added"] is True


def test_pine_compile_reports_errors_and_ctrl_enter_fallback(tmp_path):
    mcp, page = _build(tmp_path)
    page.compile_button = None
    page.markers = [{"line": 3, "column": 1, "severity": "error", "message": "Mismatched input"},
                    {"line": 1, "column": 1, "severity": "warning", "message": "unused"}]
    data = _data(mcp, "tv_desktop_pine_compile")
    assert data["ok"] is False and data["errors"][0]["line"] == 3
    assert len(data["warnings"]) == 1
    assert data["clicked"] == "Ctrl+Enter"
    assert page.pressed[-1] == "Enter"


def test_pine_get_source_and_editor_missing(tmp_path):
    mcp, page = _build(tmp_path)
    page.editor = "x"
    assert _data(mcp, "tv_desktop_pine_get_source")["source"] == "x"
    page.editor = None
    with pytest.raises(ToolError, match="Pine Editor is not reachable"):
        asyncio.run(mcp.call_tool("tv_desktop_pine_get_source"))


def test_pine_list_and_open_script(tmp_path):
    mcp, page = _build(tmp_path)
    data = _data(mcp, "tv_desktop_pine_list_scripts")
    assert data["scripts"][0]["name"] == "My SMC"
    data = _data(mcp, "tv_desktop_pine_open_script", {"name": "my smc"})
    assert data["id"] == "abc" and page.editor.startswith("//@version")
    with pytest.raises(ToolError, match="No saved script matches"):
        asyncio.run(mcp.call_tool("tv_desktop_pine_open_script", {"name": "zzz"}))


def test_pine_save_ctrl_s(tmp_path):
    mcp, page = _build(tmp_path)
    data = _data(mcp, "tv_desktop_pine_save")
    assert data["via"] == "Ctrl+S" and page.pressed[-1] == "s"


def test_list_drawings_returns_viewport_and_shapes(tmp_path):
    page = FakePage(shapes={"abc123": {
        "name": "rectangle",
        "points": [{"time": 1, "price": 1.1}, {"time": 2, "price": 1.2}],
        "text": "Key Level",
    }})
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_list_drawings")
    assert data["provider"] == "desktop"
    assert data["visible_time_range"]["from"] == 1787151600
    assert data["visible_price_range"]["to"] == 1.17
    assert data["shapes"][0]["id"] == "abc123"
    assert data["shapes"][0]["text"] == "Key Level"


def test_draw_rectangle_creates_shape(tmp_path):
    mcp, page = _build(tmp_path)
    data = _data(mcp, "tv_desktop_draw", {
        "kind": "rectangle",
        "points": [{"time": 100, "price": 1.1655}, {"time": 200, "price": 1.1662}],
        "text": "FVG",
        "color": "#2962ff",
    })
    assert data["kind"] == "rectangle"
    assert data["created"][0]["id"] == "fake1"
    assert page.shapes["fake1"]["text"] == "FVG"
    # rectangle overrides carry the fill derived from the hex color
    assert '"backgroundColor": "rgba(41,98,255,0.15)"' in page.exprs[-1]


def test_draw_horizontal_line_time_optional(tmp_path):
    mcp, _ = _build(tmp_path)
    data = _data(mcp, "tv_desktop_draw", {
        "kind": "horizontal_line",
        "points": [{"price": 1.165}],
    })
    assert data["created"][0]["name"] == "horizontal_line"


def test_draw_rejects_unknown_kind(tmp_path):
    mcp, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="Unknown kind"):
        asyncio.run(mcp.call_tool("tv_desktop_draw", {
            "kind": "circle", "points": [{"time": 1, "price": 1.0}]}))


def test_draw_rejects_wrong_point_count(tmp_path):
    mcp, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="exactly 2 point"):
        asyncio.run(mcp.call_tool("tv_desktop_draw", {
            "kind": "rectangle", "points": [{"time": 1, "price": 1.0}]}))


def test_draw_rejects_missing_price(tmp_path):
    mcp, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="missing 'price'"):
        asyncio.run(mcp.call_tool("tv_desktop_draw", {
            "kind": "trend_line", "points": [{"time": 1, "price": 1.0}, {"time": 2}]}))


def test_remove_drawing_by_id(tmp_path):
    page = FakePage(shapes={"gone1": {"name": "trend_line", "points": [], "text": None}})
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_remove_drawing", {"drawing_id": "gone1"})
    assert data["found"] is True and data["id"] == "gone1"
    assert "gone1" not in page.shapes


def test_remove_unknown_id_raises(tmp_path):
    mcp, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="No drawing with id"):
        asyncio.run(mcp.call_tool("tv_desktop_remove_drawing", {"drawing_id": "nope"}))


_LUX = {
    "id": "lux1",
    "title": "Imbalance Detector [LuxAlgo]",
    "plots": [{"id": "plot_0", "type": "alertcondition", "title": "Bullish FVG"}],
    "rows": [[1787810400, 0], [1787814000, 1], [1787817600, 0]],
    "boxes": [
        {"id": 1, "time1": 1787745600, "time2": 1787752800,
         "price1": 1.16556, "price2": 1.16534, "text": None, "extend": "n",
         "bg_color": {"hex": "#0011ff", "alpha": 0.2}, "border_color": None},
    ],
}
_WF = {"id": "wf1", "title": "Williams Fractals", "plots": [], "rows": []}


def test_list_studies_returns_chart_and_studies(tmp_path):
    page = FakePage(studies=[_LUX, _WF])
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_list_studies")
    assert data["provider"] == "desktop"
    assert data["symbol"] == "OANDA:EURUSD"
    assert [s["id"] for s in data["studies"]] == ["lux1", "wf1"]


def test_read_study_plots_by_title_substring(tmp_path):
    page = FakePage(studies=[_LUX, _WF])
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_read_study_plots",
                 {"study": "imbalance", "count": 2})
    assert data["id"] == "lux1"
    assert data["rows"] == [[1787814000, 1], [1787817600, 0]]
    assert page.study_payloads[-1] == {
        "query": "imbalance", "count": 2, "nonempty_only": False}


def test_read_study_graphics_passes_limit_and_kinds(tmp_path):
    page = FakePage(studies=[_LUX])
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_read_study_graphics",
                 {"study": "lux1", "limit": 10, "kinds": ["boxes"]})
    assert data["counts"]["boxes"] == 1
    assert data["boxes"][0]["price1"] == 1.16556
    assert page.study_payloads[-1] == {
        "query": "lux1", "limit": 10, "kinds": ["boxes"], "compact": False}


def test_study_query_miss_raises_with_candidates(tmp_path):
    page = FakePage(studies=[_LUX, _WF])
    mcp, _ = _build(tmp_path, page)
    with pytest.raises(ToolError, match="matches no study.*Imbalance"):
        asyncio.run(mcp.call_tool("tv_desktop_read_study_plots",
                                  {"study": "nope"}))


def test_study_query_ambiguous_raises(tmp_path):
    page = FakePage(studies=[_LUX, _WF])
    mcp, _ = _build(tmp_path, page)
    with pytest.raises(ToolError, match="ambiguous"):
        asyncio.run(mcp.call_tool("tv_desktop_read_study_graphics",
                                  {"study": "i"}))


def test_read_study_plots_count_bounds(tmp_path):
    mcp, _ = _build(tmp_path, FakePage(studies=[_LUX]))
    with pytest.raises(Exception):
        asyncio.run(mcp.call_tool("tv_desktop_read_study_plots",
                                  {"study": "lux1", "count": 0}))


def test_timeframe_map_covers_all_canonicals():
    from tvmcp.desktop.driver import _TF_KEYS
    from tvmcp.symbols import _TIMEFRAMES

    assert set(_TF_KEYS) == set(_TIMEFRAMES)

