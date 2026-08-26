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

    def __init__(self, symbol="OANDA:EURUSD", interval="1h"):
        self.symbol = symbol
        self.interval = interval
        self.typed = []
        self.pressed = []
        self.shots = []

    def eval(self, expr):
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


def test_registers_four_tools(tmp_path):
    mcp, _ = _build(tmp_path)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {
        "tv_desktop_status",
        "tv_desktop_screenshot",
        "tv_desktop_set_symbol",
        "tv_desktop_set_timeframe",
    }


def test_read_only_excludes_navigation(tmp_path):
    mcp, _ = _build(tmp_path, read_only=True)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"tv_desktop_status", "tv_desktop_screenshot"}


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


def test_timeframe_map_covers_all_canonicals():
    from tvmcp.desktop.driver import _TF_KEYS
    from tvmcp.symbols import _TIMEFRAMES

    assert set(_TF_KEYS) == set(_TIMEFRAMES)

