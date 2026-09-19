"""Shared CDP-free fake of the desktop driver's page (no app, no network).

`FakePage` implements the `DesktopPage` interface the driver/pine_editor/replay/
strategy_tester/ui modules rely on and dispatches on the `/*tvmcp:<name>*/` tag
of the JS each helper sends, returning canned results shaped like the real
in-page blocks. Imported by test_desktop.py, test_desktop_ui.py and friends.
"""

import json


def _payload(expr):
    """The JSON object after `const p = ` (raw_decode: payload strings may hold `;`)."""
    return json.JSONDecoder().raw_decode(expr.split("const p = ", 1)[1])[0]


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
            self.editor = payload["source"]
            return {"lines": 1, "chars": len(self.editor), "applied": True}
        if "/*tvmcp:pine_click*/" in expr:
            return {"clicked": self.compile_button, "studies_before": 2}
        if "/*tvmcp:pine_result*/" in expr:
            return {"markers": self.markers, "studies_after": 3 if not any(
                m["severity"] == "error" for m in self.markers) else 2}
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
            return {"name": m[0]["name"], "id": m[0]["id"], "version": 1,
                    "lines": 2, "chars": len(self.editor)}
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
            rows = [r for r in self.bars if since is None or r[0] >= since]
            total = len(rows)
            rows = rows[-payload["count"]:]
            return {"symbol": self.symbol, "resolution": self.interval, "rows": rows,
                    "total_after_since": total, "loaded_bars": len(self.bars),
                    "earliest_loaded": earliest, "pages_loaded": 0,
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

    def screenshot(self, path):
        self.shots.append(path)
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG fake")
