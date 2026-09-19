"""M8 desktop-3: chart-tab scoring and binding in `driver.cdp_page`.

Fakes `/json/list` targets and the per-target websocket (`_connect`), injects a
clock, and checks: the highest-scoring tab is bound; calls within 5 s reuse the
bound id without re-probing; after 5 s a rebind happens only when another tab
paints (rAF > 0) AND scores strictly higher; a throttled/minimized window never
thrashes; a failed probe scores -1 and an all-dead layout errors.
"""

import pytest
from fastmcp.exceptions import ToolError

from tvmcp.desktop import driver


class FakeCdp:
    """Stands in for driver._Cdp: answers Runtime.evaluate from a probe table."""

    calls: list = []

    def __init__(self, target_id, probes):
        self.target_id = target_id
        self._probes = probes
        self.closed = False

    def call(self, method, params=None):
        expr = (params or {}).get("expression", "")
        if method == "Runtime.evaluate" and "/*tvmcp:probe*/" in expr:
            FakeCdp.calls.append(self.target_id)
            p = self._probes[self.target_id]
            if p is None:
                raise ToolError("CDP Runtime.evaluate failed: target crashed")
            return {"result": {"value": p}}
        return {"result": {"value": {"title": "x", "symbol": "ES", "visible": True}}}

    def close(self):
        self.closed = True


def _targets(*ids):
    return [{"id": i, "type": "page", "url": "https://www.tradingview.com/chart/abc/",
             "webSocketDebuggerUrl": f"ws://127.0.0.1:9223/devtools/page/{i}"} for i in ids]


@pytest.fixture
def harness(monkeypatch):
    state = {"now": 100.0, "targets": _targets("A", "B"), "probes": {}}
    FakeCdp.calls = []
    monkeypatch.setattr(driver, "_bound", None)
    monkeypatch.setattr(driver, "_now", lambda: state["now"])
    monkeypatch.setattr(driver, "_chart_targets", lambda url: state["targets"])
    monkeypatch.setattr(driver, "_connect", lambda t: FakeCdp(t["id"], state["probes"]))
    yield state
    driver._bound = None


PAINTING = {"raf": 18, "api": True, "monaco_visible": False, "visibility": "visible"}
HIDDEN = {"raf": 0, "api": True, "monaco_visible": False, "visibility": "hidden"}
DEAD = {"raf": 0, "api": False, "monaco_visible": False, "visibility": "hidden"}


def _open(url="http://127.0.0.1:9223"):
    with driver.cdp_page(url) as page:
        return page._cdp.target_id


def test_score_weights():
    assert driver._score(PAINTING) == 13
    assert driver._score({**PAINTING, "monaco_visible": True}) == 15
    assert driver._score(HIDDEN) == 4
    assert driver._score(DEAD) == 0
    assert driver._score(None) == -1


def test_binds_highest_score(harness):
    harness["probes"] = {"A": HIDDEN, "B": PAINTING}
    assert _open() == "B"
    assert driver.bound_target() == {"id": "B", "score": 13, "chart_tabs": 2}
    assert sorted(FakeCdp.calls) == ["A", "B"]  # every tab probed once


def test_ties_go_to_first_tab(harness):
    harness["probes"] = {"A": PAINTING, "B": PAINTING}
    assert _open() == "A"


def test_no_rebind_or_reprobe_within_5s(harness):
    harness["probes"] = {"A": PAINTING, "B": HIDDEN}
    assert _open() == "A"
    n = len(FakeCdp.calls)
    harness["probes"] = {"A": DEAD, "B": PAINTING}  # the world changed...
    harness["now"] += 4.9
    assert _open() == "A"  # ...but the binding holds and nothing is probed
    assert len(FakeCdp.calls) == n


def test_rebinds_after_5s_when_other_tab_paints_and_scores_higher(harness):
    harness["probes"] = {"A": PAINTING, "B": HIDDEN}
    assert _open() == "A"
    harness["probes"] = {"A": HIDDEN, "B": PAINTING}
    harness["now"] += 5.1
    assert _open() == "B"
    assert driver.bound_target()["id"] == "B"


def test_minimized_window_does_not_thrash(harness):
    # Both tabs throttled (no rAF): the other tab scores higher on a tiebreak
    # bit but does not paint -> keep the current binding.
    harness["probes"] = {"A": PAINTING, "B": HIDDEN}
    assert _open() == "A"
    harness["probes"] = {"A": DEAD, "B": HIDDEN}
    harness["now"] += 6
    assert _open() == "A"
    assert driver.bound_target()["score"] == 0  # score refreshed, binding kept


def test_equal_score_after_5s_keeps_binding(harness):
    harness["probes"] = {"A": PAINTING, "B": HIDDEN}
    assert _open() == "A"
    harness["probes"] = {"A": PAINTING, "B": PAINTING}
    harness["now"] += 6
    assert _open() == "A"  # strictly higher required


def test_bound_tab_closed_rescans(harness):
    harness["probes"] = {"A": PAINTING, "B": HIDDEN}
    assert _open() == "A"
    harness["targets"] = _targets("B", "C")
    harness["probes"] = {"B": HIDDEN, "C": PAINTING}
    assert _open() == "C"
    assert driver.bound_target()["chart_tabs"] == 2


def test_failed_probe_scores_minus_one_and_all_dead_raises(harness):
    harness["probes"] = {"A": None, "B": HIDDEN}
    assert _open() == "B"
    driver._bound = None
    harness["probes"] = {"A": None, "B": None}
    with pytest.raises(ToolError, match="Could not evaluate"):
        _open()


def test_fresh_websocket_per_call(harness):
    harness["probes"] = {"A": PAINTING, "B": HIDDEN}
    with driver.cdp_page("http://x") as page:
        cdp = page._cdp
        assert not cdp.closed
    assert cdp.closed
    with driver.cdp_page("http://x") as page2:
        assert page2._cdp is not cdp
