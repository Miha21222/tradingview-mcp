"""Put the live TradingView Desktop workspace into a known state (M8 part B).

`prepare(page, ...)` is the first stage of the Pine build pipeline and a tool
of its own: restore a minimized window (a minimized Chromium throttles every
tab, so nothing paints and the editor never mounts), close blocking dialogs,
dock a floating Pine editor, open the editor with staged recovery until a LIVE
Monaco answers (a model AND a laid-out DOM node - `activateScriptEditorTab()`
/ `showWidget('pine-editor')` return without mounting it on this build, so the
footer button is stage 0), optionally clear studies, optionally navigate.

`clear_studies` is tri-state on purpose: the owner's chart carries paid
studies (LuxAlgo, Dark Trader) that must survive a build run - `"strategies"`
removes only `strategy()` scripts (via `metaInfo().isTVScriptStrategy`),
`"all"` is explicit, `"none"` is the default. Every action lands in `steps`.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from . import clock, driver, pine_editor, ui

CLEAR_MODES = ("none", "strategies", "all")
MIN_EDITOR_WIDTH = 200
_STAGE_NAMES = ("footer_button", "activateScriptEditorTab", "showWidget", "footer_toggle")
_STAGE_WAIT_S = 3.0

_HEALTH_JS = """
/*tvmcp:editor_health*/
(() => {
  const m = __FIND__;
  if (!m) return {live: false, has_model: false, visible: false, width: null, reason: 'no_monaco'};
  let model = null, node = null, width = null;
  try { model = m.editor.getModel(); } catch (e) {}
  try { node = m.editor.getDomNode(); } catch (e) {}
  try { width = m.editor.getLayoutInfo().width; } catch (e) {}
  const visible = !!(node && node.offsetParent !== null);
  return {live: !!model && visible, has_model: !!model, visible: visible,
          width: (typeof width === 'number') ? Math.round(width) : null};
})()
"""

_STAGE_JS = """
/*tvmcp:editor_stage*/
(new Promise(async (resolve) => {
  const p = __PAYLOAD__;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const footer = () => document.querySelector('[data-name="pine-dialog-button"]')
    || document.querySelector('[aria-label="Pine"]');
  const bar = () => window.TradingView && window.TradingView.bottomWidgetBar;
  let did = null;
  try {
    if (p.stage === 0) { const b = footer(); if (b) { b.click(); did = 'footer_button'; } }
    else if (p.stage === 1) {
      const b = bar();
      if (b && typeof b.activateScriptEditorTab === 'function') { b.activateScriptEditorTab(); did = 'activateScriptEditorTab'; }
    } else if (p.stage === 2) {
      const b = bar();
      if (b && typeof b.showWidget === 'function') { b.showWidget('pine-editor'); did = 'showWidget'; }
    } else if (p.stage === 3) {
      const b = footer();
      if (b) { b.click(); await sleep(400); b.click(); did = 'footer_toggle'; }
    }
  } catch (e) { return resolve({stage: p.stage, did: did, error: String(e).slice(0, 200)}); }
  resolve({stage: p.stage, did: did});
}))
"""

_DOCK_JS = """
/*tvmcp:dock*/
(() => {
  __HELPERS__
  let btn = document.querySelector('[data-name="move-overlay-to-split"]');
  if (!(btn && visible(btn))) {
    const re = /move overlay to split|split-view|split view|в разделённ|разделённый вид|прикрепить/i;
    btn = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible)
      .find(b => re.test([attr(b, 'title'), attr(b, 'aria-label'), txt(b)].join(' | ')));
  }
  if (!btn) return {floating: false, docked: false};
  try { btn.click(); } catch (e) { return {floating: true, docked: false, error: String(e).slice(0, 200)}; }
  return {floating: true, docked: true};
})()
"""

_CLEAR_JS = """
/*tvmcp:clear_studies*/
(new Promise(async (resolve) => {
  if (!window.TradingViewApi) return resolve({no_api: true});
  const p = __PAYLOAD__;
  const ch = window.TradingViewApi.activeChart();
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const isStrategy = (id) => {
    try { return !!ch.getStudyById(id)._study.metaInfo().isTVScriptStrategy; } catch (e) { return false; }
  };
  const all = ch.getAllStudies();
  const targets = all.filter(s => p.mode === 'all' || isStrategy(s.id));
  const removed = [], failed = [];
  for (const s of targets) {
    try { ch.removeEntity(s.id); removed.push({id: s.id, title: s.name}); }
    catch (e) { failed.push({id: s.id, title: s.name, error: String(e).slice(0, 120)}); }
  }
  // getAllStudies() lags removal by a tick - poll until the count stabilizes.
  let last = -1, stable = 0;
  for (let i = 0; i < 20 && removed.length; i++) {
    await sleep(150);
    const n = ch.getAllStudies().length;
    if (n === last) stable++; else { stable = 0; last = n; }
    if (stable >= 2) break;
  }
  resolve({mode: p.mode, removed: removed, failed: failed,
           remaining: ch.getAllStudies().map(s => ({id: s.id, title: s.name}))});
}))
"""


def parse_clear_mode(value) -> str:
    """`"none"|"strategies"|"all"`; bool True = "all", False = "none"."""
    if value is True:
        return "all"
    if value is False or value is None:
        return "none"
    v = str(value).strip().lower()
    if v in ("true", "yes"):
        return "all"
    if v in ("false", "no", ""):
        return "none"
    if v not in CLEAR_MODES:
        raise ToolError(f"clear_studies must be one of {CLEAR_MODES} (or true/false), got {value!r}")
    return v


def step(steps: list, name: str, t0: float, **fields) -> dict:
    """Append `{step, ms since t0, **fields}` to `steps` and return it."""
    entry = {"step": name, "ms": int(round((clock.now() - t0) * 1000)), **fields}
    steps.append(entry)
    return entry


def restore_window(page) -> bool | str:
    """Un-minimize the app window via Browser.setWindowBounds; bringToFront otherwise.

    True = was minimized and is now normal; False = nothing to restore (only
    brought to front); 'unsupported' = the Browser domain is not available on
    this session (fell back to Page.bringToFront when possible).
    """
    call = getattr(page, "call", None)
    if call is None:
        return "unsupported"

    def front():
        try:
            call("Page.bringToFront", {})
        except ToolError:
            pass

    try:
        win = call("Browser.getWindowForTarget", {}) or {}
        wid = win.get("windowId")
        state = (win.get("bounds") or {}).get("windowState")
        if wid is not None and state == "minimized":
            call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
            front()
            return True
        front()
        return False
    except ToolError:
        front()
        return "unsupported"


def editor_health(page) -> dict:
    return page.eval(pine_editor._js(_HEALTH_JS)) or {"live": False, "width": None}


def open_editor(page, steps: list | None = None, t0: float | None = None) -> dict:
    """Open the Pine editor with staged recovery until Monaco is live.

    Returns {live, width, stages_tried, via}. Never raises - the caller
    decides what a dead editor means.
    """
    steps = steps if steps is not None else []
    t0 = clock.now() if t0 is None else t0
    tried: list[str] = []
    health = editor_health(page)
    if health.get("live"):
        step(steps, "editor_open", t0, via="already_open", width=health.get("width"))
        return {"live": True, "width": health.get("width"), "stages_tried": tried, "via": "already_open"}
    for stage in range(len(_STAGE_NAMES)):
        res = page.eval(pine_editor._js(_STAGE_JS, {"stage": stage}), await_promise=True) or {}
        tried.append(res.get("did") or f"{_STAGE_NAMES[stage]}:unavailable")
        deadline = clock.now() + _STAGE_WAIT_S
        while True:
            health = editor_health(page)
            if health.get("live"):
                step(steps, "editor_open", t0, via=res.get("did"), stages_tried=list(tried),
                     width=health.get("width"))
                return {"live": True, "width": health.get("width"), "stages_tried": tried,
                        "via": res.get("did")}
            if clock.now() >= deadline:
                break
            clock.sleep(0.25)
    step(steps, "editor_open", t0, via=None, stages_tried=list(tried), live=False)
    return {"live": False, "width": health.get("width"), "stages_tried": tried, "via": None}


def clear_studies(page, mode: str) -> dict:
    if mode == "none":
        return {"mode": "none", "removed": [], "failed": [], "remaining": None}
    res = page.eval(pine_editor._js(_CLEAR_JS, {"mode": mode}), await_promise=True)
    if not res or res.get("no_api"):
        raise ToolError(driver._NO_API)
    return res


def prepare(page, clear_studies_mode="none", symbol: str | None = None,
            timeframe: str | None = None) -> dict:
    mode = parse_clear_mode(clear_studies_mode)
    steps: list[dict] = []
    t0 = clock.now()
    notes: list[str] = []

    restored = restore_window(page)
    step(steps, "window_restore", t0, window_restored=restored)

    dlg = ui.dismiss_dialogs(page)
    step(steps, "dismiss_dialogs", t0, dismissed=len(dlg["dismissed"]), remaining=len(dlg["remaining"]))

    dock = page.eval(ui._js(_DOCK_JS)) or {}
    step(steps, "dock_editor", t0, floating=bool(dock.get("floating")), docked=bool(dock.get("docked")))

    opened = open_editor(page, steps, t0)
    width = opened.get("width")
    if not opened["live"]:
        notes.append("Pine editor never mounted a live Monaco (tried: "
                     + ", ".join(opened["stages_tried"]) + ") - ask the user to click "
                     "once inside the Pine editor code area, then retry.")
    elif width is not None and width < MIN_EDITOR_WIDTH:
        notes.append(f"Pine editor panel collapsed ({width}px) - drag the splitter wider; "
                     "buttons are icon-only at this width.")

    cleared = clear_studies(page, mode)
    step(steps, "clear_studies", t0, mode=mode, removed=len(cleared["removed"]),
         failed=len(cleared.get("failed") or []))

    if symbol or timeframe:
        nav = driver._nav(page, symbol, timeframe)
        step(steps, "navigate", t0, symbol=symbol, timeframe=timeframe,
             ready=nav.get("ready"), no_api=bool(nav.get("no_api")))
        if nav.get("no_api"):
            notes.append("navigation skipped: the chart API is not exposed on this page")

    studies = driver.list_studies(page)
    step(steps, "list_studies", t0, count=len(studies.get("studies") or []))
    return {
        "symbol": studies.get("symbol"),
        "resolution": studies.get("resolution"),
        "studies": [{"id": s.get("id"), "title": s.get("title")} for s in studies.get("studies") or []],
        "pine_editor_open": bool(opened["live"]),
        "pine_editor_width": width,
        "window_restored": restored,
        "editor_docked": bool(dock.get("docked")),
        "blocking_dialogs": dlg,
        "cleared_studies": cleared["removed"],
        "note": " ".join(notes) or None,
        "steps": steps,
    }
