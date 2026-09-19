"""Drive the Pine Editor panel of the live TradingView Desktop app (M7).

Mechanism (borrowed from tradesdontlie/tradingview-mcp pine.js): the editor is
a Monaco instance mounted by React; the instance is reached by walking the
`__reactFiber$*` parent chain from `.monaco-editor.pine-editor-monaco` until a
node whose `memoizedProps.value.monacoEnv.editor.getEditors()` is non-empty.
That walk is build-fragile by nature - every tool checks it first and says so
when it fails. Source goes in with `editor.setValue(...)`; compile = click the
panel's "Add to chart" / "Update on chart" / "Save and add to chart" button
(Ctrl+Enter as fallback), then read Monaco's model markers for errors and diff
`getAllStudies()` to learn whether a study was added. Saved-script list/open
call the same `pine-facade` endpoints the app itself calls, from inside the
page with its own cookies (the user's own scripts only).

Pre-check Pine with `tv_pine_compile` (server-side, no chart needed) before
pushing into the editor - it's faster and leaves the user's editor alone.
"""

from __future__ import annotations

import json
import time

from fastmcp.exceptions import ToolError

from .driver import CTRL

_NO_EDITOR = (
    "Pine Editor is not reachable: the panel did not open, or the Monaco "
    "instance is not where this app build mounts it (React fiber walk failed). "
    "Open the Pine Editor tab by hand and retry; if it persists the pine-editor "
    "slice needs rework for this TradingView Desktop build."
)

_FIND_MONACO = """
(function findMonacoEditor() {
  const container = document.querySelector('.monaco-editor.pine-editor-monaco');
  if (!container) return null;
  let el = container, fiberKey = null;
  for (let i = 0; i < 20 && el; i++) {
    fiberKey = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
    if (fiberKey) break;
    el = el.parentElement;
  }
  if (!fiberKey) return null;
  let cur = el[fiberKey];
  for (let d = 0; d < 15 && cur; d++) {
    const v = cur.memoizedProps && cur.memoizedProps.value;
    if (v && v.monacoEnv && v.monacoEnv.editor && typeof v.monacoEnv.editor.getEditors === 'function') {
      const eds = v.monacoEnv.editor.getEditors();
      if (eds.length) return {editor: eds[0], env: v.monacoEnv};
    }
    cur = cur.return;
  }
  return null;
})()
"""

_OPEN_JS = """
/*tvmcp:pine_open*/
(new Promise(async (resolve) => {
  const find = () => __FIND__;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  if (find()) return resolve({ready: true, opened: false});
  // The footer's Pine button is what reliably mounts the editor in the
  // desktop build (bottomWidgetBar.activateScriptEditorTab()/showWidget
  // return without opening it - verified 2026-09-19 on TV Desktop 3.4.x).
  const btn = document.querySelector('[data-name="pine-dialog-button"]')
    || document.querySelector('[aria-label="Pine"]');
  if (btn) btn.click();
  else {
    try {
      const b = window.TradingView && window.TradingView.bottomWidgetBar;
      if (b && typeof b.activateScriptEditorTab === 'function') b.activateScriptEditorTab();
      else if (b && typeof b.showWidget === 'function') b.showWidget('pine-editor');
    } catch (e) {}
  }
  for (let i = 0; i < 40; i++) {
    await sleep(200);
    if (find()) return resolve({ready: true, opened: true, via: btn ? 'pine-dialog-button' : 'bottomWidgetBar'});
  }
  resolve({ready: false});
}))
"""

_MARKERS_SNIPPET = """
  const markers = (() => {
    const m = __FIND__;
    if (!m) return null;
    const model = m.editor.getModel();
    if (!model) return [];
    return m.env.editor.getModelMarkers({resource: model.uri}).map(mk => ({
      line: mk.startLineNumber, column: mk.startColumn,
      severity: mk.severity >= 8 ? 'error' : (mk.severity >= 4 ? 'warning' : 'info'),
      message: String(mk.message || '').slice(0, 300),
    }));
  })();
"""

_GET_JS = """
/*tvmcp:pine_get*/
(() => {
  const m = __FIND__;
  if (!m) return {no_editor: true};
  const src = m.editor.getValue();
  __MARKERS__
  return {source: src, lines: src.split('\\n').length, chars: src.length, markers: markers || []};
})()
"""

