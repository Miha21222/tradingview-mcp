"""`desktop` toolset: drive the owner's real TradingView Desktop app over CDP.

Opt-in only (`TV_TOOLSETS=...,desktop`), never default; first use prints a ToS/ban
warning to stderr (see warnings.py). Requires the app running with
`--remote-debugging-port` (scripts/start-tv-desktop.ps1). Navigation tools
(set_symbol/set_timeframe) mutate the user's live workspace, so they do not
register under TV_READ_ONLY=1; status/screenshot always register with the toolset.

First slice: status, screenshot, set symbol, set timeframe. Deferred: drawings,
bar replay, Pine editor, Strategy Tester reads (see docs/PLAN.md). Brittle by
nature - TradingView UI updates can break selectors/keyboard flows; errors say so.
Not affiliated with TradingView, Inc.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, Callable

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import Settings
from ..symbols import resolve, resolve_timeframe
from . import driver
from .warnings import warn_once

_PROVIDER = "desktop"


def register(mcp: Any, settings: Settings, page_factory: Callable | None = None) -> None:
    pages = page_factory or driver.cdp_page
    out_dir = settings.chart_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_status() -> dict:
        """Whether TradingView Desktop is reachable over CDP, and what it shows.

        Returns the app's current page title/url and the active symbol/interval as
        read from the UI (untrusted display strings). Opt-in tool; automating the
        desktop app may violate TradingView's ToS.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            status = driver.read_status(page)
        return {"connected": True, "provider": _PROVIDER, **status}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_screenshot() -> dict:
        """Screenshot the TradingView Desktop window to a PNG and return its path.

        Captures whatever the app currently shows (chart, dialogs, watchlists).
        Use after tv_desktop_set_symbol/timeframe to see the user's real chart with
        their indicators and drawings.
        """
        warn_once()
        fname = f"desktop_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        out_path = out_dir / fname
        with pages(settings.cdp_url) as page:
            status = driver.read_status(page)
            driver.screenshot(page, out_path)
        return {"path": str(out_path), "provider": _PROVIDER, **status}

    if settings.read_only:
        return  # navigation mutates the live workspace - excluded under TV_READ_ONLY

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_set_symbol(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
    ) -> dict:
        """Switch the active TradingView Desktop chart to a symbol.

        Types into the app's symbol quick-search (keyboard automation), so the
        chart layout the user sees really changes. Verify via the returned
        `symbol` field or a follow-up tv_desktop_screenshot.
        """
        warn_once()
        sym = resolve(symbol)
        with pages(settings.cdp_url) as page:
            status = driver.set_symbol(page, sym.tv)
        shown = status.get("symbol")
        if shown and sym.tv.split(":")[-1] not in shown.replace(":", ""):
            raise ToolError(
                f"Asked for {sym.tv} but the app now shows {shown!r} - TradingView "
                "may have matched a different listing; check with tv_desktop_screenshot"
            )
        return {"requested": sym.tv, "provider": _PROVIDER, **status}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_set_timeframe(
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")],
    ) -> dict:
        """Switch the active TradingView Desktop chart to a timeframe.

        Uses TV's interval quick-type (e.g. '240' for H4). Verify via the returned
        `interval` field or a follow-up tv_desktop_screenshot.
        """
        warn_once()
        tf = resolve_timeframe(timeframe)
        with pages(settings.cdp_url) as page:
            status = driver.set_timeframe(page, tf.canonical)
        return {"requested": tf.canonical, "provider": _PROVIDER, **status}
