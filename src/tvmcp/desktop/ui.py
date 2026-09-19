"""Generic UI helpers for the live TradingView Desktop page (M8 desktop-3).

The chart API covers most needs, but dialogs, toolbars and the Pine panel are
plain React DOM. These helpers give the agent a bounded way to look at and
click that DOM without writing selectors into every tool:

- `find_elements`: visible elements matching a query by `data-name`,
  `aria-label` substring, button text/title, or a raw CSS selector
  (try/catch - an invalid selector is simply no match).
- `click_element`: click only when exactly one element matches; zero or many
  matches come back as data (`miss` / `ambiguous` + candidates), never as an
  exception, so the caller can refine the query.
- `dismiss_dialogs`: close every visible dialog (including TradingView's
  role-less confirm modals) by clicking its
  Cancel/close button. Landmine: TradingView ignores a synthetic Escape
  keydown (verified on the replay date picker, M7) - clicking is the only
  reliable dismissal.
- `fill_input_native_js`: React-controlled inputs ignore a plain `.value`
  assignment; go through the native prototype setter and fire input/change.

Queries travel as a JSON payload (never interpolated into JS), are capped at
200 chars, and every text read back from the DOM is untrusted display data.
"""

from __future__ import annotations

import json

from fastmcp.exceptions import ToolError

MAX_QUERY_CHARS = 200

# Shared DOM helpers: visibility test, label extraction, matching, summarising.
_UI_HELPERS_JS = """
  const visible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const txt = (el) => String(el.textContent || '').replace(/\\s+/g, ' ').trim();
  const attr = (el, n) => el.getAttribute(n) || '';
  const summarise = (el) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      data_name: attr(el, 'data-name') || null,
      aria_label: attr(el, 'aria-label').slice(0, 60) || null,
      text: (txt(el) || attr(el, 'title')).slice(0, 60) || null,
      role: attr(el, 'role') || null,
      rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
      in_dialog: !!el.closest('[role="dialog"], [data-name="confirm-dialog"], [data-name="rename-dialog"], [data-qa-id="ui-lib-PopupDialog"]'),
    };
  };
  const isButton = (el) => el.tagName === 'BUTTON' || attr(el, 'role') === 'button';
  const find = (query, limit) => {
    const q = String(query || '').slice(0, 200);
    const ql = q.toLowerCase();
    const seen = new Set(), out = [];
    const add = (el) => {
      if (!el || seen.has(el) || !visible(el)) return;
      seen.add(el); out.push(el);
    };
    for (const el of document.querySelectorAll('[data-name]'))
      if (attr(el, 'data-name') === q) add(el);
    for (const el of document.querySelectorAll('[aria-label]'))
      if (attr(el, 'aria-label').toLowerCase().includes(ql)) add(el);
    for (const el of document.querySelectorAll('button, [role="button"]'))
      if (txt(el).toLowerCase().includes(ql) || attr(el, 'title').toLowerCase().includes(ql)) add(el);
    try { for (const el of document.querySelectorAll(q)) add(el); } catch (e) {}
    return out.slice(0, limit);
  };
  const clickables = (limit) => {
    const out = [];
    for (const el of document.querySelectorAll('button, [role="button"]')) {
      if (!visible(el)) continue;
      const s = summarise(el);
      if (s.text || s.aria_label || s.data_name) out.push(s);
      if (out.length >= limit) break;
    }
    return out;
  };
"""

_FIND_JS = """
/*tvmcp:ui_find*/
(() => {
  const p = __PAYLOAD__;
  __HELPERS__
  return {query: p.query, matches: find(p.query, p.limit).map(summarise)};
})()
"""

_CLICK_JS = """
/*tvmcp:ui_click*/
(() => {
  const p = __PAYLOAD__;
  __HELPERS__
  const m = find(p.target, 20);
  if (m.length === 0)
    return {clicked: false, miss: true, candidates: clickables(20)};
  if (m.length > 1)
    return {clicked: false, ambiguous: true, candidates: m.map(summarise)};
  const el = m[0];
  const before = summarise(el);
  try { el.scrollIntoView({block: 'nearest', inline: 'nearest'}); } catch (e) {}
  el.click();
  return {clicked: true, matched: before};
})()
"""