_SET_JS = """
/*tvmcp:pine_set*/
(() => {
  const m = __FIND__;
  if (!m) return {no_editor: true};
  const p = __PAYLOAD__;
  // A saved script open in the editor is auto-saved to the user's account as a
  // NEW VERSION when its text changes (verified 2026-09-19: one setValue
  // bumped the owner's script to v2). Refuse unless the caller opted in.
  const saveBtn = document.querySelector('[class*="saveButton"]');
  const savedOpen = !!(saveBtn && /saved/i.test(saveBtn.className));
  if (savedOpen && !p.overwrite_saved) return {saved_script_open: true};
  m.editor.setValue(p.source);
  const now = m.editor.getValue();
  return {lines: now.split('\\n').length, chars: now.length, applied: now === p.source,
          replaced_saved_script: savedOpen};
})()
"""

_COMPILE_CLICK_JS = """
/*tvmcp:pine_click*/
(() => {
  if (!__FIND__) return {no_editor: true};
  let studies = null;
  try { studies = window.TradingViewApi.activeChart().getAllStudies().length; } catch (e) {}
  // Buttons are icon-only in the desktop build (label lives in `title`), and
  // localized (RU verified 2026-09-19) - match text/title/aria-label, EN + RU.
  const btns = Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null);
  const label = (b) => [(b.textContent || ''), b.getAttribute('title') || '', b.getAttribute('aria-label') || ''].join(' | ').trim();
  const byText = (re) => btns.find(b => re.test(label(b)));
  const order = [[/save and add to chart|сохранить и добавить/i, 'Save and add to chart'],
                 [/\badd to chart\b|добавить на график/i, 'Add to chart'],
                 [/update on chart|обновить на графике/i, 'Update on chart']];
  for (const [re, name] of order) {
    const b = byText(re);
    if (b) { b.click(); return {clicked: name, studies_before: studies}; }
  }
  try { __FIND__.editor.focus(); } catch (e) {}
  return {clicked: null, studies_before: studies};
})()
"""

_COMPILE_RESULT_JS = """
/*tvmcp:pine_result*/
(() => {
  if (!__FIND__) return {no_editor: true};
  __MARKERS__
  let studies = null;
  try { studies = window.TradingViewApi.activeChart().getAllStudies().length; } catch (e) {}
  return {markers: markers || [], studies_after: studies};
})()
"""

_SAVE_DIALOG_JS = """
/*tvmcp:pine_save*/
(() => {
  const dlg = document.querySelector('[role="dialog"]');
  if (!dlg) return {dialog: false};
  const all = Array.from(dlg.querySelectorAll('button'));
  const btn = all.find(b => /^(save|сохранить)$/i.test((b.textContent || '').trim()));
  if (!btn) return {dialog: true, clicked: false, buttons: all.map(b => (b.textContent || '').trim().slice(0, 30))};
  btn.click();
  return {dialog: true, clicked: true};
})()
"""

_LIST_JS = """
/*tvmcp:pine_list*/
(fetch('https://pine-facade.tradingview.com/pine-facade/list/?filter=saved', {credentials: 'include'})
  .then(r => r.json())
  .then(data => {
    if (!Array.isArray(data)) return {error: 'pine-facade returned a non-list (not logged in?)'};
    return {scripts: data.map(s => ({
      id: s.scriptIdPart || null, name: s.scriptName || s.scriptTitle || 'Untitled',
      title: s.scriptTitle || null, version: s.version || null, modified: s.modified || null,
      kind: s.extra && s.extra.kind ? s.extra.kind : null,
    }))};
  })
  .catch(e => ({error: String(e)})))
"""

_OPEN_SCRIPT_JS = """
/*tvmcp:pine_open_script*/
(async () => {
  const p = __PAYLOAD__;
  const m = __FIND__;
  if (!m) return {no_editor: true};
  const r = await fetch('https://pine-facade.tradingview.com/pine-facade/list/?filter=saved', {credentials: 'include'});
  const list = await r.json();
  if (!Array.isArray(list)) return {error: 'pine-facade returned a non-list (not logged in?)'};
  const q = p.name.toLowerCase();
  const nm = (s) => [(s.scriptName || ''), (s.scriptTitle || ''), (s.scriptIdPart || '')].map(x => String(x).toLowerCase());
  let match = list.find(s => nm(s).includes(q)) || list.find(s => nm(s).some(x => x.includes(q)));
  if (!match) return {not_found: true, names: list.map(s => s.scriptName || s.scriptTitle).slice(0, 50)};
  const id = match.scriptIdPart, ver = match.version || 1;
  const r2 = await fetch('https://pine-facade.tradingview.com/pine-facade/get/' + id + '/' + ver, {credentials: 'include'});
  const data = await r2.json();
  const src = data && data.source ? data.source : '';
  if (!src) return {error: 'script source is empty', name: match.scriptName || match.scriptTitle};
  m.editor.setValue(src);
  return {name: match.scriptName || match.scriptTitle, id: id, version: ver,
          lines: src.split('\\n').length, chars: src.length};
})()
"""


