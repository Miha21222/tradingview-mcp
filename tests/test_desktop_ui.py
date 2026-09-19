"""M8 desktop-3: UI find/click, dialog dismissal, native input fill, bars read,
level checks on the live chart, and the `target` field of tv_desktop_status.
All against the shared FakePage - no CDP, no network.
"""

import asyncio
import json
from contextlib import contextmanager

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import desktop
from tvmcp.config import Settings
from tvmcp.desktop import driver, ui

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
def _reset():
    from tvmcp.desktop import warnings as w

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


_BTN_OK = {"tag": "button", "data_name": "submit-button", "aria_label": None,
           "text": "OK", "role": None, "rect": {"x": 1, "y": 1, "w": 10, "h": 10},
           "in_dialog": True}
_BTN_CANCEL = {"tag": "button", "data_name": None, "aria_label": "Cancel",
               "text": "Cancel", "role": None, "rect": {"x": 1, "y": 1, "w": 10, "h": 10},
               "in_dialog": True}
_DIV_X = {"tag": "div", "data_name": "close", "aria_label": "Close dialog",
          "text": None, "role": "button", "rect": {"x": 0, "y": 0, "w": 8, "h": 8},
          "in_dialog": True}


# --- find --------------------------------------------------------------------------

def test_ui_find_by_data_name_and_label(tmp_path):
    page = FakePage()
    page.elements = [_BTN_OK, _BTN_CANCEL, _DIV_X]
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_ui_find_element", {"query": "submit-button"})
    assert data["provider"] == "desktop"
    assert [m["text"] for m in data["matches"]] == ["OK"]
    data = _data(mcp, "tv_desktop_ui_find_element", {"query": "close"})
    assert [m["data_name"] for m in data["matches"]] == ["close"]
    assert data["matches"][0]["in_dialog"] is True


def test_ui_find_payload_is_json_not_interpolated(tmp_path):
    page = FakePage()
    mcp, _ = _build(tmp_path, page)
    q = "x'); alert(1) ('"
    _data(mcp, "tv_desktop_ui_find_element", {"query": q, "limit": 5})
    assert page.ui_payloads[-1] == {"query": q, "limit": 5}
    assert json.dumps(q) in page.exprs[-1]


def test_ui_find_rejects_long_query(tmp_path):
    with pytest.raises(ToolError):
        ui.find_elements(FakePage(), "x" * 201)
    with pytest.raises(ToolError):
        ui.find_elements(FakePage(), "   ")


# --- click -------------------------------------------------------------------------

def test_ui_click_exactly_one(tmp_path):
    page = FakePage()
    page.elements = [_BTN_OK, _BTN_CANCEL]
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_ui_click", {"target": "cancel"})
    assert data["clicked"] is True and data["matched"]["text"] == "Cancel"
    assert page.clicks == [_BTN_CANCEL]


def test_ui_click_miss_returns_candidates_not_error(tmp_path):
    page = FakePage()
    page.elements = [_BTN_OK, _DIV_X]
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_ui_click", {"target": "nope"})
    assert data["clicked"] is False and data["miss"] is True
    assert {c["data_name"] for c in data["candidates"]} == {"submit-button", "close"}
    assert page.clicks == []


def test_ui_click_ambiguous_lists_matches(tmp_path):
    page = FakePage()
    page.elements = [_BTN_CANCEL, _DIV_X]  # both carry "close"/"cancel"-ish labels
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_ui_click", {"target": "c"})
    assert data["clicked"] is False and data["ambiguous"] is True
    assert len(data["candidates"]) == 2
    assert page.clicks == []


def test_ui_click_is_write_gated(tmp_path):
    mcp, _ = _build(tmp_path, read_only=True)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "tv_desktop_ui_find_element" in names
    assert "tv_desktop_ui_click" not in names
    assert "tv_desktop_launch" not in names


# --- dialogs -----------------------------------------------------------------------

