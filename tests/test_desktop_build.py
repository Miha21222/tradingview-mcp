"""M8 part B: `desktop/pine_build.py` + `tv_desktop_pine_build_and_backtest`.

Happy path, foreign saved script -> verified copy, copy fails -> create-new,
compile errors stop at markers with hints, warnings never stop, results on
poll N, indicator source -> results null, timeout -> fragile note with a
recompile pass, prepare failure, every step recorded. FakePage + FakeClock.
"""

import asyncio
import json
from contextlib import contextmanager

import pytest
from fastmcp import FastMCP

from tvmcp import desktop
from tvmcp.config import Settings
from tvmcp.desktop import clock, driver, pine_build

from desktop_fake import FakeClock, FakePage

STRAT = ("//@version=6\nstrategy(\"Agent Strat\", overlay=true, calc_on_every_tick=true)\n"
         "ma = ta.sma(close, 20)\nif ta.crossover(close, ma)\n    strategy.entry('L', strategy.long)\n")
INDI = "//@version=6\nindicator(\"Agent Indi\", overlay=true)\nplot(ta.sma(close, 20))\n"
REPORT = {
    "strategies": [{"id": "s1", "title": "Agent Strat", "visible": True}],
    "selected": {"id": "s1", "title": "Agent Strat"}, "currency": "USD",
    "metrics": {"net_profit": 120.5, "profit_factor": 1.4, "buy_hold_return": 300.0,
                "total_trades": 12},
    "sides": {},
}
STAGES = ["prepare", "own_copy", "set_source", "compile", "markers", "results"]


def _settings(tmp_path, read_only=False) -> Settings:
    return Settings(
        toolsets=frozenset({"desktop"}),
        extra_tools=frozenset(),
        read_only=read_only,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=None,
        oanda_env="practice",
        session_id=None,
    )


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch):
    from tvmcp.desktop import warnings as w

    fc = FakeClock()
    monkeypatch.setattr(clock, "now", fc.now)
    monkeypatch.setattr(clock, "sleep", fc.sleep)
    w._printed = False
    driver._bound = None
    yield fc
    w._printed = False
    driver._bound = None


def _build(tmp_path, page=None, read_only=False):
    page = page or FakePage()

    @contextmanager
    def factory(cdp_url):
        yield page

    mcp = FastMCP(name="test")
    desktop.register(mcp, _settings(tmp_path, read_only), page_factory=factory)
    return mcp, page


def _data(mcp, name, args=None):
    r = asyncio.run(mcp.call_tool(name, args or {}))
    return json.loads(r.content[0].text)


def _page(**kw):
    page = FakePage(studies=[{"id": "lux1", "title": "LuxAlgo", "strategy": False},
                             {"id": "old", "title": "Old strategy", "strategy": True}])
    page.editor = ""
    page.strategy = REPORT
    for k, v in kw.items():
        setattr(page, k, v)
    return page


def _step_names(res):
    return [s["step"] for s in res["steps"] if s["step"] in STAGES]


def test_happy_path_records_every_stage():
    page = _page()
    res = pine_build.build_and_backtest(page, STRAT, "agent-x")
    assert res["ok"] is True and res["stage"] == "results"
    assert res["results"]["metrics"]["buy_hold_return"] == 300.0
    assert res["script"] == {"name": "agent-x", "mode": "unsaved", "verified": True,
                             "header": res["script"]["header"]}
    assert _step_names(res) == STAGES
    assert all(isinstance(s["ms"], int) and s["ms"] >= 0 for s in res["steps"])
    assert page.editor == STRAT
    # default clear_studies='strategies': the old strategy went, LuxAlgo stayed
    assert [s["id"] for s in page.studies] == ["lux1"]
    assert res["workspace"]["cleared_studies"] == [{"id": "old", "title": "Old strategy"}]
    assert res["compile_errors"] == [] and res["hints"] == []
    assert "buy_hold_return" in res["note"]
    assert page.strategy_payloads[-1] == {"orders": 0, "open_panel": True, "wait_ms": 500}


def test_foreign_saved_script_becomes_verified_copy():
    page = _page(editor="//@version=6\nindicator('theirs')", script_name="My SMC", saved=True)
    res = pine_build.build_and_backtest(page, STRAT, "agent-x")
    assert res["ok"] is True
    assert res["script"]["mode"] == "copied" and res["script"]["verified"] is True
    assert page.script_name == "agent-x" and any(s["name"] == "agent-x" for s in page.scripts)
    assert page.editor == STRAT  # set on the copy, with overwrite_saved (it is ours now)
    assert "theirs" not in page.editor


def test_copy_failure_falls_back_to_create_new():
    page = _page(editor="//@version=6\nindicator('theirs')", script_name="My SMC",
                 saved=True, copy_succeeds=False)
    res = pine_build.build_and_backtest(page, STRAT, "agent-x")
    assert res["ok"] is True and res["script"]["mode"] == "created"
    assert page.menu_clicks == ["copy", "new"] and page.editor == STRAT


