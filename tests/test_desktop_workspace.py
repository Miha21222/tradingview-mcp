"""M8 part B: `desktop/workspace.py` - window restore, dialog dismissal, editor
docking, staged editor opening until Monaco is live, tri-state study clearing,
optional navigation, and the `tv_desktop_workspace_prepare` tool. FakePage +
FakeClock only - no CDP, no waiting.
"""

import asyncio
import json
from contextlib import contextmanager

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import desktop
from tvmcp.config import Settings
from tvmcp.desktop import clock, driver, workspace

from desktop_fake import FakeClock, FakePage


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
def _fake_clock(monkeypatch):
    from tvmcp.desktop import warnings as w

    fc = FakeClock()
    monkeypatch.setattr(clock, "now", fc.now)
    monkeypatch.setattr(clock, "sleep", fc.sleep)
    w._printed = False
    driver._bound = None
    yield fc
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


def _studies():
    return [{"id": "lux1", "title": "LuxAlgo Imbalance", "strategy": False},
            {"id": "s1", "title": "SMA Cross strategy", "strategy": True},
            {"id": "dt1", "title": "Dark Trader Sessions", "strategy": False}]


def test_prepare_happy_path_records_every_step():
    page = FakePage(studies=_studies())
    page.window_state = "minimized"
    page.dialogs = [{"title": "Welcome", "buttons": ["Cancel", "OK"]}]
    page.floating_editor = True
    res = workspace.prepare(page, "strategies", "OANDA:EURUSD", "15")
    assert res["window_restored"] is True and page.window_state == "normal"
    assert ("Browser.setWindowBounds", {"windowId": 1, "bounds": {"windowState": "normal"}}) in page.window_calls
    assert res["blocking_dialogs"]["dismissed"] == [{"title": "Welcome", "via": "cancel"}]
    assert res["editor_docked"] is True and page.floating_editor is False
    assert res["pine_editor_open"] is True and res["pine_editor_width"] == 800
    # tri-state: only the strategy went, the user's paid studies stayed
    assert [c["id"] for c in res["cleared_studies"]] == ["s1"]
    assert [s["id"] for s in res["studies"]] == ["lux1", "dt1"]
    assert page.nav[-1] == {"symbol": "OANDA:EURUSD", "resolution": "15", "timeout_ms": 8000}
    assert res["note"] is None
    names = [s["step"] for s in res["steps"]]
    assert names == ["window_restore", "dismiss_dialogs", "dock_editor", "editor_open",
                     "clear_studies", "navigate", "list_studies"]
    assert all(isinstance(s["ms"], int) for s in res["steps"])
    assert res["steps"][3]["via"] == "already_open"


def test_window_restore_unsupported_falls_back_to_bring_to_front():
    page = FakePage()
    page.window_supported = False
    assert workspace.restore_window(page) == "unsupported"
    assert page.window_calls[-1][0] == "Page.bringToFront"

    class NoCall:
        pass

    assert workspace.restore_window(NoCall()) == "unsupported"


def test_window_not_minimized_only_brought_to_front():
    page = FakePage()
    assert workspace.restore_window(page) is False
    assert [m for m, _ in page.window_calls] == ["Browser.getWindowForTarget", "Page.bringToFront"]


def test_editor_opens_after_staged_recovery(_fake_clock):
    page = FakePage()
    page.editor_live = False
    page.editor_live_after_stage = 2  # footer + activateScriptEditorTab do nothing
    res = workspace.open_editor(page)
    assert res["live"] is True and res["via"] == "showWidget"
    assert res["stages_tried"] == ["footer_button", "activateScriptEditorTab", "showWidget"]
    assert page.stages_tried == [0, 1, 2]
    # two dead stages waited the per-stage budget on the fake clock, no real sleep
    assert sum(_fake_clock.slept) >= 2 * workspace._STAGE_WAIT_S - 0.5


def test_editor_never_mounts_reports_note_without_raising():
    page = FakePage()
    page.editor_live = False
    res = workspace.prepare(page)
    assert res["pine_editor_open"] is False and res["pine_editor_width"] is None
    assert "click once inside the Pine editor" in res["note"]
    assert page.stages_tried == [0, 1, 2, 3]
    assert res["steps"][3]["live"] is False


def test_collapsed_editor_gets_a_width_note():
    page = FakePage()
    page.editor_width = 150
    res = workspace.prepare(page)
    assert res["pine_editor_open"] is True and res["pine_editor_width"] == 150
    assert "collapsed" in res["note"] and "splitter" in res["note"]


def test_clear_modes_tri_state_and_bool_alias():
    assert workspace.parse_clear_mode(True) == "all"
    assert workspace.parse_clear_mode(False) == "none"
    assert workspace.parse_clear_mode(None) == "none"
    assert workspace.parse_clear_mode("Strategies") == "strategies"
    with pytest.raises(ToolError, match="clear_studies must be one of"):
        workspace.parse_clear_mode("bogus")


def test_clear_all_removes_every_study_and_none_touches_nothing():
    page = FakePage(studies=_studies())
    res = workspace.prepare(page, "none")
    assert res["cleared_studies"] == [] and len(page.studies) == 3
    res = workspace.prepare(page, True)
    assert [c["id"] for c in res["cleared_studies"]] == ["lux1", "s1", "dt1"]
    assert page.studies == []


def test_clear_without_api_raises():
    page = FakePage(studies=_studies())
    page.api = False
    with pytest.raises(ToolError, match="TradingViewApi"):
        workspace.clear_studies(page, "all")


def test_navigation_without_api_is_noted_not_fatal():
    page = FakePage()
    page.api = False
    res = workspace.prepare(page, "none", "OANDA:EURUSD", None)
    assert "navigation skipped" in res["note"]
    nav = next(s for s in res["steps"] if s["step"] == "navigate")
    assert nav["no_api"] is True


def test_tool_resolves_aliases_and_is_write_gated(tmp_path):
    mcp, page = _build(tmp_path, FakePage(studies=_studies()))
    data = _data(mcp, "tv_desktop_workspace_prepare",
                 {"clear_studies": True, "symbol": "eurusd", "timeframe": "M15"})
    assert data["provider"] == "desktop"
    assert page.nav[-1]["symbol"] == "OANDA:EURUSD" and page.nav[-1]["resolution"] == "15"
    assert len(data["cleared_studies"]) == 3
    mcp_ro, _ = _build(tmp_path, read_only=True)
    names = {t.name for t in asyncio.run(mcp_ro.list_tools())}
    assert "tv_desktop_workspace_prepare" not in names
