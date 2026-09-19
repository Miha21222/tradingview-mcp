"""Bar replay on the live TradingView Desktop chart (M7).

Mechanism (borrowed from tradesdontlie/tradingview-mcp replay.js): the private
`window.TradingViewApi._replayApi` object. Its getters return WatchedValues
(unwrap with `.value()`). Landmines: (1) `selectDate(ms)` is async and MUST be
awaited inside the page - fire-and-forget leaves `isReplayStarted()` true while
`doStep()` does nothing; (2) `selectDate` resolves before the data series is
ready - poll `currentDate()` non-null before stepping; (3) `doStep()` updates
`currentDate()` ~500 ms later - poll for the change; (4) `changeAutoplayDelay`
accepts only a fixed whitelist and an invalid value corrupts the account's
cloud replay state - autoplay is deliberately NOT exposed here (step is enough
for an agent; autoplay is a human-watching feature).

Replay trades (`buy/sell/closePosition`) are the app's paper replay-trading
panel, not a broker - still write-gated, still the user's workspace.
"""

from __future__ import annotations

import json

from fastmcp.exceptions import ToolError

_NO_REPLAY = (
    "This TradingView Desktop page does not expose the replay API "
    "(chart still loading, or the app build changed). Retry after the chart "
    "renders; if it persists, the replay slice needs rework."
)

_REPLAY_JS = """
/*tvmcp:replay*/
(new Promise(async (resolve) => {
  const p = __PAYLOAD__;
  const rp = window.TradingViewApi && window.TradingViewApi._replayApi;
  if (!rp) return resolve({no_api: true});
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const u = (v) => (v && typeof v === 'object' && typeof v.value === 'function') ? v.value() : v;
  const call = (name) => { try { return u(rp[name]()); } catch (e) { return null; } };
  const state = () => ({
    available: call('isReplayAvailable'), started: call('isReplayStarted'),
    autoplay: call('isAutoplayStarted'), mode: call('replayMode'),
    current_date: call('currentDate'),
    position: call('position'), realized_pnl: call('realizedPL'),
  });
  try {
    if (p.action === 'status') return resolve({ok: true, ...state()});
    if (p.action === 'stop') {
      if (!call('isReplayStarted')) return resolve({ok: true, action: 'already_stopped', ...state()});
      await rp.stopReplay();
      await sleep(300);
      return resolve({ok: true, action: 'stopped', ...state()});
    }
    if (p.action === 'start') {
      if (!call('isReplayAvailable')) return resolve({error: 'Replay is not available for the current symbol/timeframe (plan limits or unsupported symbol)'});
      try { rp.showReplayToolbar(); } catch (e) {}
      if (p.time_ms != null) await rp.selectDate(p.time_ms);
      else await rp.selectFirstAvailableDate();
      let started = false, cd = null;
      for (let i = 0; i < 40; i++) {
        started = !!call('isReplayStarted'); cd = call('currentDate');
        if (started && cd != null) break;
        await sleep(250);
      }
      if (!started || cd == null) {
        try { await rp.stopReplay(); } catch (e) {}
        return resolve({error: 'Replay did not start - the date may have no data on this timeframe; try a more recent date or a higher timeframe'});
      }
      return resolve({ok: true, action: 'started', ...state()});
    }
    if (!call('isReplayStarted')) return resolve({error: 'Replay is not started - call tv_desktop_replay_start first'});
    if (p.action === 'step') {
      let moved = 0, last = call('currentDate');
      for (let n = 0; n < p.count; n++) {
        const before = last;
        await rp.doStep();
        let changed = false;
        for (let i = 0; i < 16; i++) {
          await sleep(200);
          last = call('currentDate');
          if (last !== before) { changed = true; break; }
        }
        if (!changed) break;
        moved++;
      }
      return resolve({ok: true, action: 'step', requested: p.count, stepped: moved, ...state()});
    }
    if (p.action === 'trade') {
      if (p.side === 'buy') await rp.buy();
      else if (p.side === 'sell') await rp.sell();
      else if (p.side === 'close') await rp.closePosition();
      else return resolve({error: 'side must be buy, sell or close'});
      await sleep(300);
      return resolve({ok: true, action: 'trade', side: p.side, ...state()});
    }
    return resolve({error: 'unknown action ' + p.action});
  } catch (e) { return resolve({error: String(e)}); }
}))
"""


def _run(page, payload: dict) -> dict:
    res = page.eval(_REPLAY_JS.replace("__PAYLOAD__", json.dumps(payload)),
                    await_promise=True)
    if not res or res.get("no_api"):
        raise ToolError(_NO_REPLAY)
    if res.get("error"):
        raise ToolError(f"Replay: {res['error']}")
    return res


def status(page) -> dict:
    return _run(page, {"action": "status"})


_CLOSE_PICKER_JS = """
/*tvmcp:replay_close_picker*/
(() => {
  let closed = 0;
  for (const d of document.querySelectorAll('[role="dialog"]')) {
    const btns = Array.from(d.querySelectorAll('button'));
    const b = btns.find(x => /^(cancel|отмена)$/i.test((x.textContent || '').trim()))
      || btns.find(x => /close|закрыть/i.test((x.getAttribute('aria-label') || x.textContent || '')));
    if (b) { b.click(); closed++; }
  }
  return {closed: closed};
})()
"""


def start(page, time_ms: int | None) -> dict:
    res = _run(page, {"action": "start", "time_ms": time_ms})
    # showReplayToolbar()+selectDate() leave the toolbar's date-picker dialog
    # open (verified 2026-09-19; Escape does not close it) - it would swallow
    # later keyboard shortcuts, so click its Cancel/close button.
    try:
        res["picker_closed"] = (page.eval(_CLOSE_PICKER_JS) or {}).get("closed", 0)
    except ToolError:
        res["picker_closed"] = None
    return res


def step(page, count: int) -> dict:
    return _run(page, {"action": "step", "count": count})


def trade(page, side: str) -> dict:
    return _run(page, {"action": "trade", "side": side})


def stop(page) -> dict:
    return _run(page, {"action": "stop"})
