"""`desktop` toolset: drive the owner's real TradingView Desktop app over CDP.

Opt-in only (`TV_TOOLSETS=...,desktop`), never default; first use prints a ToS/ban
warning to stderr (see warnings.py). Requires the app running with
`--remote-debugging-port` (scripts/start-tv-desktop.ps1). Navigation tools
(set_symbol/set_timeframe) mutate the user's live workspace, so they do not
register under TV_READ_ONLY=1; status/screenshot always register with the toolset.

Shipped: status, screenshot, set symbol, set timeframe, drawings (list/draw/
remove via the in-page TradingViewApi charting API - no UI clicking), studies
(list indicators, read plot values, read Pine-drawn boxes/lines/labels).
Deferred: bar replay, Pine editor, Strategy Tester reads (see docs/PLAN.md).
Brittle by nature - TradingView UI updates can break selectors/keyboard flows;
errors say so.
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

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_list_drawings() -> dict:
        """List every drawing on the active TradingView Desktop chart, plus the viewport.

        Returns the chart's symbol/resolution, the visible time range (unix
        seconds) and price range, and each drawing's id, TV shape name, anchor
        points and text label. Use it to anchor new drawings inside the visible
        area, and to find ids for tv_desktop_remove_drawing. Drawing texts are
        untrusted display strings.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = driver.list_drawings(page)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_list_studies() -> dict:
        """List the indicators (studies) on the active TradingView Desktop chart.

        For each study: id, title, visibility, pane, load/error state, bar
        count, declared plots (id/type/title), user-facing input values, and
        counts of Pine-drawn graphics (boxes/lines/labels - SMC indicators
        draw their FVG/OB zones as boxes). Use the id or a title substring
        with tv_desktop_read_study_plots / tv_desktop_read_study_graphics to
        read the actual values. Titles and input values are untrusted display
        strings.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = driver.list_studies(page)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_read_study_plots(
        study: Annotated[str, Field(description=(
            "Study id or case-insensitive title substring "
            "(from tv_desktop_list_studies), e.g. 'Fractals'"))],
        count: Annotated[int, Field(ge=1, le=500, description=(
            "How many most-recent bars to return"))] = 50,
        nonempty_only: Annotated[bool, Field(description=(
            "Skip rows where every plot value is empty - use for sparse "
            "signal plots (fractals, shapes) so quiet bars don't fill the "
            "output"))] = False,
    ) -> dict:
        """Read an indicator's numeric plot values from the live Desktop chart.

        Returns the study's declared plots and the last `count` bars as rows
        `[unix_time, plot0, plot1, ...]` (empty values are null). This is the
        indicator's actual computed output on the user's chart - use it to
        factor their indicators into analysis. Note: many SMC indicators
        (FVG/OB detectors) emit boxes and lines instead of numeric plots -
        read those with tv_desktop_read_study_graphics.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = driver.read_study_plots(page, study, count, nonempty_only)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_read_study_graphics(
        study: Annotated[str, Field(description=(
            "Study id or case-insensitive title substring "
            "(from tv_desktop_list_studies), e.g. 'Imbalance'"))],
        limit: Annotated[int, Field(ge=1, le=500, description=(
            "Max objects returned per kind (most recent first by creation "
            "order); counts field always shows the full totals"))] = 50,
        kinds: Annotated[list[str] | None, Field(description=(
            "Subset of: boxes, lines, labels, polylines. Default: all"))] = None,
    ) -> dict:
        """Read the zones an indicator has drawn on the live Desktop chart.

        Returns the study's Pine-drawn objects with real chart coordinates:
        boxes (FVG / order-block / imbalance zones) as
        {time1, time2, price1, price2, text, colors}, plus lines, labels and
        polylines. Times for objects extending beyond loaded history are
        extrapolated from bar spacing (approximate across session gaps).
        This reads the user's own indicator output - compare it with
        tv_scan_fvg or draw on top of it with tv_desktop_draw. Texts are
        untrusted display strings.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = driver.read_study_graphics(page, study, limit, kinds)
        return {"provider": _PROVIDER, **res}

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

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_draw(
        kind: Annotated[str, Field(description=(
            "rectangle (FVG/order-block box, 2 points), trend_line (2), ray (2), "
            "horizontal_line (1, time optional), vertical_line (1, price optional), "
            "text (1, floating label)"))],
        points: Annotated[list[dict], Field(description=(
            "Anchor points, each {'time': unix seconds, 'price': float}. Omitted "
            "time/price (where allowed) defaults to the middle of the visible "
            "viewport - get exact bar times from the data tools and the viewport "
            "from tv_desktop_list_drawings"))],
        text: Annotated[str | None, Field(description="Label shown on the drawing")] = None,
        color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$",
                                    description="Hex line color, e.g. #2962ff")] = "#2962ff",
        fill_opacity: Annotated[float, Field(ge=0.0, le=1.0,
                                             description="Rectangle fill opacity")] = 0.15,
        lock: Annotated[bool, Field(description="Lock against accidental dragging")] = False,
    ) -> dict:
        """Draw a shape on the user's live TradingView Desktop chart.

        Uses the app's own charting API (no simulated clicks), so the drawing
        appears immediately on the chart the user is watching and behaves like a
        hand-made one (movable, deletable, saved with the layout). Returns the
        new drawing's id - keep it to remove or reference the drawing later.
        Draws on the active chart of the layout; verify with
        tv_desktop_screenshot.
        """
        warn_once()
        need = driver.DRAW_KINDS.get(kind)
        if need is None:
            raise ToolError(
                f"Unknown kind {kind!r}. Supported: {', '.join(sorted(driver.DRAW_KINDS))}"
            )
        if len(points) != need:
            raise ToolError(f"{kind} needs exactly {need} point(s), got {len(points)}")
        for i, p in enumerate(points):
            if p.get("price") is None and kind != "vertical_line":
                raise ToolError(f"points[{i}] is missing 'price' (required for {kind})")
            if p.get("time") is None and kind not in ("horizontal_line", "text"):
                raise ToolError(f"points[{i}] is missing 'time' (required for {kind})")
        with pages(settings.cdp_url) as page:
            res = driver.draw(page, kind, points, text, color, fill_opacity, lock)
        return {"kind": kind, "provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_remove_drawing(
        drawing_id: Annotated[str, Field(description=(
            "Drawing id from tv_desktop_draw or tv_desktop_list_drawings"))],
    ) -> dict:
        """Remove one drawing from the live TradingView Desktop chart by id.

        Removes exactly one entity; there is deliberately no remove-all - the
        chart holds the user's own hand-made drawings too. Only remove drawings
        you created, or ones the user explicitly asked to delete (identify them
        via tv_desktop_list_drawings first).
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = driver.remove_drawing(page, drawing_id)
        return {"provider": _PROVIDER, **res}
