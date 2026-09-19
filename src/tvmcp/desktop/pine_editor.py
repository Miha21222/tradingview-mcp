"""Drive the Pine Editor panel of the live TradingView Desktop app (M7 + M8).

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

M8 part B adds the pieces a build pipeline needs without ever mutating one of
the user's saved scripts by accident:
- `read_header`: script name + save-button state + declared title + whether
  the name is in the user's saved list (the "library").
- `ensure_own_copy` / `save_as`: a saved foreign script is copied through the
  editor menu ("Make a copy" -> rename dialog -> Save) and the copy is
  verified by BOTH the header name and the facade list before any text goes
  in; failing that, "Create new" yields an unsaved buffer. Never `setValue`
  while a foreign saved script is open (autosave landmine, M7).
- `find_exact` / `replace_exact`: exact-match edits through
  `model.pushEditOperations` (undoable), EOL-normalized, guarded by an
  occurrence count and an optional sha256 of the buffer.
- `get_errors`: markers only, no clicks; `compile_on_chart` now handles the
  "save before adding?" and rename dialogs and waits for markers to settle
  instead of sleeping a fixed 2.5 s.

Pre-check Pine with `tv_pine_compile` (server-side, no chart needed) before
pushing into the editor - it's faster and leaves the user's editor alone.
Every `Runtime.evaluate` here finishes well under the 15 s websocket timeout;
the longer waits are Python-side polling loops over short evaluates.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone

from fastmcp.exceptions import ToolError

from . import clock, pine_hints
from .driver import CTRL

_NO_EDITOR = (
    "Pine Editor is not reachable: the panel did not open, or the Monaco "
    "instance is not where this app build mounts it (React fiber walk failed). "
    "Open the Pine Editor tab by hand and retry; if it persists the pine-editor "
    "slice needs rework for this TradingView Desktop build."
)

_SAVED_OPEN = (
    "The Pine Editor currently holds one of the user's SAVED scripts; "
    "replacing its text is auto-saved to their account as a new "
    "version. Ask the user, then pass overwrite_saved=true - or have "
    "them open a new blank script first (Pine Editor > Open > New), or use "
    "tv_desktop_pine_save_as / tv_desktop_pine_build_and_backtest, which work "
    "on an agent-owned copy."
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

# Shared by find/replace: sha256 of the buffer via WebCrypto (async), EOL
# normalization, indexOf occurrence loop, offset -> {line, column}.
_TEXT_SNIPPET = """
  const sha256 = async (s) => {
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
    return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
  };
  const norm = (s, eol) => String(s).replace(/\\r\\n|\\n/g, eol);
  const occurrences = (text, needle) => {
    const out = [];
    if (!needle) return out;
    let i = text.indexOf(needle);
    while (i !== -1) { out.push(i); i = text.indexOf(needle, i + needle.length); }
    return out;
  };
  const posOf = (model, offsets) => offsets.map(o => {
    const q = model.getPositionAt(o); return {line: q.lineNumber, column: q.column};
  });
  const savedState = () => {
    const saveBtn = document.querySelector('[data-qa-id="pine-script-save-button"]')
      || document.querySelector('[class*="saveButton"]');
    return !!(saveBtn && /(^|[^a-z])saved[^a-z]/i.test(' ' + saveBtn.className + ' '));
  };
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
  const saveBtn = document.querySelector('[data-qa-id="pine-script-save-button"]')
    || document.querySelector('[class*="saveButton"]');
  const savedOpen = !!(saveBtn && /(^|[^a-z])saved[^a-z]/i.test(' ' + saveBtn.className + ' '));
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
  const order = [[/save and add to chart|сохранить и добавить на график|сохранить и добавить/i, 'Save and add to chart'],
                 [/\\badd to chart\\b|добавить на график/i, 'Add to chart'],
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
  // The confirm modal has no role="dialog" (verified live 2026-09-19): it is
  // [data-name="confirm-dialog"] / [data-qa-id="ui-lib-PopupDialog"], and its
  // affirmative button is [data-qa-id="yes-btn"] (localized text).
  const dlg = Array.from(document.querySelectorAll('[role="dialog"], [data-name="confirm-dialog"], [data-name="rename-dialog"], [data-qa-id="ui-lib-PopupDialog"]'))
    .filter(d => d.offsetParent !== null).pop();
  if (!dlg) return {dialog: false};
  const all = Array.from(dlg.querySelectorAll('button'));
  const btn = dlg.querySelector('[data-qa-id="yes-btn"], button[name="yes"]')
    || all.find(b => /^(save|сохранить)$/i.test((b.textContent || '').trim()));
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

# --- M8 part B ---------------------------------------------------------------

_HEADER_JS = """
/*tvmcp:pine_header*/
(() => {
  const m = __FIND__;
  const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
  // The title and the save button carry stable data-qa-ids (verified live on TV
  // Desktop 3.4.1, RU locale); class hashes and data-names change between builds,
  // so they are only fallbacks. Searching the document is safe: the Pine header
  // exists once, and the editor may be a floating dialog that shares no ancestor
  // with the chart pane.
  const nameEl = document.querySelector('[data-qa-id="pine-script-title-button"]')
    || document.querySelector('[class*="nameButton"]')
    || document.querySelector('[data-name="script-title"], [data-name="script-name"], '
      + '[class*="scriptTitle"], [class*="scriptName"]');
  const saveBtn = document.querySelector('[data-qa-id="pine-script-save-button"]')
    || document.querySelector('[class*="saveButton"], [data-name="save-script"]');
  const saved = !!(saveBtn && /(^|[^a-z])saved[^a-z]/i.test(' ' + saveBtn.className + ' '));
  let src = '';
  try { src = m ? m.editor.getValue().slice(0, 4000) : ''; } catch (e) {}
  const dm = src.match(/\\b(?:strategy|indicator|library)\\s*\\(\\s*(?:title\\s*=\\s*)?(["'])((?:\\\\.|(?!\\1).)*)\\1/);
  return {no_editor: !m,
          script_name: nameEl ? (clean(nameEl.textContent).slice(0, 120) || null) : null,
          saved: saved,
          save_button_class: saveBtn ? String(saveBtn.className).slice(0, 100) : null,
          declared_title: dm ? dm[2] : null};
})()
"""

_MENU_JS = """
/*tvmcp:pine_menu*/
(new Promise(async (resolve) => {
  const p = __PAYLOAD__;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const vis = (el) => !!el && el.offsetParent !== null;
  const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
  const label = (el) => clean([el.textContent, el.getAttribute('title'), el.getAttribute('aria-label')].join(' | '));
  // Live RU labels on TV Desktop 3.4.1: "Сохранить скрипт" (Ctrl + S),
  // "Копировать…", "Переименовать…", "История версий…", "Переместить скрипт
  // вниз", "Создать новый", "Открыть скрипт…" (Ctrl + O). The copy row reads
  // "Копировать", not "Создать копию" - a bare /новый/ also matched the wrong
  // rows, so both patterns are pinned to the real wording.
  const RE = {
    copy: /make a copy|make copy|создать копию|копировать|копию/i,
    new: /create new|new script|new blank|новый скрипт|создать новый/i,
    save: /save script|^save\\b|сохранить скрипт|^сохранить\\b/i,
  };
  const menuItems = () => Array.from(document.querySelectorAll(
    '[role="menuitem"], [role="menu"] [role="option"], [data-name="menu-inner"] [class*="item"], [class*="menuBox"] [class*="item"]'
  )).filter(vis);
  let items = menuItems(), opener = null;
  if (items.length < 2) {
    const panel = (() => {
      let el = document.querySelector('.monaco-editor.pine-editor-monaco');
      for (let i = 0; el && i < 12; i++) {
        if (el.querySelector && el.querySelector('[data-qa-id="pine-script-save-button"], [class*="saveButton"], [data-name="save-script"]')) return el;
        el = el.parentElement;
      }
      return document;
    })();
    const cands = [];
    // The script-name control is a DIV (not a button) carrying a stable
    // data-qa-id - verified live; clicking it opens this menu. It may sit
    // outside the panel when the editor is a floating dialog, so look
    // document-wide first.
    for (const sel of ['[data-qa-id="pine-script-title-button"]', '[class*="nameButton"]']) {
      const e = document.querySelector(sel); if (vis(e)) cands.push(e);
    }
    for (const sel of ['[data-name="script-name-menu"]', '[data-name="pine-editor-menu"]', '[data-name="script-title"]', '[data-name="open-script"]']) {
      const e = panel.querySelector(sel); if (vis(e)) cands.push(e);
    }
    const btns = Array.from(panel.querySelectorAll('button, [role="button"]')).filter(vis);
    if (p.script_name) { const b = btns.find(x => clean(x.textContent) === p.script_name); if (b) cands.push(b); }
    const re = /^more|menu|ещё|еще|меню|^open|открыть|script name|название/i;
    for (const b of btns) if (re.test(label(b)) && !cands.includes(b)) cands.push(b);
    for (const c of cands) {
      try { c.click(); } catch (e) { continue; }
      for (let i = 0; i < 6; i++) { await sleep(150); items = menuItems(); if (items.length >= 2) break; }
      if (items.length >= 2) { opener = label(c).slice(0, 60); break; }
    }
    if (items.length < 2) return resolve({menu_opened: false, clicked: null, items: [], opener: null});
  }
  const labels = items.map(i => label(i).slice(0, 60));
  let target = items.find(i => RE[p.item].test(label(i)));
  if (!target) {
    // Fallback by position: "Make a copy" sits next to the shortcut-labelled
    // Save (Ctrl + S) / Open (Ctrl + O) rows; "Create new" is the first row.
    const iS = items.findIndex(i => /ctrl\\s*\\+\\s*s\\b/i.test(label(i)));
    const iO = items.findIndex(i => /ctrl\\s*\\+\\s*o\\b/i.test(label(i)));
    if (p.item === 'save' && iS >= 0) target = items[iS];
    else if (p.item === 'copy') target = (iS >= 0 && items[iS + 1]) || null;
    else if (p.item === 'new') target = (iO >= 0 && items[iO - 1]) || null;
  }
  if (!target) return resolve({menu_opened: true, clicked: null, items: labels, opener: opener});
  const chosen = label(target).slice(0, 60);
  try { target.click(); } catch (e) { return resolve({menu_opened: true, clicked: null, items: labels, opener: opener, error: String(e).slice(0, 120)}); }
  resolve({menu_opened: true, clicked: chosen, items: labels, opener: opener});
}))
"""

_DIALOG_JS = """
/*tvmcp:pine_dialog*/
(() => {
  const p = __PAYLOAD__;
  const vis = (el) => !!el && el.offsetParent !== null;
  const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
  const dlgs = Array.from(document.querySelectorAll('[data-name="rename-dialog"], [data-name="confirm-dialog"], [data-qa-id="ui-lib-PopupDialog"], [role="dialog"]')).filter(vis);
  if (!dlgs.length) return {dialog: false};
  const d = dlgs[dlgs.length - 1];
  const tEl = d.querySelector('[data-name="dialog-title"], h1, h2, h3, [class*="title"]');
  const title = clean((tEl && tEl.textContent) || d.getAttribute('aria-label') || '').slice(0, 120);
  const btns = Array.from(d.querySelectorAll('button')).filter(vis);
  const bl = btns.map(b => clean([b.textContent, b.getAttribute('title'), b.getAttribute('aria-label')].join(' | ')).slice(0, 40));
  const saveBtn = () => {
    // Confirm modals answer with [data-qa-id="yes-btn"] / button[name="yes"];
    // the rename dialog uses save-btn. Text matching is the last resort - the
    // wording is localized.
    const y = d.querySelector('button[data-qa-id="yes-btn"], button[name="yes"]');
    if (y) return [y, 'yes-btn'];
    const q = d.querySelector('button[data-qa-id="save-btn"]');
    if (q) return [q, 'save-btn'];
    const s = d.querySelector('button[type="submit"], [type="submit"]');
    if (s) return [s, 'submit'];
    const t = btns.find(b => /^(save|сохранить|ok|ок)$/i.test(clean(b.textContent)));
    return t ? [t, 'text'] : [null, null];
  };
  const input = Array.from(d.querySelectorAll('input[type="text"], input:not([type]), textarea')).find(vis);
  if (input) {
    if (p.name == null) return {dialog: true, kind: 'rename', title: title, filled: false, clicked: false, buttons: bl};
    const proto = (input instanceof HTMLTextAreaElement) ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(input, p.name); else input.value = p.name;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
    const [b, via] = saveBtn();
    if (!b) return {dialog: true, kind: 'rename', title: title, filled: true, clicked: false, buttons: bl};
    b.click();
    return {dialog: true, kind: 'rename', title: title, filled: true, clicked: true, via: via};
  }
  const body = clean(d.textContent).slice(0, 300);
  if (/save this script before adding|save (the )?script before|сохранить скрипт перед добавлением|перед добавлением/i.test(title + ' ' + body)) {
    const [b] = saveBtn();
    if (!b) return {dialog: true, kind: 'save_prompt', title: title, clicked: false, buttons: bl};
    b.click();
    return {dialog: true, kind: 'save_prompt', title: title, clicked: true};
  }
  return {dialog: true, kind: 'other', title: title, clicked: false, buttons: bl};
})()
"""

_FIND_JS = """
/*tvmcp:pine_find*/
(async () => {
  const m = __FIND__;
  if (!m) return {no_editor: true};
  const p = __PAYLOAD__;
  __TEXT__
  const model = m.editor.getModel();
  if (!model) return {no_editor: true};
  const eol = model.getEOL();
  const text = model.getValue();
  const needle = norm(p.needle, eol);
  const offs = occurrences(text, needle);
  return {eol: eol === '\\r\\n' ? 'CRLF' : 'LF', occurrences: offs.length,
          positions: posOf(model, offs).slice(0, 50), sha256: await sha256(text),
          chars: text.length, saved_script_open: savedState()};
})()
"""

_REPLACE_JS = """
/*tvmcp:pine_replace*/
(async () => {
  const m = __FIND__;
  if (!m) return {no_editor: true};
  const p = __PAYLOAD__;
  __TEXT__
  const model = m.editor.getModel();
  if (!model) return {no_editor: true};
  const savedOpen = savedState();
  // Same autosave landmine as pine_set: a saved script's text change is
  // written to the user's account within seconds.
  if (savedOpen && !p.overwrite_saved && !p.dry_run) return {refused_saved: true};
  const eol = model.getEOL(), eolName = eol === '\\r\\n' ? 'CRLF' : 'LF';
  const before = model.getValue();
  const needle = norm(p.needle, eol), replacement = norm(p.replacement, eol);
  const offs = occurrences(before, needle);
  const shaBefore = await sha256(before);
  const base = {eol: eolName, occurrences_before: offs.length, sha256_before: shaBefore,
                positions: posOf(model, offs).slice(0, 50), saved_script_open: savedOpen};
  if (p.expect_sha && p.expect_sha !== shaBefore)
    return {...base, applied: false, reason: 'sha_mismatch', occurrences_after: offs.length, sha256_after: shaBefore};
  if (offs.length !== p.expected)
    return {...base, applied: false, reason: 'occurrence_mismatch', occurrences_after: offs.length, sha256_after: shaBefore};
  if (p.dry_run)
    return {...base, applied: false, reason: 'dry_run', occurrences_after: offs.length, sha256_after: shaBefore};
  // Back-to-front so earlier offsets stay valid; pushEditOperations keeps undo.
  const edits = offs.slice().reverse().map(o => {
    const s = model.getPositionAt(o), e = model.getPositionAt(o + needle.length);
    return {range: {startLineNumber: s.lineNumber, startColumn: s.column, endLineNumber: e.lineNumber, endColumn: e.column},
            text: replacement, forceMoveMarkers: true};
  });
  model.pushEditOperations([], edits, () => null);
  const after = model.getValue();
  let verified = true, shift = 0;
  for (const o of offs) {
    const s = model.getPositionAt(o + shift), e = model.getPositionAt(o + shift + replacement.length);
    const got = model.getValueInRange({startLineNumber: s.lineNumber, startColumn: s.column, endLineNumber: e.lineNumber, endColumn: e.column});
    if (got !== replacement) verified = false;
    shift += replacement.length - needle.length;
  }
  const remain = occurrences(after, needle).length;
  const countCheck = replacement.includes(needle) ? null : (remain === offs.length - p.expected);
  if (countCheck === false) verified = false;
  return {...base, applied: true, verified: verified, occurrences_after: remain,
          sha256_after: await sha256(after), count_check: countCheck};
})()
"""

_ERRORS_JS = """
/*tvmcp:pine_errors*/
(() => {
  const m = __FIND__;
  if (!m) return {no_editor: true};
  __MARKERS__
  let head = '';
  try { head = m.editor.getValue().slice(0, 400); } catch (e) {}
  return {markers: markers || [], source_head: head};
})()
"""


def _js(template: str, payload: dict | None = None) -> str:
    js = (template.replace("__MARKERS__", _MARKERS_SNIPPET)
          .replace("__TEXT__", _TEXT_SNIPPET)
          .replace("__FIND__", _FIND_MONACO))
    if payload is not None:
        js = js.replace("__PAYLOAD__", json.dumps(payload))
    return js


def _check(res) -> dict:
    if not res or res.get("no_editor"):
        raise ToolError(_NO_EDITOR)
    if res.get("error"):
        raise ToolError(f"Pine Editor: {res['error']}")
    return res


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_open(page) -> dict:
    res = page.eval(_js(_OPEN_JS), await_promise=True)
    if not res or not res.get("ready"):
        raise ToolError(_NO_EDITOR)
    return res


def get_source(page) -> dict:
    ensure_open(page)
    res = _check(page.eval(_js(_GET_JS)))
    res["sha256"] = _sha256(res.get("source") or "")
    return res


def set_source(page, source: str, overwrite_saved: bool = False) -> dict:
    ensure_open(page)
    res = _check(page.eval(_js(_SET_JS, {"source": source, "overwrite_saved": overwrite_saved})))
    if res.get("saved_script_open"):
        raise ToolError(_SAVED_OPEN)
    if not res.get("applied"):
        raise ToolError("Monaco accepted setValue but the editor text differs from the input")
    return res


# --- header / own copy ---------------------------------------------------------

def _facade_scripts(page) -> list[dict] | None:
    """The user's saved scripts, or None when the facade is unreachable."""
    try:
        return list_scripts(page).get("scripts") or []
    except ToolError:
        return None


def _in_library(scripts, name: str | None) -> bool | None:
    if scripts is None or not name:
        return None
    return any((s.get("name") == name) or (s.get("title") == name) for s in scripts)


def read_header(page) -> dict:
    """{script_name, saved, declared_title, is_library_script} of the open script.

    `saved` mirrors the save button's "saved" state (the autosave guard);
    `is_library_script` = the header name is in the user's saved list (None
    when the facade list could not be read).
    """
    ensure_open(page)
    res = _check(page.eval(_js(_HEADER_JS)))
    name = res.get("script_name")
    return {
        "script_name": name,
        "saved": bool(res.get("saved")),
        "declared_title": res.get("declared_title"),
        "is_library_script": _in_library(_facade_scripts(page), name),
    }


def _menu(page, item: str, script_name: str | None = None) -> dict:
    res = page.eval(_js(_MENU_JS, {"item": item, "script_name": script_name}), await_promise=True)
    return res or {"menu_opened": False, "clicked": None, "items": []}


def _dialog(page, name: str | None = None) -> dict:
    return page.eval(_js(_DIALOG_JS, {"name": name})) or {"dialog": False}


def _wait_dialog(page, name: str | None, timeout_s: float) -> dict:
    deadline = clock.now() + timeout_s
    while True:
        r = _dialog(page, name)
        if r.get("dialog") or clock.now() >= deadline:
            return r
        clock.sleep(0.25)


def _verify_named(page, name: str, timeout_s: float = 5.0) -> tuple[bool, dict]:
    """Poll (<= timeout_s, one short evaluate per read) until header == name
    AND the facade list contains name."""
    deadline = clock.now() + timeout_s
    while True:
        header = read_header(page)
        if header["script_name"] == name and header["is_library_script"]:
            return True, header
        if clock.now() >= deadline:
            return False, header
        clock.sleep(0.5)


def _copy_flow(page, name: str, header: dict) -> dict:
    """Editor menu "Make a copy" -> rename dialog (native fill) -> Save -> verify."""
    menu = _menu(page, "copy", header.get("script_name"))
    if not menu.get("clicked"):
        return {"ok": False, "menu": menu, "dialog": None}
    dlg = _wait_dialog(page, name, timeout_s=3.0)
    if not dlg.get("clicked"):
        return {"ok": False, "menu": menu, "dialog": dlg}
    ok, hdr = _verify_named(page, name)
    return {"ok": ok, "menu": menu, "dialog": dlg, "header": hdr}


def ensure_own_copy(page, name: str) -> dict:
    """Make sure the editor holds a buffer the agent may overwrite.

    unsaved/blank -> 'unsaved'; saved and named `name` -> 'own'; saved and
    foreign -> 'copied' (verified by header AND facade list) or, when the copy
    flow fails, 'created' (menu "Create new" -> unsaved buffer). Raises only
    when no safe buffer could be obtained - never setValue on a foreign saved
    script.
    """
    ensure_open(page)
    header = read_header(page)
    if not header["saved"]:
        return {"mode": "unsaved", "verified": True, "header": header}
    if header["script_name"] == name:
        return {"mode": "own", "verified": True, "header": header}
    copy = _copy_flow(page, name, header)
    if copy["ok"]:
        return {"mode": "copied", "verified": True, "header": copy["header"],
                "detail": {"menu": copy["menu"], "dialog": copy["dialog"]}}
    menu = _menu(page, "new", header.get("script_name"))
    clock.sleep(1.2)
    header2 = read_header(page)
    if header2["saved"]:
        raise ToolError(
            f"Could not obtain an agent-owned buffer: the editor holds the saved script "
            f"{header['script_name']!r}, 'Make a copy' did not produce {name!r} "
            f"(menu: {copy['menu'].get('items') or 'not opened'}), and 'Create new' left a "
            "saved script open. Ask the user to open a new blank script (Pine Editor > "
            "Open > New) and retry."
        )
    return {"mode": "created", "verified": not header2["saved"], "header": header2,
            "detail": {"copy": {"menu": copy["menu"], "dialog": copy["dialog"]}, "new_menu": menu}}


# --- exact edits ---------------------------------------------------------------

def _check_expect_script(page, expect_script: str | None) -> None:
    if expect_script is None:
        return
    hdr = read_header(page)
    have = {hdr.get("script_name"), hdr.get("declared_title")} - {None}
    if expect_script.strip() not in have:
        raise ToolError(
            f"expect_script={expect_script!r} but the editor shows "
            f"script {hdr.get('script_name')!r} (declared title {hdr.get('declared_title')!r}). "
            "Nothing changed."
        )


def find_exact(page, needle: str, expect_script: str | None = None) -> dict:
    """Count exact occurrences of `needle` (EOL-normalized) and sha256 the buffer."""
    if not needle:
        raise ToolError("needle must be a non-empty string")
    ensure_open(page)
    _check_expect_script(page, expect_script)
    res = _check(page.eval(_js(_FIND_JS, {"needle": needle}), await_promise=True))
    return {"needle_chars": len(needle), **res}


def replace_exact(page, needle: str, replacement: str, expected_occurrences: int = 1,
                  expect_script: str | None = None, expect_source_sha256: str | None = None,
                  dry_run: bool = False, overwrite_saved: bool = False) -> dict:
    """Replace every exact occurrence of `needle` iff the count equals
    `expected_occurrences` (and the buffer sha matches when given); undoable."""
    if not needle:
        raise ToolError("needle must be a non-empty string")
    if expected_occurrences < 1:
        raise ToolError("expected_occurrences must be >= 1")
    ensure_open(page)
    _check_expect_script(page, expect_script)
    payload = {"needle": needle, "replacement": replacement, "expected": int(expected_occurrences),
               "expect_sha": expect_source_sha256, "dry_run": bool(dry_run),
               "overwrite_saved": bool(overwrite_saved)}
    res = _check(page.eval(_js(_REPLACE_JS, payload), await_promise=True))
    if res.get("refused_saved"):
        raise ToolError(_SAVED_OPEN)
    out = {k: res.get(k) for k in ("applied", "occurrences_before", "occurrences_after",
                                   "sha256_before", "sha256_after", "eol", "positions")}
    out["reason"] = res.get("reason")
    out["verified"] = res.get("verified") if res.get("applied") else None
    out["saved_script_open"] = res.get("saved_script_open")
    if res.get("reason") == "occurrence_mismatch":
        out["note"] = (f"needle occurs {res.get('occurrences_before')} time(s), expected "
                       f"{expected_occurrences} - nothing changed; adjust the needle or "
                       "expected_occurrences")
    elif res.get("reason") == "sha_mismatch":
        out["note"] = ("buffer sha256 differs from expect_source_sha256 - the editor text "
                       "changed since you read it; re-read with tv_desktop_pine_get_source")
    elif res.get("applied") and not res.get("verified"):
        out["note"] = "edit applied but read-back differs - inspect with tv_desktop_pine_get_source"
    return out


# --- compile / errors ---------------------------------------------------------

def study_count(page) -> int | None:
    res = _check(page.eval(_js(_COMPILE_RESULT_JS)))
    return res.get("studies_after")


def settle_markers(page, max_s: float = 6.0, interval_s: float = 0.3) -> dict:
    """Poll markers until unchanged for 2 polls (max `max_s`); returns the last read."""
    deadline = clock.now() + max_s
    last, stable, polls = None, 0, 0
    while True:
        res = _check(page.eval(_js(_COMPILE_RESULT_JS)))
        polls += 1
        sig = json.dumps(res.get("markers") or [], sort_keys=True)
        if sig == last:
            stable += 1
        else:
            stable, last = 0, sig
        if stable >= 2 or clock.now() >= deadline:
            res["settle_polls"] = polls
            res["settled"] = stable >= 2
            return res
        clock.sleep(interval_s)


def _handle_compile_dialogs(page, save_name: str | None, timeout_s: float = 6.0) -> list[dict]:
    """After the compile click: "Save this script before adding?" -> Save; a
    rename input -> fill `save_name` -> Save. Polls <= timeout_s, stops after
    three quiet polls."""
    handled: list[dict] = []
    start = clock.now()
    deadline = start + timeout_s
    quiet = 0
    while clock.now() < deadline:
        r = _dialog(page, save_name)
        if r.get("dialog"):
            handled.append({k: r.get(k) for k in ("kind", "title", "clicked", "filled", "via", "buttons")
                            if k in r})
            quiet = 0
            if not r.get("clicked"):
                break  # a dialog we cannot answer - report it, do not spin
        else:
            quiet += 1
            # The "save before adding?" modal can take a second to mount; only
            # give up after ~1.5 s of quiet, and never before 1.5 s elapsed.
            if quiet >= 5 and clock.now() - start >= 1.5:
                break
        clock.sleep(0.3)
    return handled


def compile_on_chart(page, save_name: str | None = None, settle_s: float = 6.0) -> dict:
    ensure_open(page)
    click = _check(page.eval(_js(_COMPILE_CLICK_JS)))
    if not click.get("clicked"):
        page.press("Enter", modifiers=CTRL)  # Pine Editor shortcut: add/update on chart
        click["clicked"] = "Ctrl+Enter"
    dialogs = _handle_compile_dialogs(page, save_name)
    res = settle_markers(page, max_s=settle_s)
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
        "dialogs_handled": dialogs,
        "settled": res.get("settled"),
    }