def test_dismiss_dialogs_clicks_cancel_else_close(tmp_path):
    page = FakePage()
    page.dialogs = [{"title": "Replay date", "buttons": ["Отмена", "OK"]},
                    {"title": "Sticky", "buttons": ["Apply"]},
                    {"title": "Info", "buttons": ["close"]}]
    res = ui.dismiss_dialogs(page)
    assert [d["title"] for d in res["dismissed"]] == ["Replay date", "Info"]
    assert res["dismissed"][0]["via"] == "cancel" and res["dismissed"][1]["via"] == "close"
    assert res["remaining"] == [{"title": "Sticky", "buttons": ["Apply"]}]
    assert "/*tvmcp:dialogs*/" in page.exprs[-1]
    # Escape is ignored by TV: the block must never rely on a keydown
    assert "Escape" not in page.exprs[-1] and not page.pressed


# --- native fill -------------------------------------------------------------------

def test_fill_input_native_js_uses_prototype_setter():
    js = ui.fill_input_native_js("document.querySelector('input')", "a\"b")
    assert "/*tvmcp:fill*/" in js
    assert "HTMLInputElement.prototype" in js and "desc.set.call(el, \"a\\\"b\")" in js
    assert "new Event('input'" in js and "new Event('change'" in js


# --- status target ------------------------------------------------------------------

def test_status_reports_bound_target(tmp_path):
    mcp, _ = _build(tmp_path)
    assert _data(mcp, "tv_desktop_status")["target"] is None
    driver._bound = {"target_id": "T1", "score": 13, "at": 0.0, "chart_tabs": 2}
    assert _data(mcp, "tv_desktop_status")["target"] == {"id": "T1", "score": 13, "chart_tabs": 2}


# --- bars + check_levels on the live chart -------------------------------------------

def _bars(start=1_700_000_000, n=10, step=900):
    rows = []
    for i in range(n):
        t = start + i * step
        o = 100.0 + i
        rows.append([t, o, o + 0.5, o - 0.5, o + 0.2, 10])
    return rows


def test_read_bars_since_pages_and_slices(tmp_path):
    page = FakePage()
    page.bars = _bars()
    res = driver.read_bars(page, 3, since_ts=1_700_000_000 + 4 * 900)
    assert page.bar_payloads[-1] == {"count": 3, "since": 1_700_003_600, "max_pages": 25}
    assert [r[0] for r in res["rows"]] == [1_700_006_300, 1_700_007_200, 1_700_008_100]
    assert res["total_after_since"] == 6 and res["clamped"] is False
    assert "/*tvmcp:bars*/" in page.exprs[-1] and "pageBack" in page.exprs[-1]


def test_read_bars_no_api_raises(tmp_path):
    page = FakePage()
    page.api = False
    with pytest.raises(ToolError, match="TradingViewApi"):
        driver.read_bars(page, 10)


def test_desktop_check_levels_end_to_end(tmp_path):
    page = FakePage(symbol="CME_MINI:ES1!", interval="15")
    page.bars = _bars()  # opens 100..109, highs +0.5
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_check_levels", {
        "levels": [{"name": "IBH", "price": 104.4}, {"name": "far", "price": 200}],
        "since": "2023-11-14T22:13:20Z",  # == 1_700_000_000
        "count": 100,
    })
    assert data["provider"] == "desktop" and data["symbol"] == "CME_MINI:ES1!"
    assert data["resolution_minutes"] == 15 and data["bars_checked"] == 10
    # since floored to the 15m boundary and forwarded to the bars read
    assert page.bar_payloads[-1]["since"] == 1_700_000_000 - (1_700_000_000 % 900)
    ibh, far = data["levels"]
    assert ibh["tagged"] is True and ibh["first_tag"]["from"] == "below"
    assert far["tagged"] is False and far["closest_approach"]["side"] == "below"
    assert far["closest_approach"]["distance"] == pytest.approx(200 - 109.5)
    assert "stale" in ibh["coverage_warning"]  # 2023 bars vs now


def test_desktop_check_levels_registers_read_only(tmp_path):
    mcp, _ = _build(tmp_path, read_only=True)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "tv_desktop_check_levels" in names
