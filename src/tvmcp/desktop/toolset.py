"""`desktop` toolset: drive the owner's real TradingView Desktop app over CDP.

Opt-in only (`TV_TOOLSETS=...,desktop`), never default; first use prints a ToS/ban
warning to stderr (see warnings.py). Requires the app running with
`--remote-debugging-port` (scripts/start-tv-desktop.ps1). Navigation tools
(set_symbol/set_timeframe) mutate the user's live workspace, so they do not
register under TV_READ_ONLY=1; status/screenshot always register with the toolset.

Shipped: status, screenshot, set symbol/timeframe (chart API, keyboard
fallback), viewport (scroll to date / visible range with history paging),
drawings (list/draw/remove via the in-page TradingViewApi charting API - no UI
clicking), studies (list indicators, read plot values, read Pine-drawn
boxes/lines/labels incl. compact levels/zones, set inputs with read-back),
Strategy Tester report + orders, bar replay (start/step/trade/status/stop),
Pine Editor (get/set source, compile-on-chart with Monaco markers, save,
list/open the user's saved scripts). M7 borrowed the mechanisms from
tradesdontlie/tradingview-mcp (MIT) - see docs/PLAN.md for landmines.
M8 (desktop-3) adds the app launcher (`tv_desktop_launch`), generic UI
find/click, level-tag checks on the chart's own bars, and scored chart-tab
binding in the driver (`target` in tv_desktop_status).
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
from ..scan import levels as levels_mod
from . import driver, launcher, pine_editor, replay, strategy_tester, ui
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
        return {"connected": True, "provider": _PROVIDER, **status,
                "target": driver.bound_target()}

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
        compact: Annotated[bool, Field(description=(
            "Collapse output to unique price `levels` (horizontal lines) and "
            "`zones` ({high, low} boxes) with counts - a few hundred bytes "
            "instead of every object; use for level/zone indicators"))] = False,
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
            res = driver.read_study_graphics(page, study, limit, kinds, compact)
        return {"provider": _PROVIDER, **res}


    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_read_strategy(
        orders: Annotated[int, Field(ge=0, le=200, description=(
            "How many most-recent orders to include (0 = metrics only)"))] = 0,
        open_panel: Annotated[bool, Field(description=(
            "Open the Strategy Tester panel first (TradingView computes the "
            "report only once the panel has been shown)"))] = True,
        wait_seconds: Annotated[int, Field(ge=0, le=30, description=(
            "How long to wait for the report to compute"))] = 6,
    ) -> dict:
        """Read the Strategy Tester report of the strategy on the live Desktop chart.

        Returns the strategies present, which one is computed (the one selected
        in the panel), its key stats (net profit, profit factor, max drawdown,
        trade counts, percent profitable, Sharpe/Sortino, commission paid,
        buy-and-hold return for the benchmark), long/short splits, and
        optionally the last `orders` fills with bar times. Read-only: it never
        unhides a hidden strategy - a hidden one is reported so you can ask the
        user. Pair with tv_desktop_pine_compile to iterate a strategy on the
        user's own chart. Always compare against `buy_hold_return` before
        calling a result good.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = strategy_tester.read_strategy(page, orders, open_panel, wait_seconds * 1000)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_replay_status() -> dict:
        """Bar-replay state of the live Desktop chart.

        Returns whether replay is available/started/autoplaying, the replay
        cursor date (unix ms), and the paper replay-trading position and
        realized P&L. Use before tv_desktop_replay_step to know where the
        cursor is.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = replay.status(page)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_pine_get_source() -> dict:
        """Read the Pine source currently open in the Desktop app's Pine Editor.

        Opens the Pine Editor panel if needed and returns the editor text, its
        size, and Monaco's current markers (compile errors/warnings with
        line/column). Large protected scripts can be 100KB+ - read only when
        you need to edit. The source is the user's (or its author's) property;
        never copy it elsewhere.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = pine_editor.get_source(page)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_pine_list_scripts() -> dict:
        """List the user's own saved Pine scripts (name, id, version, modified).

        Reads the saved-scripts list the Pine Editor itself loads, from inside
        the logged-in app. Use a name with tv_desktop_pine_open_script to load
        one into the editor. Names are untrusted display strings.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = pine_editor.list_scripts(page)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_ui_find_element(
        query: Annotated[str, Field(min_length=1, max_length=200, description=(
            "Exact data-name, aria-label substring, button text/title substring, "
            "or a CSS selector (all case-insensitive except data-name)"))],
        limit: Annotated[int, Field(ge=1, le=100, description="Max matches")] = 20,
    ) -> dict:
        """Find visible UI elements in the TradingView Desktop window by name/label/text.

        Matches `[data-name=query]`, `aria-label` containing query, buttons whose
        text or title contains query, and query as a CSS selector. Returns
        {tag, data_name, aria_label, text, role, rect, in_dialog} per match -
        use it to discover what a dialog offers before tv_desktop_ui_click, or
        to check whether a panel/dialog is open. Texts are untrusted display
        strings from the app.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = ui.find_elements(page, query, limit)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_desktop_check_levels(
        levels: Annotated[list[dict], Field(description=(
            "Levels to check, each {'name': str, 'price': float} or "
            "{'name': str, 'high': float, 'low': float} (zone); max 50"))],
        since: Annotated[str, Field(description=(
            "ISO-8601 UTC time or session:<name>[@YYYY-MM-DD] (fixed UTC session "
            "hours, not DST-aware)"))],
        count: Annotated[int, Field(ge=10, le=5000, description=(
            "Max bars to read from the chart (history is paged back to `since`)"))] = 2000,
    ) -> dict:
        """Has the live chart's price tagged each level/zone since a time?

        Reads the active chart's own bars (the feed the user sees, `provider:
        desktop`) at its current symbol/resolution, paging history back to
        `since`, and reports per level: `tagged`, `first_tag` {time, from,
        high, low}, `closest_approach` when untagged, and `coverage_warning`
        when the bars cannot answer (history starts after `since`, stale last
        bar). Use it to verify plan levels (IB, PDH/PDL, session highs) against
        the chart the user is looking at.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            minutes = driver.read_resolution_minutes(page)
            since_ts = levels_mod.parse_since(since, minutes)
            bars = driver.read_bars(page, count, since_ts=int(since_ts.timestamp()))
        df = levels_mod.rows_to_df(bars.get("rows") or [])
        return {
            "provider": _PROVIDER,
            "symbol": bars.get("symbol"),
            "resolution": bars.get("resolution"),
            "resolution_minutes": minutes,
            "since": since_ts.isoformat().replace("+00:00", "Z"),
            "bars_checked": int(len(df)),
            "earliest_loaded": bars.get("earliest_loaded"),
            "pages_loaded": bars.get("pages_loaded"),
            "clamped": bars.get("clamped"),
            "latest_loaded": bars.get("latest_loaded"),
            "levels": levels_mod.check_levels(df, levels, since_ts, minutes,
                                              last_loaded=bars.get("latest_loaded")),
        }

    if settings.read_only:
        return  # navigation mutates the live workspace - excluded under TV_READ_ONLY

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_launch(
        wait_seconds: Annotated[int, Field(ge=1, le=120, description=(
            "How long to wait for CDP to answer and a chart tab to appear"))] = 30,
    ) -> dict:
        """Start TradingView Desktop with the CDP debug flag on TV_CDP_URL's port.

        If CDP already answers with a TradingView tab, returns
        `already_running: true` without touching anything. Refuses (with the
        exact fix) when another CDP app owns the port, or when TradingView is
        running WITHOUT the flag - it never closes the user's app for them.
        Finds the executable via TV_DESKTOP_EXE, the Store package, or
        %LOCALAPPDATA%/Programs/TradingView. Returns {launched, exe, pid, port,
        browser, chart_tab, waited_ms}; `chart_tab: false` means the user still
        has to log in / open a chart - check tv_desktop_status afterwards.
        """
        warn_once()
        return {"provider": _PROVIDER, **launcher.launch(settings, wait_seconds)}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_ui_click(
        target: Annotated[str, Field(min_length=1, max_length=200, description=(
            "Same query forms as tv_desktop_ui_find_element; must match exactly "
            "one visible element"))],
    ) -> dict:
        """Click one visible UI element in the TradingView Desktop window.

        Clicks only when the query matches exactly one element
        (`clicked: true`, `matched`). Zero matches return `miss: true` with
        visible clickable `candidates`; several matches return `ambiguous:
        true` with the matches - refine the query instead of guessing. Use
        for dialogs/menus the chart API does not cover; verify the effect with
        tv_desktop_ui_find_element or tv_desktop_screenshot. This clicks in
        the user's live app.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = ui.click_element(page, target)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_set_symbol(
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")],
    ) -> dict:
        """Switch the active TradingView Desktop chart to a symbol.

        Calls the chart's own API (`setSymbol`) and waits until the chart
        reports the new symbol with no loading spinner (`ready`), falling back
        to the keyboard quick-search when the API is unavailable (`method`).
        The layout the user sees really changes. Verify via the returned
        `api_symbol`/`symbol` fields or a follow-up tv_desktop_screenshot.
        """
        warn_once()
        sym = resolve(symbol)
        with pages(settings.cdp_url) as page:
            status = driver.set_symbol(page, sym.tv)
        shown = status.get("api_symbol") or status.get("symbol")
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

        Calls the chart's own API (`setResolution`) and waits for the reload
        (`ready`), with the interval quick-type as keyboard fallback. Verify
        via the returned `api_resolution`/`interval` fields or a follow-up
        tv_desktop_screenshot.
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

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_scroll_to_date(
        time: Annotated[int, Field(ge=0, description="Unix seconds to center the viewport on")],
        bars_each_side: Annotated[int, Field(ge=5, le=2000, description=(
            "How many bars of the current resolution to show on each side"))] = 25,
    ) -> dict:
        """Scroll the live Desktop chart to a past date (loading history as needed).

        Pages history back via the chart's data feed until the date is loaded,
        then sets the visible range around it. Returns the resulting visible
        range, how many pages were loaded, the earliest loaded bar, and
        `clamped` when the feed ran out before reaching the date (plan limits).
        Use before tv_desktop_screenshot for a past session, or before drawing
        on old bars.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            minutes = driver.read_resolution_minutes(page)
            res = driver.scroll_to_date(page, time, bars_each_side, minutes)
        return {"provider": _PROVIDER, "resolution_minutes": minutes, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_set_visible_range(
        from_time: Annotated[int, Field(ge=0, description="Unix seconds, left edge")],
        to_time: Annotated[int, Field(ge=0, description="Unix seconds, right edge")],
    ) -> dict:
        """Set the exact visible time range of the live Desktop chart.

        Pages history back first when `from_time` is older than what is loaded.
        Returns the actual visible range after the change (compare with the
        request; `clamped` = feed history ended before `from_time`).
        """
        warn_once()
        if to_time <= from_time:
            raise ToolError("to_time must be after from_time")
        with pages(settings.cdp_url) as page:
            res = driver.set_visible_range(page, from_time, to_time)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_set_study_inputs(
        study: Annotated[str, Field(description=(
            "Study id or case-insensitive title substring "
            "(from tv_desktop_list_studies)"))],
        inputs: Annotated[dict, Field(description=(
            "{input id or user-facing name: new value}, e.g. {'length': 50} "
            "or {'Fast Length': 3, 'Slow Length': 10}; ids/names from "
            "tv_desktop_list_studies"))],
    ) -> dict:
        """Change an indicator's input values on the live Desktop chart, and verify.

        Writes through the chart API, then reads the inputs back: `applied` is
        true only when every value read back equals what was requested;
        `mismatched` lists the rest (wrong type, out of range, option not in
        the list). Unknown input names error with the available inputs. Never
        report a settings change as done without `applied: true`.
        """
        warn_once()
        if not inputs:
            raise ToolError("inputs must be a non-empty mapping")
        with pages(settings.cdp_url) as page:
            res = driver.set_study_inputs(page, study, inputs)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_replay_start(
        time: Annotated[int | None, Field(ge=0, description=(
            "Unix seconds to start replay at (the first bar shown is the one "
            "at/after this time); omit for the earliest available"))] = None,
    ) -> dict:
        """Enter bar-replay mode on the live Desktop chart at a date.

        Starts TradingView's own replay (the user sees the replay toolbar), and
        waits until the replay cursor is ready. Then advance with
        tv_desktop_replay_step, read the chart/indicators as usual (they show
        only bars up to the cursor), and leave with tv_desktop_replay_stop.
        Fails when the date has no data on this timeframe (plan history
        limits) - retry with a more recent date or a higher timeframe.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = replay.start(page, None if time is None else time * 1000)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_replay_step(
        count: Annotated[int, Field(ge=1, le=500, description="Bars to advance")] = 1,
    ) -> dict:
        """Advance bar replay by `count` bars on the live Desktop chart.

        Each step waits for the replay cursor date to move; `stepped` < count
        means the replay reached the end of data (or stalled). Returns the new
        cursor date and the paper position/P&L. There is deliberately no
        autoplay tool: step as many bars as you need and read between steps.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = replay.step(page, count)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_replay_trade(
        side: Annotated[str, Field(pattern="^(buy|sell|close)$",
                                   description="buy, sell, or close the paper position")],
    ) -> dict:
        """Place a paper trade in TradingView's replay-trading panel (not a broker).

        Market buy/sell/close at the current replay bar, using the app's own
        replay paper account; returns the resulting position and realized P&L.
        This is a practice tool for walking through past sessions - it never
        touches a real account. Requires replay to be started.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = replay.trade(page, side)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_replay_stop() -> dict:
        """Leave bar-replay mode and return the live Desktop chart to realtime."""
        warn_once()
        with pages(settings.cdp_url) as page:
            res = replay.stop(page)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_pine_set_source(
        source: Annotated[str, Field(min_length=1, max_length=400_000,
                                     description="Full Pine script text")],
        overwrite_saved: Annotated[bool, Field(description=(
            "Allow replacing the text of a SAVED script that is open in the "
            "editor - TradingView auto-saves the change to the user's account "
            "as a new version. Only with the user's explicit OK"))] = False,
    ) -> dict:
        """Replace the text in the Desktop app's Pine Editor with `source`.

        Opens the Pine Editor panel if needed and sets the editor content.
        Refuses when a saved script is open unless `overwrite_saved` is true,
        because the app auto-saves edits to saved scripts as a new version in
        the user's account. Then call tv_desktop_pine_compile to add/update it
        on the chart and read errors. Prefer tv_pine_compile (server-side, no
        chart) for quick syntax checks.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = pine_editor.set_source(page, source, overwrite_saved)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_pine_compile() -> dict:
        """Compile the Pine Editor's script onto the live chart and return errors.

        Clicks the editor's "Add to chart" / "Update on chart" button (Ctrl+Enter
        fallback), waits, then returns Monaco's markers split into `errors` and
        `warnings` (line/column/message), `ok`, and whether a new study
        appeared on the chart. Loop: fix the source, tv_desktop_pine_set_source,
        tv_desktop_pine_compile, until `ok`. For a strategy(), follow with
        tv_desktop_read_strategy.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = pine_editor.compile_on_chart(page)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_pine_save() -> dict:
        """Save the Pine Editor's script to the user's TradingView account (Ctrl+S).

        Silent for an already-named script; a brand-new script opens a name
        dialog, which this tool confirms only if the dialog's Save button is
        found - otherwise it asks you to have the user name it once.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = pine_editor.save(page)
        return {"provider": _PROVIDER, **res}

    @mcp.tool(tags={"desktop"}, annotations={"readOnlyHint": False, "openWorldHint": True})
    def tv_desktop_pine_open_script(
        name: Annotated[str, Field(min_length=1, description=(
            "Saved script name (case-insensitive; substring accepted) from "
            "tv_desktop_pine_list_scripts"))],
    ) -> dict:
        """Load one of the user's saved Pine scripts into the Pine Editor.

        Fetches the script's latest version the way the editor does and sets
        the editor text (overwriting what was there). Then edit, compile with
        tv_desktop_pine_compile, and save with tv_desktop_pine_save.
        """
        warn_once()
        with pages(settings.cdp_url) as page:
            res = pine_editor.open_script(page, name)
        return {"provider": _PROVIDER, **res}
