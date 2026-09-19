"""Shared CDP-free fake of the desktop driver's page (no app, no network).

`FakePage` implements the `DesktopPage` interface the driver/pine_editor/replay/
strategy_tester/ui/workspace/pine_build modules rely on and dispatches on the
`/*tvmcp:<name>*/` tag of the JS each helper sends, returning canned results
shaped like the real in-page blocks. Imported by test_desktop.py,
test_desktop_ui.py and friends.
"""

import hashlib
import json
import re

from fastmcp.exceptions import ToolError


def _payload(expr):
    """The JSON object after `const p = ` (raw_decode: payload strings may hold `;`)."""
    return json.JSONDecoder().raw_decode(expr.split("const p = ", 1)[1])[0]


class FakeClock:
    """Monotonic clock that advances on sleep - patch `clock.now`/`clock.sleep` with it."""

    def __init__(self, start=1000.0):
        self.t = start
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


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
        # [{"id": ..., "title": ..., "plots": [...], "rows": [...], "boxes": [...],
        #   "strategy": bool}]
        self.studies = studies if studies is not None else []
        self.study_payloads = []
        self._next_id = 0
        # M7 fakes
        self.api = True                      # chart API exposed (nav via API)
        self.api_symbol_override = None      # what the API resolves a symbol to
        self.nav = []
        self.viewports = []
        self.strategy = {"strategies": [], "report": None}
        self.strategy_payloads = []
        self.replay_state = {"available": True, "started": False, "autoplay": False,
                             "mode": None, "current_date": None, "position": None,
                             "realized_pnl": 0}
        self.replay_calls = []
        self.editor = ""                     # None = editor unreachable
        self.markers = []
        self.compile_button = "Add to chart"
        self.scripts = [{"id": "abc", "name": "My SMC", "title": "My SMC",
                         "version": 3, "modified": None, "kind": "study"}]
        # M8 fakes
        self.visible = True
        self.raf = 6                         # rAF ticks the probe counts (0 = throttled)
        self.monaco_visible = False
        # [{tag, data_name, aria_label, text, role, rect, in_dialog}]
        self.elements = []
        self.clicks = []                     # elements clicked via ui_click
        # [{"title": ..., "buttons": [...]}] - a Cancel/Отмена/close button dismisses
        self.dialogs = []
        self.bars = []                       # rows [t, o, h, l, c, v], oldest first
        self.bar_payloads = []
        self.ui_payloads = []
        # M8 part B fakes
        self.window_state = "normal"         # "minimized" -> restore flips it to normal
        self.window_supported = True         # False -> Browser.* raise ToolError
        self.window_calls = []               # (method, params)
        self.editor_live = True              # Monaco has a model AND a laid-out node
        self.editor_width = 800
        self.editor_live_after_stage = None  # N -> live once stage N has been tried
        self.stages_tried = []
        self.floating_editor = False         # True -> the dock button exists
        self.script_name = "Untitled script"
        self.saved = False                   # save button shows the "saved" state
        self.copy_succeeds = True            # "Make a copy" menu item present
        self.menu_available = True           # the script menu opens at all
        self.menu_clicks = []
        self.pending_dialog = None           # 'rename' | 'save_prompt'
        self.dialog_calls = []
        self.marker_sequence = None          # successive marker lists for pine_result
        self.studies_after = None            # override the pine_result count
        self.strategy_after_n_polls = 0      # strategy branch is empty for N reads
        self.strategy_polls = 0
        self.save_prompt_on_compile = False  # compile click opens "save before adding?"
        self.replace_calls = []
        self.cleared = []

    # --- raw CDP calls (window restore) ---------------------------------------
    def call(self, method, params=None):
        self.window_calls.append((method, params or {}))
        if method.startswith("Browser.") and not self.window_supported:
            raise ToolError(f"CDP {method} failed: 'Browser.getWindowForTarget' wasn't found")
        if method == "Browser.getWindowForTarget":
            return {"windowId": 1, "bounds": {"windowState": self.window_state}}
        if method == "Browser.setWindowBounds":
            self.window_state = (params or {}).get("bounds", {}).get("windowState", self.window_state)
            return {}
        return {}

    # --- text helpers for the find/replace fakes -------------------------------
    def _eol(self):
        return "\r\n" if "\r\n" in (self.editor or "") else "\n"

    @staticmethod
    def _sha(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _positions(text, offsets):
        out = []
        for o in offsets:
            before = text[:o]
            out.append({"line": before.count("\n") + 1,
                        "column": o - (before.rfind("\n") + 1) + 1})
        return out

    def _norm(self, s):
        eol = self._eol()
        return re.sub(r"\r\n|\n", lambda _m: eol, s)

    def _occurrences(self, needle):
        text = self.editor or ""
        n = self._norm(needle)
        offs, i = [], text.find(n)
        while i != -1:
            offs.append(i)
            i = text.find(n, i + len(n))
        return text, n, offs

    def _declared_title(self):
        m = re.search(r"\b(?:strategy|indicator|library)\s*\(\s*(?:title\s*=\s*)?([\"'])(.*?)\1",
                      (self.editor or "")[:600])
        return m.group(2) if m else None

    def eval(self, expr, await_promise=False):
        self.exprs.append(expr)
        if "/*tvmcp:studies*/" in expr:
            return {
                "symbol": self.symbol,
                "resolution": "60",
                "studies": self.studies,
            }
        if "/*tvmcp:plots*/" in expr or "/*tvmcp:graphics*/" in expr:
            payload = _payload(expr)
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
        if "/*tvmcp:nav*/" in expr:
            payload = _payload(expr)
            self.nav.append(payload)
            if not self.api:
                return {"no_api": True}
            if payload.get("symbol"):
                self.symbol = self.api_symbol_override or payload["symbol"]
            if payload.get("resolution"):
                self.interval = payload["resolution"]
            return {"api_symbol": self.symbol, "api_resolution": self.interval,
                    "ready": True, "waited_ms": 600}
        if "/*tvmcp:resolution*/" in expr:
            return {"resolution": self.interval}
        if "/*tvmcp:viewport*/" in expr:
            payload = _payload(expr)
            self.viewports.append(payload)
            return {"requested": {"from": payload["from"], "to": payload["to"]},
                    "visible": {"from": payload["from"], "to": payload["to"]},
                    "method": "setVisibleRange", "pages_loaded": 2,
                    "earliest_loaded": payload["from"] - 10,
                    "history_exhausted": False, "clamped": False}
        if "/*tvmcp:inputs*/" in expr:
            payload = _payload(expr)
            self.study_payloads.append(payload)
            q = payload["query"]
            matches = [st for st in self.studies
                       if st["id"] == q or q.lower() in st["title"].lower()]
            if len(matches) != 1:
                return {"__miss": True, "not_found": not matches,
                        "candidates": [{"id": st["id"], "title": st["title"]}
                                       for st in self.studies]}
            st = matches[0]
            avail = st.get("inputs", {})
            unknown = [k for k in payload["inputs"] if k not in avail]
            if unknown:
                return {"id": st["id"], "title": st["title"], "unknown": unknown,
                        "available": [{"id": k, "name": k, "type": "integer", "value": v}
                                      for k, v in avail.items()]}
            before = {k: avail[k] for k in payload["inputs"]}
            for k, v in payload["inputs"].items():
                avail[k] = v if k != "stuck" else avail[k]
            after = {k: avail[k] for k in payload["inputs"]}
            mism = [k for k in payload["inputs"] if str(after[k]) != str(payload["inputs"][k])]
            return {"id": st["id"], "title": st["title"], "before": before,
                    "after": after, "applied": not mism, "mismatched": mism}
        if "/*tvmcp:strategy*/" in expr:
            payload = _payload(expr)
            self.strategy_payloads.append(payload)
            self.strategy_polls += 1
            if self.strategy_polls <= self.strategy_after_n_polls:
                return {"strategies": [], "report": None}
            return self.strategy
        if "/*tvmcp:replay*/" in expr:
            payload = _payload(expr)
            self.replay_calls.append(payload)
            a = payload["action"]
            if a == "start":
                if payload.get("time_ms") == 1000:
                    return {"error": "Replay did not start - the date may have no data"}
                self.replay_state.update(started=True, current_date=payload.get("time_ms") or 1000)
                return {"ok": True, "action": "started", **self.replay_state}
            if a == "step":
                if not self.replay_state["started"]:
                    return {"error": "Replay is not started - call tv_desktop_replay_start first"}
                self.replay_state["current_date"] += 60000 * payload["count"]
                return {"ok": True, "action": "step", "requested": payload["count"],
                        "stepped": payload["count"], **self.replay_state}
            if a == "trade":
                self.replay_state["position"] = {"side": payload["side"]}
                return {"ok": True, "action": "trade", "side": payload["side"], **self.replay_state}
            if a == "stop":
                self.replay_state.update(started=False)
                return {"ok": True, "action": "stopped", **self.replay_state}
            return {"ok": True, **self.replay_state}
        if "/*tvmcp:pine_open*/" in expr:
            return {"ready": self.editor is not None, "opened": True}
        if "/*tvmcp:pine_get*/" in expr:
            if self.editor is None:
                return {"no_editor": True}
            return {"source": self.editor, "lines": self.editor.count("\n") + 1,
                    "chars": len(self.editor), "markers": self.markers}
        if "/*tvmcp:pine_set*/" in expr:
            payload = _payload(expr)
            if self.saved and not payload.get("overwrite_saved"):
                return {"saved_script_open": True}
            self.editor = payload["source"]
            return {"lines": 1, "chars": len(self.editor), "applied": True,
                    "replaced_saved_script": self.saved}
        if "/*tvmcp:pine_click*/" in expr:
            if self.save_prompt_on_compile:
                self.pending_dialog = "save_prompt"
            return {"clicked": self.compile_button, "studies_before": 2}
        if "/*tvmcp:pine_result*/" in expr:
            markers = self.markers
            if self.marker_sequence:
                markers = self.marker_sequence[0]
                if len(self.marker_sequence) > 1:
                    self.marker_sequence.pop(0)
            ok = not any(m["severity"] == "error" for m in markers)
            after = self.studies_after if self.studies_after is not None else (3 if ok else 2)
            return {"markers": markers, "studies_after": after}
        if "/*tvmcp:pine_errors*/" in expr:
            if self.editor is None:
                return {"no_editor": True}
            return {"markers": self.markers, "source_head": (self.editor or "")[:400]}
        if "/*tvmcp:pine_save*/" in expr:
            return {"dialog": False}
        if "/*tvmcp:pine_list*/" in expr:
            return {"scripts": self.scripts}
        if "/*tvmcp:pine_open_script*/" in expr:
            payload = _payload(expr)
            q = payload["name"].lower()
            m = [sc for sc in self.scripts if q in sc["name"].lower()]
            if not m:
                return {"not_found": True, "names": [sc["name"] for sc in self.scripts]}
            self.editor = "//@version=6\nindicator('x')"
            self.script_name, self.saved = m[0]["name"], True
            return {"name": m[0]["name"], "id": m[0]["id"], "version": 1,
                    "lines": 2, "chars": len(self.editor)}
        # --- M8 part B ---------------------------------------------------------
        if "/*tvmcp:editor_health*/" in expr:
            live = self.editor is not None and self.editor_live
            return {"live": live, "has_model": live, "visible": live,
                    "width": self.editor_width if live else None}
        if "/*tvmcp:editor_stage*/" in expr:
            payload = _payload(expr)
            self.stages_tried.append(payload["stage"])
            if (self.editor_live_after_stage is not None
                    and payload["stage"] >= self.editor_live_after_stage):
                self.editor_live = True
            names = ["footer_button", "activateScriptEditorTab", "showWidget", "footer_toggle"]
            return {"stage": payload["stage"], "did": names[payload["stage"]]}
        if "/*tvmcp:dock*/" in expr:
            if self.floating_editor:
                self.floating_editor = False
                return {"floating": True, "docked": True}
            return {"floating": False, "docked": False}
        if "/*tvmcp:clear_studies*/" in expr:
            payload = _payload(expr)
            if not self.api:
                return {"no_api": True}
            mode = payload["mode"]
            gone = [s for s in self.studies if mode == "all" or s.get("strategy")]
            self.studies = [s for s in self.studies if s not in gone]
            removed = [{"id": s["id"], "title": s["title"]} for s in gone]
            self.cleared.extend(removed)
            return {"mode": mode, "removed": removed, "failed": [],
                    "remaining": [{"id": s["id"], "title": s["title"]} for s in self.studies]}
        if "/*tvmcp:pine_header*/" in expr:
            return {"no_editor": self.editor is None, "script_name": self.script_name,
                    "saved": self.saved, "declared_title": self._declared_title()}
        if "/*tvmcp:pine_menu*/" in expr:
            payload = _payload(expr)
            self.menu_clicks.append(payload["item"])
            if not self.menu_available:
                return {"menu_opened": False, "clicked": None, "items": []}
            items = ["Create new", "Open script Ctrl + O", "Save script Ctrl + S"]
            if self.copy_succeeds:
                items.insert(3, "Make a copy")
            item = payload["item"]
            if item == "copy":
                if not self.copy_succeeds:
                    return {"menu_opened": True, "clicked": None, "items": items}
                self.pending_dialog = "rename"
                return {"menu_opened": True, "clicked": "Make a copy", "items": items}
            if item == "new":
                self.editor, self.saved, self.script_name = "", False, "Untitled script"
                return {"menu_opened": True, "clicked": "Create new", "items": items}
            if item == "save":
                self.pending_dialog = "rename"
                return {"menu_opened": True, "clicked": "Save script Ctrl + S", "items": items}
            return {"menu_opened": True, "clicked": None, "items": items}
        if "/*tvmcp:pine_dialog*/" in expr:
            payload = _payload(expr)
            self.dialog_calls.append(payload)
            name = payload.get("name")
            if self.pending_dialog == "save_prompt":
                self.pending_dialog = "rename"
                return {"dialog": True, "kind": "save_prompt",
                        "title": "Save this script before adding?", "clicked": True}
            if self.pending_dialog == "rename":
                if name is None:
                    return {"dialog": True, "kind": "rename", "title": "Rename",
                            "filled": False, "clicked": False, "buttons": ["Cancel", "Save"]}
                self.pending_dialog = None
                self.script_name, self.saved = name, True
                if not any(s["name"] == name for s in self.scripts):
                    self.scripts.append({"id": f"id{len(self.scripts)}", "name": name,
                                         "title": name, "version": 1, "modified": None,
                                         "kind": "strategy"})
                return {"dialog": True, "kind": "rename", "title": "Rename",
                        "filled": True, "clicked": True, "via": "save-btn"}
            return {"dialog": False}
        if "/*tvmcp:pine_find*/" in expr:
            if self.editor is None:
                return {"no_editor": True}
            payload = _payload(expr)
            text, n, offs = self._occurrences(payload["needle"])
            return {"eol": "CRLF" if self._eol() == "\r\n" else "LF",
                    "occurrences": len(offs), "positions": self._positions(text, offs),
                    "sha256": self._sha(text), "chars": len(text),
                    "saved_script_open": self.saved}
        if "/*tvmcp:pine_replace*/" in expr:
            if self.editor is None:
                return {"no_editor": True}
            payload = _payload(expr)
            self.replace_calls.append(payload)
            if self.saved and not payload["overwrite_saved"] and not payload["dry_run"]:
                return {"refused_saved": True}
            text, n, offs = self._occurrences(payload["needle"])
            repl = self._norm(payload["replacement"])
            sha_before = self._sha(text)
            base = {"eol": "CRLF" if self._eol() == "\r\n" else "LF",
                    "occurrences_before": len(offs), "sha256_before": sha_before,
                    "positions": self._positions(text, offs), "saved_script_open": self.saved}
            if payload.get("expect_sha") and payload["expect_sha"] != sha_before:
                return {**base, "applied": False, "reason": "sha_mismatch",
                        "occurrences_after": len(offs), "sha256_after": sha_before}
            if len(offs) != payload["expected"]:
                return {**base, "applied": False, "reason": "occurrence_mismatch",
                        "occurrences_after": len(offs), "sha256_after": sha_before}
            if payload["dry_run"]:
                return {**base, "applied": False, "reason": "dry_run",
                        "occurrences_after": len(offs), "sha256_after": sha_before}
            self.editor = text.replace(n, repl)
            remain = self.editor.count(n)
            count_check = None if n in repl else remain == 0
            return {**base, "applied": True, "verified": count_check is not False,
                    "occurrences_after": remain, "sha256_after": self._sha(self.editor),
                    "count_check": count_check}
        if "/*tvmcp:list*/" in expr:
            return {
                "symbol": self.symbol,
                "resolution": "60",
                "visible_time_range": {"from": 1787151600, "to": 1787792400},
                "visible_price_range": {"from": 1.16, "to": 1.17},
                "shapes": [{"id": i, **s} for i, s in self.shapes.items()],
            }
        if "/*tvmcp:draw*/" in expr:
            payload = _payload(expr)
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
        if "/*tvmcp:probe*/" in expr:
            return {"raf": self.raf, "api": self.api,
                    "monaco_visible": self.monaco_visible,
                    "visibility": "visible" if self.visible else "hidden"}
        if "/*tvmcp:ui_find*/" in expr or "/*tvmcp:ui_click*/" in expr:
            payload = _payload(expr)
            self.ui_payloads.append(payload)
            q = payload.get("query") or payload.get("target")
            m = self._find_elements(q)
            if "/*tvmcp:ui_find*/" in expr:
                return {"query": q, "matches": m[:payload["limit"]]}
            if not m:
                buttons = [e for e in self.elements
                           if e.get("tag") == "button" or e.get("role") == "button"]
                return {"clicked": False, "miss": True, "candidates": buttons[:20]}
            if len(m) > 1:
                return {"clicked": False, "ambiguous": True, "candidates": m}
            self.clicks.append(m[0])
            return {"clicked": True, "matched": m[0]}
        if "/*tvmcp:dialogs*/" in expr:
            dismissed, remaining = [], []
            for d in self.dialogs:
                btns = [b.lower() for b in d.get("buttons", [])]
                if "cancel" in btns or "отмена" in btns:
                    dismissed.append({"title": d["title"], "via": "cancel"})
                elif "close" in btns:
                    dismissed.append({"title": d["title"], "via": "close"})
                else:
                    remaining.append({"title": d["title"], "buttons": d.get("buttons", [])})
            self.dialogs = [d for d in self.dialogs
                            if d["title"] in {r["title"] for r in remaining}]
            return {"dismissed": dismissed, "remaining": remaining}
        if "/*tvmcp:bars*/" in expr:
            payload = _payload(expr)
            self.bar_payloads.append(payload)
            if not self.api:
                return {"no_api": True}
            since = payload.get("since")
            earliest = self.bars[0][0] if self.bars else None
            latest = self.bars[-1][0] if self.bars else None
            rows = [r for r in self.bars if since is None or r[0] >= since]
            total = len(rows)
            rows = rows[-payload["count"]:]
            return {"symbol": self.symbol, "resolution": self.interval, "rows": rows,
                    "total_after_since": total, "loaded_bars": len(self.bars),
                    "earliest_loaded": earliest, "latest_loaded": latest, "pages_loaded": 0,
                    "history_exhausted": False,
                    "clamped": since is not None and earliest is not None and earliest > since}
        return {
            "title": "TradingView Desktop",
            "url": "https://www.tradingview.com/chart/",
            "visible": self.visible,
            "symbol": self.symbol,
            "interval": self.interval,
        }

    def _find_elements(self, q):
        ql = (q or "").lower()
        out = []
        for e in self.elements:
            hit = (e.get("data_name") == q
                   or ql in (e.get("aria_label") or "").lower()
                   or ((e.get("tag") == "button" or e.get("role") == "button")
                       and ql in (e.get("text") or "").lower()))
            if hit and e not in out:
                out.append(e)
        return out

    def type_text(self, text):
        self.typed.append(text)

    def press(self, key, modifiers=0):
        self.pressed.append(key)
        self.modifiers = modifiers
        # Ctrl+S on an unsaved buffer opens the name dialog, like the app does
        if key == "s" and modifiers and not self.saved:
            self.pending_dialog = "rename"

    def screenshot(self, path):
        self.shots.append(path)
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG fake")