def test_no_safe_buffer_fails_at_own_copy_without_raising():
    page = _page(editor="//@version=6\nindicator('theirs')", script_name="My SMC",
                 saved=True, menu_available=False)
    res = pine_build.build_and_backtest(page, STRAT, "agent-x")
    assert res["ok"] is False and res["stage"] == "own_copy"
    assert "agent-owned buffer" in res["error"]
    assert page.editor == "//@version=6\nindicator('theirs')"
    assert _step_names(res) == ["prepare"]


def test_compile_errors_stop_at_markers_with_hints():
    page = _page(markers=[{"line": 2, "column": 1, "severity": "error",
                           "message": "Undeclared identifier 'strategy.fixed'"},
                          {"line": 1, "column": 1, "severity": "warning", "message": "unused"}])
    res = pine_build.build_and_backtest(page, STRAT.replace("//@version=6", "//@version=5"), "agent-x")
    assert res["ok"] is False and res["stage"] == "markers"
    assert res["compile_errors"][0]["line"] == 2 and len(res["compile_warnings"]) == 1
    assert any("default_qty_type" in h for h in res["hints"])
    assert any("//@version=6" in h for h in res["hints"])
    assert res["note"] == pine_build.RERUN_NOTE
    assert res["results"] is None and page.strategy_polls == 0
    assert _step_names(res) == ["prepare", "own_copy", "set_source", "compile", "markers"]


def test_warnings_alone_never_stop():
    page = _page(markers=[{"line": 1, "column": 1, "severity": "warning", "message": "unused"}])
    res = pine_build.build_and_backtest(page, STRAT, "agent-x")
    assert res["ok"] is True and len(res["compile_warnings"]) == 1 and res["hints"] == []


def test_results_arrive_on_poll_n(_fake_clock):
    page = _page(strategy_after_n_polls=3)
    res = pine_build.build_and_backtest(page, STRAT, "agent-x", max_wait_s=25)
    assert res["ok"] is True
    results_step = next(s for s in res["steps"] if s["step"] == "results")
    assert results_step["polls"] == 4 and results_step["recompiled"] is False
    assert _fake_clock.slept.count(1.0) == 3


def test_indicator_source_returns_null_results():
    page = _page()
    res = pine_build.build_and_backtest(page, INDI, "agent-i")
    assert res["ok"] is True and res["results"] is None
    assert "indicator()" in res["note"] and page.strategy_polls == 0
    assert next(s for s in res["steps"] if s["step"] == "results")["skipped"] == "indicator"


def test_timeout_recompiles_once_when_chart_lost_the_study_then_notes_fragile(_fake_clock):
    page = _page(strategy_after_n_polls=10_000, studies_after=0)
    res = pine_build.build_and_backtest(page, STRAT, "agent-x", max_wait_s=10)
    assert res["ok"] is False and res["stage"] == "results" and res["results"] is None
    assert res["note"].startswith("fragile:") and "No strategy" in res["note"]
    names = [s["step"] for s in res["steps"]]
    assert names.count("recompile") == 1
    assert names[-1] == "results" and res["steps"][-1]["timed_out"] is True
    assert res["steps"][-1]["recompiled"] is True
    assert [e for e in page.exprs if "/*tvmcp:pine_click*/" in e].__len__() == 2
    assert not res["note"].endswith("keyboard fallback")  # last error is appended


def test_timeout_without_recompile_when_studies_present(_fake_clock):
    page = _page(strategy_after_n_polls=10_000)
    res = pine_build.build_and_backtest(page, STRAT, "agent-x", max_wait_s=8)
    assert res["ok"] is False
    assert "recompile" not in [s["step"] for s in res["steps"]]


def test_prepare_failure_when_editor_never_mounts():
    page = _page(editor_live=False)
    res = pine_build.build_and_backtest(page, STRAT, "agent-x")
    assert res["ok"] is False and res["stage"] == "prepare"
    assert "click once inside" in res["error"]
    assert res["script"]["mode"] is None and page.editor == ""


def test_prepare_tool_error_is_returned_not_raised():
    page = _page(api=False)  # clear_studies needs the chart API
    res = pine_build.build_and_backtest(page, STRAT, "agent-x", clear_studies="all")
    assert res["ok"] is False and res["stage"] == "prepare" and "TradingViewApi" in res["error"]


def test_unexpected_exception_is_returned_as_data(monkeypatch):
    page = _page()
    monkeypatch.setattr(pine_build.pine_editor, "compile_on_chart",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res = pine_build.build_and_backtest(page, STRAT, "agent-x")
    assert res["ok"] is False and res["stage"] == "compile" and "RuntimeError: boom" in res["error"]


def test_tool_resolves_symbol_timeframe_and_is_write_gated(tmp_path):
    mcp, page = _build(tmp_path, _page())
    data = _data(mcp, "tv_desktop_pine_build_and_backtest", {
        "source": STRAT, "name": "agent-x", "symbol": "eurusd", "timeframe": "M15",
        "clear_studies": "none"})
    assert data["ok"] is True and data["provider"] == "desktop"
    assert page.nav[-1]["symbol"] == "OANDA:EURUSD" and page.nav[-1]["resolution"] == "15"
    assert [s["id"] for s in page.studies] == ["lux1", "old"]  # clear_studies='none'
    names = {t.name for t in asyncio.run(_build(tmp_path, read_only=True)[0].list_tools())}
    assert "tv_desktop_pine_build_and_backtest" not in names