def get_errors(page) -> dict:
    """Monaco markers only (no clicks): {ok, errors, warnings, hints}."""
    ensure_open(page)
    res = _check(page.eval(_js(_ERRORS_JS)))
    errors = [m for m in res.get("markers") or [] if m.get("severity") == "error"]
    warnings = [m for m in res.get("markers") or [] if m.get("severity") != "error"]
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "hints": pine_hints.hints_for(errors, warnings, res.get("source_head") or "")}


# --- save ----------------------------------------------------------------------

def _modified_ts(value) -> float | None:
    """pine-facade `modified` -> unix seconds, None when unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e12 else v
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _facade_fresh(page, name: str | None, since_ts: float, timeout_s: float = 3.0) -> bool:
    """Poll the saved list for `name` with modified >= since_ts (unparseable = fresh)."""
    if not name:
        return False
    deadline = clock.now() + timeout_s
    while True:
        for s in _facade_scripts(page) or []:
            if s.get("name") == name or s.get("title") == name:
                m = _modified_ts(s.get("modified"))
                if m is None or m >= since_ts - 5:
                    return True
        if clock.now() >= deadline:
            return False
        clock.sleep(0.5)


def save(page) -> dict:
    ensure_open(page)
    t0 = time.time()
    page.press("s", modifiers=CTRL)
    clock.sleep(0.8)
    dlg = page.eval(_js(_SAVE_DIALOG_JS)) or {}
    if dlg.get("dialog") and not dlg.get("clicked"):
        raise ToolError(
            "A save dialog opened but its Save button was not found - the "
            "script is probably new and needs a name; ask the user to save it "
            "by hand once, later saves are silent (or use tv_desktop_pine_save_as)."
        )
    if dlg.get("clicked"):
        clock.sleep(0.5)
    header = read_header(page)
    verified = _facade_fresh(page, header.get("script_name"), t0)
    return {"saved": True, "via": "dialog" if dlg.get("dialog") else "Ctrl+S",
            "verified": verified, "script_name": header.get("script_name")}


def save_as(page, name: str) -> dict:
    """Save the editor buffer under `name` as an agent-owned script, verified.

    Saved foreign script -> copy flow; saved script already named `name` ->
    nothing to do; unsaved buffer -> Ctrl+S (menu "Save script" as fallback)
    -> rename dialog filled with `name` -> Save. Verification = header name
    AND facade list; ToolError otherwise.
    """
    if not name or not name.strip():
        raise ToolError("name must be a non-empty string")
    ensure_open(page)
    header = read_header(page)
    if header["saved"] and header["script_name"] == name:
        ok, hdr = _verify_named(page, name, timeout_s=1.0)
        return {"saved": True, "verified": ok, "via": "already", "header": hdr}
    if header["saved"]:
        copy = _copy_flow(page, name, header)
        if not copy["ok"]:
            raise ToolError(
                f"'Make a copy' of {header['script_name']!r} as {name!r} could not be verified "
                f"(menu: {copy['menu'].get('items') or 'not opened'}; dialog: "
                f"{(copy.get('dialog') or {}).get('kind')}). Nothing was overwritten; ask the user "
                "to make the copy by hand or open a new blank script."
            )
        return {"saved": True, "verified": True, "via": "copy", "header": copy["header"]}
    page.press("s", modifiers=CTRL)
    via = "Ctrl+S"
    dlg = _wait_dialog(page, name, timeout_s=3.0)
    if not dlg.get("dialog"):
        _menu(page, "save", header.get("script_name"))
        via = "menu"
        dlg = _wait_dialog(page, name, timeout_s=3.0)
    if not dlg.get("clicked"):
        raise ToolError(
            "The save dialog did not confirm (kind: "
            f"{dlg.get('kind') or 'none'}, buttons: {dlg.get('buttons') or []}). "
            "Ask the user to save the script by hand once."
        )
    ok, hdr = _verify_named(page, name)
    if not ok:
        raise ToolError(
            f"Saved, but neither the editor header nor the saved-scripts list shows {name!r} "
            f"(header: {hdr.get('script_name')!r}, in library: {hdr.get('is_library_script')}). "
            "Check tv_desktop_pine_list_scripts before retrying."
        )
    return {"saved": True, "verified": True, "via": via, "header": hdr}


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


def declares_strategy(source: str) -> bool:
    return re.search(r"^\s*strategy\s*\(", source or "", re.M) is not None
