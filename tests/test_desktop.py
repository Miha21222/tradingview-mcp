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


class FakePage:
    """Implements the DesktopPage interface the driver functions rely on."""

    def __init__(self, symbol="OANDA:EURUSD", interval="1h", shapes=None,
                 studies=None):
        self.symbol = symbol
        self.interval = interval
        self.typed = []
        self.pressed = []
        self.shots = []
        self.exprs = []
        # id -> {"name": ..., "points": [...], "text": ...}
        self.shapes = shapes if shapes is not None else {}
        # [{"id": ..., "title": ..., "plots": [...], "rows": [...], "boxes": [...]}]
        self.studies = studies if studies is not None else []
        self.study_payloads = []
        self._next_id = 0

    def eval(self, expr, await_promise=False):
        self.exprs.append(expr)
        if "/*tvmcp:studies*/" in expr:
            return {
                "symbol": self.symbol,
                "resolution": "60",
                "studies": self.studies,
            }
        if "/*tvmcp:plots*/" in expr or "/*tvmcp:graphics*/" in expr:
            payload = json.loads(expr.split("const p = ", 1)[1].split(";", 1)[0])
            self.study_payloads.append(payload)
            q = payload["query"]
            matches = [s for s in self.studies
                       if s["id"] == q or q.lower() in s["title"].lower()]
            if len(matches) != 1:
                return {"__miss": True, "not_found": not matches,
                        "candidates": [{"id": s["id"], "title": s["title"]}
                                       for s in self.studies]}
            s = matches[0]
            if "/*tvmcp:plots*/" in expr:
                return {"id": s["id"], "title": s["title"],
                        "plots": s.get("plots", []),
                        "columns": ["time", "plot_0"],
                        "total_bars": 400,
                        "rows": s.get("rows", [])[-payload["count"]:]}
            return {"id": s["id"], "title": s["title"],
                    "counts": {"boxes": len(s.get("boxes", []))},
                    "boxes": s.get("boxes", [])[-payload["limit"]:]}
        if "/*tvmcp:list*/" in expr:
            return {
                "symbol": self.symbol,
                "resolution": "60",
                "visible_time_range": {"from": 1787151600, "to": 1787792400},
                "visible_price_range": {"from": 1.16, "to": 1.17},
                "shapes": [{"id": i, **s} for i, s in self.shapes.items()],
            }
        if "/*tvmcp:draw*/" in expr:
            payload = json.loads(expr.split("const p = ", 1)[1].split(";", 1)[0])
            self._next_id += 1
            sid = f"fake{self._next_id}"
            self.shapes[sid] = {
                "name": payload["shape"],
                "points": payload["points"],
                "text": payload["text"],
            }
            return {"created": [{"id": sid, "name": payload["shape"]}],
                    "points": payload["points"]}
        if "/*tvmcp:remove*/" in expr:
            sid = json.loads(expr.split("s.id === ", 1)[1].split(");", 1)[0])
            if sid not in self.shapes:
                return {"found": False, "present": list(self.shapes)}
            gone = self.shapes.pop(sid)
            return {"found": True, "id": sid, "name": gone["name"],
                    "text": gone["text"]}
        return {
            "title": "TradingView Desktop",
            "url": "https://www.tradingview.com/chart/",
            "visible": True,
            "symbol": self.symbol,
            "interval": self.interval,
        }

    def type_text(self, text):
        self.typed.append(text)

    def press(self, key):
        self.pressed.append(key)

    def screenshot(self, path):
        self.shots.append(path)
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG fake")


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
    from tvmcp.desktop import warnings as w

    w._printed = False
    yield
    w._printed = False


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


def test_registers_ten_tools(tmp_path):
    mcp, _ = _build(tmp_path)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {
        "tv_desktop_status",
        "tv_desktop_screenshot",
        "tv_desktop_list_drawings",
        "tv_desktop_list_studies",
        "tv_desktop_read_study_plots",
        "tv_desktop_read_study_graphics",
        "tv_desktop_set_symbol",
        "tv_desktop_set_timeframe",
        "tv_desktop_draw",
        "tv_desktop_remove_drawing",
    }


def test_read_only_excludes_navigation_and_writes(tmp_path):
    mcp, _ = _build(tmp_path, read_only=True)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {
        "tv_desktop_status",
        "tv_desktop_screenshot",
        "tv_desktop_list_drawings",
        "tv_desktop_list_studies",
        "tv_desktop_read_study_plots",
        "tv_desktop_read_study_graphics",
    }


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


def test_set_symbol_types_and_verifies(tmp_path):
    page = FakePage(symbol="EURUSD")
    mcp, _ = _build(tmp_path, page)
    data = _data(mcp, "tv_desktop_set_symbol", {"symbol": "eurusd"})
    assert data["requested"] == "OANDA:EURUSD"
    assert "OANDA:EURUSD" in page.typed
    assert "Enter" in page.pressed


def test_set_symbol_mismatch_raises(tmp_path):
    page = FakePage(symbol="AAPL")  # app resolved something else
    mcp, _ = _build(tmp_path, page)
    with pytest.raises(ToolError, match="different listing"):
        asyncio.run(mcp.call_tool("tv_desktop_set_symbol", {"symbol": "EURUSD"}))


def test_set_timeframe_uses_quick_keys(tmp_path):
    mcp, page = _build(tmp_path)
    data = _data(mcp, "tv_desktop_set_timeframe", {"timeframe": "H4"})
    assert data["requested"] == "H4"
    assert "240" in page.typed


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
        "query": "lux1", "limit": 10, "kinds": ["boxes"]}


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