_DIALOGS_JS = """
/*tvmcp:dialogs*/
(new Promise(async (resolve) => {
  __HELPERS__
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const title = (d) => {
    const t = d.querySelector('[data-name="dialog-title"], h1, h2, h3, [class*="title"]');
    return ((t && txt(t)) || attr(d, 'aria-label') || txt(d)).slice(0, 80) || null;
  };
  const label = (b) => [txt(b), attr(b, 'title'), attr(b, 'aria-label')].join(' | ');
  // TradingView's confirm modals ("Save the script before adding to chart?")
  // carry NO role="dialog" - they are [data-name="confirm-dialog"] /
  // [data-qa-id="ui-lib-PopupDialog"] with yes-btn / no-btn inside. Verified
  // live 2026-09-19; missing them left an invisible modal blocking the build.
  const dialogs = () => Array.from(document.querySelectorAll('[role="dialog"], [data-name="confirm-dialog"], [data-name="rename-dialog"], [data-qa-id="ui-lib-PopupDialog"]')).filter(visible);
  const dismissed = [];
  for (const d of dialogs()) {
    const t = title(d);
    const btns = Array.from(d.querySelectorAll('button')).filter(visible);
    // TV ignores a synthetic Esc key - a button click is the only reliable way out.
    let btn = btns.find(b => /^(cancel|отмена)$/i.test(txt(b))), via = 'cancel';
    if (!btn) {
      btn = d.querySelector('[data-name="close"]')
        || d.querySelector('button[aria-label="Close"]')
        || d.querySelector('button[aria-label="Закрыть"]');
      via = 'close';
    }
    if (!btn) continue;
    try { btn.click(); dismissed.push({title: t, via: via}); } catch (e) {}
  }
  if (dismissed.length) await sleep(300);
  const remaining = dialogs().map(d => ({
    title: title(d),
    buttons: Array.from(d.querySelectorAll('button')).filter(visible)
      .map(b => label(b).replace(/\\s*\\|\\s*/g, ' | ').trim().slice(0, 40)).slice(0, 10),
  }));
  resolve({dismissed: dismissed, remaining: remaining});
}))
"""

_FILL_JS = """
/*tvmcp:fill*/
(() => {
  const el = __SELECTOR__;
  if (!el) return {found: false};
  const proto = (el instanceof HTMLTextAreaElement) ? HTMLTextAreaElement.prototype
              : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  // React tracks the value through the native setter; a plain `el.value = x`
  // is swallowed by its value tracker and the change never reaches state.
  if (desc && desc.set) desc.set.call(el, __VALUE__); else el.value = __VALUE__;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {found: true, value: String(el.value).slice(0, 200)};
})()
"""


def _js(template: str, payload: dict | None = None) -> str:
    js = template.replace("__HELPERS__", _UI_HELPERS_JS)
    if payload is not None:
        js = js.replace("__PAYLOAD__", json.dumps(payload))
    return js


def _check_query(query: str, what: str = "query") -> str:
    q = (query or "").strip()
    if not q:
        raise ToolError(f"{what} must be a non-empty string")
    if len(q) > MAX_QUERY_CHARS:
        raise ToolError(f"{what} is longer than {MAX_QUERY_CHARS} chars; be specific, not long")
    return q


def fill_input_native_js(selector_expr: str, value: str) -> str:
    """JS that sets `value` on the element `selector_expr` evaluates to (React-safe).

    `selector_expr` is a JS expression written by our own code (e.g.
    `document.querySelector('[data-name="x"] input')`), never user text;
    `value` is JSON-encoded, so it is data.
    """
    return _FILL_JS.replace("__SELECTOR__", selector_expr).replace("__VALUE__", json.dumps(value))


def fill_input(page, selector: str, value: str) -> dict:
    """Fill the first element matching a CSS `selector` via the native setter."""
    expr = f"document.querySelector({json.dumps(selector)})"
    res = page.eval(fill_input_native_js(expr, value)) or {}
    if not res.get("found"):
        raise ToolError(f"No element matches {selector!r} to fill")
    return res


def find_elements(page, query: str, limit: int = 20) -> dict:
    q = _check_query(query)
    limit = max(1, min(int(limit), 100))
    res = page.eval(_js(_FIND_JS, {"query": q, "limit": limit})) or {}
    return {"query": q, "matches": res.get("matches", [])}


def click_element(page, target: str) -> dict:
    t = _check_query(target, "target")
    res = page.eval(_js(_CLICK_JS, {"target": t}))
    if not res:
        raise ToolError("The page returned nothing for the click - retry after the chart renders")
    return {"target": t, **res}


def dismiss_dialogs(page) -> dict:
    res = page.eval(_js(_DIALOGS_JS), await_promise=True) or {}
    return {"dismissed": res.get("dismissed", []), "remaining": res.get("remaining", [])}