def _js(template: str, payload: dict | None = None) -> str:
    js = template.replace("__MARKERS__", _MARKERS_SNIPPET).replace("__FIND__", _FIND_MONACO)
    if payload is not None:
        js = js.replace("__PAYLOAD__", json.dumps(payload))
    return js


def _check(res) -> dict:
    if not res or res.get("no_editor"):
        raise ToolError(_NO_EDITOR)
    if res.get("error"):
        raise ToolError(f"Pine Editor: {res['error']}")
    return res


def ensure_open(page) -> dict:
    res = page.eval(_js(_OPEN_JS), await_promise=True)
    if not res or not res.get("ready"):
        raise ToolError(_NO_EDITOR)
    return res


def get_source(page) -> dict:
    ensure_open(page)
    return _check(page.eval(_js(_GET_JS)))


def set_source(page, source: str, overwrite_saved: bool = False) -> dict:
    ensure_open(page)
    res = _check(page.eval(_js(_SET_JS, {"source": source, "overwrite_saved": overwrite_saved})))
    if res.get("saved_script_open"):
        raise ToolError(
            "The Pine Editor currently holds one of the user's SAVED scripts; "
            "replacing its text is auto-saved to their account as a new "
            "version. Ask the user, then pass overwrite_saved=true - or have "
            "them open a new blank script first (Pine Editor > Open > New)."
        )
    if not res.get("applied"):
        raise ToolError("Monaco accepted setValue but the editor text differs from the input")
    return res


def compile_on_chart(page, settle_s: float = 2.5) -> dict:
    ensure_open(page)
    click = _check(page.eval(_js(_COMPILE_CLICK_JS)))
    if not click.get("clicked"):
        page.press("Enter", modifiers=CTRL)  # Pine Editor shortcut: add/update on chart
        click["clicked"] = "Ctrl+Enter"
    time.sleep(settle_s)
    res = _check(page.eval(_js(_COMPILE_RESULT_JS)))
    errors = [m for m in res["markers"] if m["severity"] == "error"]
    before, after = click.get("studies_before"), res.get("studies_after")
    return {
        "clicked": click["clicked"],
        "ok": not errors,
        "errors": errors,
        "warnings": [m for m in res["markers"] if m["severity"] != "error"],
        "studies_before": before,
        "studies_after": after,
        "study_added": (after > before) if (before is not None and after is not None) else None,
    }


def save(page) -> dict:
    ensure_open(page)
    page.press("s", modifiers=CTRL)
    time.sleep(0.8)
    dlg = page.eval(_js(_SAVE_DIALOG_JS)) or {}
    if dlg.get("dialog") and not dlg.get("clicked"):
        raise ToolError(
            "A save dialog opened but its Save button was not found - the "
            "script is probably new and needs a name; ask the user to save it "
            "by hand once, later saves are silent."
        )
    if dlg.get("clicked"):
        time.sleep(0.5)
    return {"saved": True, "via": "dialog" if dlg.get("dialog") else "Ctrl+S"}


def list_scripts(page) -> dict:
    res = page.eval(_LIST_JS, await_promise=True)
    if not res:
        raise ToolError("pine-facade list returned nothing (is the app logged in?)")
    if res.get("error"):
        raise ToolError(f"Pine Editor: {res['error']}")
    return res


def open_script(page, name: str) -> dict:
    ensure_open(page)
    res = _check(page.eval(_js(_OPEN_SCRIPT_JS, {"name": name}), await_promise=True))
    if res.get("not_found"):
        raise ToolError(
            f"No saved script matches {name!r}. Saved scripts: "
            f"{', '.join(res.get('names') or []) or 'none'}"
        )
    return res
