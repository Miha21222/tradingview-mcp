"""One-call Pine build + backtest pipeline on the live Desktop chart (M8 part B).

Stages, each recorded in `steps[]` with elapsed ms:
  prepare   -> workspace.prepare (window, dialogs, dock, editor, clear studies, nav)
  own_copy  -> pine_editor.ensure_own_copy (never touch a foreign saved script)
  set_source-> pine_editor.set_source on the agent-owned buffer
  compile   -> pine_editor.compile_on_chart (buttons EN/RU, save/rename dialogs,
               marker settling)
  markers   -> errors stop here with landmine hints; warnings never stop
  results   -> strategy(): poll the Strategy Tester up to `max_wait_s`;
               one recompile pass when the chart lost the study; indicator():
               results null with a note

Once the CDP connection is up this never raises: every failure comes back as
`{ok: false, stage, error, steps, ...}` so the caller can fix the source and
call again with the SAME name (the rerun lands on the same agent-owned copy).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from . import clock, pine_editor, pine_hints, strategy_tester, workspace

RERUN_NOTE = "Fix the source and call again with the SAME name - the rerun is safe."
RECOMPILE_AFTER_S = 5.0


def _indicator_dialog_fallback() -> str:
    """Stub: the Indicators-dialog keyboard path is deliberately not automated."""
    return ("fragile: no Strategy Tester report arrived; the Indicators-dialog "
            "keyboard fallback is not implemented. Open the Strategy Tester panel, "
            "select the strategy by hand, then call tv_desktop_read_strategy.")


def _poll_results(page, steps: list, t0: float, max_wait_s: float, name: str) -> tuple[dict | None, str | None]:
    start = clock.now()
    deadline = start + max_wait_s
    recompiled, last_err, polls = False, None, 0
    while True:
        polls += 1
        try:
            res = strategy_tester.read_strategy(page, orders=0, open_panel=True, wait_ms=500)
            workspace.step(steps, "results", t0, polls=polls, recompiled=recompiled)
            return res, None
        except ToolError as e:
            last_err = str(e)
        if (clock.now() - start) >= RECOMPILE_AFTER_S and not recompiled:
            count = None
            try:
                count = pine_editor.study_count(page)
            except ToolError as e:
                last_err = str(e)
            if count == 0:
                recompiled = True
                try:
                    comp = pine_editor.compile_on_chart(page, save_name=name)
                    workspace.step(steps, "recompile", t0, clicked=comp.get("clicked"),
                                   errors=len(comp.get("errors") or []))
                except ToolError as e:
                    last_err = str(e)
                    workspace.step(steps, "recompile", t0, error=last_err)
        if clock.now() >= deadline:
            break
        clock.sleep(1.0)
    workspace.step(steps, "results", t0, polls=polls, recompiled=recompiled, timed_out=True)
    note = _indicator_dialog_fallback()
    if last_err:
        note += f" Last error: {last_err}"
    return None, note


def build_and_backtest(page, source: str, name: str, symbol: str | None = None,
                       timeframe: str | None = None, clear_studies="strategies",
                       max_wait_s: float = 25) -> dict:
    steps: list[dict] = []
    t0 = clock.now()
    out: dict = {
        "ok": False, "stage": "prepare", "steps": steps,
        "script": {"name": name, "mode": None, "verified": None},
        "compile_errors": [], "compile_warnings": [], "hints": [],
        "results": None, "note": None,
    }

    def fail(stage: str, err) -> dict:
        out.update(ok=False, stage=stage, error=str(err))
        return out

    try:
        # prepare
        try:
            prep = workspace.prepare(page, clear_studies, symbol, timeframe)
        except ToolError as e:
            return fail("prepare", e)
        workspace.step(steps, "prepare", t0, editor_open=prep["pine_editor_open"],
                       cleared=len(prep["cleared_studies"]), width=prep["pine_editor_width"])
        out["workspace"] = {k: prep.get(k) for k in (
            "symbol", "resolution", "pine_editor_open", "pine_editor_width",
            "cleared_studies", "blocking_dialogs", "note")}
        if not prep["pine_editor_open"]:
            return fail("prepare", prep.get("note") or "Pine editor did not open")

        # own_copy
        out["stage"] = "own_copy"
        try:
            own = pine_editor.ensure_own_copy(page, name)
        except ToolError as e:
            return fail("own_copy", e)
        out["script"].update(mode=own["mode"], verified=bool(own["verified"]),
                             header=own.get("header"))
        workspace.step(steps, "own_copy", t0, mode=own["mode"], verified=bool(own["verified"]))

        # set_source
        out["stage"] = "set_source"
        try:
            pine_editor.set_source(page, source, overwrite_saved=own["mode"] in ("own", "copied"))
        except ToolError as e:
            return fail("set_source", e)
        workspace.step(steps, "set_source", t0, chars=len(source))

        # compile
        out["stage"] = "compile"
        try:
            comp = pine_editor.compile_on_chart(page, save_name=name)
        except ToolError as e:
            return fail("compile", e)
        workspace.step(steps, "compile", t0, clicked=comp.get("clicked"),
                       dialogs=len(comp.get("dialogs_handled") or []),
                       settled=comp.get("settled"))
        out["compile_dialogs"] = comp.get("dialogs_handled") or []

        # markers
        out["stage"] = "markers"
        out["compile_errors"] = comp.get("errors") or []
        out["compile_warnings"] = comp.get("warnings") or []
        out["hints"] = pine_hints.hints_for(out["compile_errors"], out["compile_warnings"], source)
        workspace.step(steps, "markers", t0, errors=len(out["compile_errors"]),
                       warnings=len(out["compile_warnings"]))
        if out["compile_errors"]:
            out["note"] = RERUN_NOTE
            return out

        # results
        out["stage"] = "results"
        if not pine_editor.declares_strategy(source):
            out.update(ok=True, results=None,
                       note="indicator() script: no Strategy Tester results. Read its output "
                            "with tv_desktop_read_study_plots / tv_desktop_read_study_graphics.")
            workspace.step(steps, "results", t0, skipped="indicator")
            return out
        results, note = _poll_results(page, steps, t0, max_wait_s, name)
        out["results"] = results
        if results is None:
            out.update(ok=False, note=note)
            return out
        out.update(ok=True, note="Compare net_profit against buy_hold_return before calling "
                                 "the result good; zero trades twice in a row = structural "
                                 "(date range, margin, pyramiding), not luck.")
        return out
    except ToolError as e:  # anything unexpected still comes back as data
        return fail(out.get("stage") or "unknown", e)
    except Exception as e:  # noqa: BLE001 - pipeline contract: never raise after connect
        return fail(out.get("stage") or "unknown", f"{type(e).__name__}: {e}")
