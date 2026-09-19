"""Read the Strategy Tester of the live TradingView Desktop chart (M7).

Mechanism (borrowed from tradesdontlie/tradingview-mcp data.js, re-based on the
study path this driver already trusts): every `strategy()` script on the chart
is a study whose `_study.metaInfo()` has `isTVScriptStrategy`; its
`_study.reportData()` (a WatchedValue - unwrap with `.value()`) carries the
Strategy Tester report: `performance.all.*` key stats, `maxStrategyDrawDown`,
`sharpeRatio`, `sortinoRatio`, `buyHoldReturn`, and `ordersData()` the order
list. Landmines: (1) TradingView computes the report only for the strategy
SELECTED in the Strategy Tester panel - other strategies read null; (2) a hidden
(eye-off) strategy is never computed, and looks identical to "panel not open";
(3) the panel must have been opened once - `TradingView.bottomWidgetBar
.showWidget('backtesting')` opens it; (4) order `tm` is a bar index, not a time.

Read-only: this module never unhides strategies; it reports hidden ones so the
caller (or the user) can decide.
"""

from __future__ import annotations

import json

from fastmcp.exceptions import ToolError

from .driver import _NO_API

_STRATEGY_JS = """
/*tvmcp:strategy*/
(new Promise(async (resolve) => {
  if (!window.TradingViewApi) return resolve({no_api: true});
  const p = __PAYLOAD__;
  const ch = window.TradingViewApi.activeChart();
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const unwrap = (v) => (v && typeof v === 'object' && typeof v.value === 'function') ? v.value() : v;
  const fin = (x) => (typeof x === 'number' && Number.isFinite(x)) ? x : null;
  if (p.open_panel) {
    try {
      const b = window.TradingView && window.TradingView.bottomWidgetBar;
      if (b && typeof b.showWidget === 'function') b.showWidget('backtesting');
    } catch (e) {}
  }
  const list = [];
  for (const s of ch.getAllStudies()) {
    try {
      const api = ch.getStudyById(s.id);
      const m = api._study;
      const mi = m.metaInfo();
      if ((mi.isTVScriptStrategy || mi.is_strategy) && typeof m.reportData === 'function')
        list.push({id: s.id, title: s.name, api: api, m: m});
    } catch (e) {}
  }
  const brief = list.map(s => {
    let vis = null; try { vis = s.api.isVisible(); } catch (e) {}
    return {id: s.id, title: s.title, visible: vis};
  });
  if (!list.length) return resolve({strategies: [], report: null});
  const report = (m) => { try { return unwrap(m.reportData()); } catch (e) { return null; } };
  let chosen = null, rd = null;
  const t0 = Date.now();
  while (Date.now() - t0 <= p.wait_ms) {
    for (const s of list) { const r = report(s.m); if (r && r.performance) { chosen = s; rd = r; break; } }
    if (chosen || p.wait_ms === 0) break;
    await sleep(500);
  }
  if (!chosen) return resolve({strategies: brief, report: null});
  const perf = rd.performance, all = perf.all || {};
  const metrics = {
    net_profit: fin(all.netProfit), net_profit_percent: fin(all.netProfitPercent),
    gross_profit: fin(all.grossProfit), gross_loss: fin(all.grossLoss),
    profit_factor: fin(all.profitFactor),
    max_drawdown: fin(perf.maxStrategyDrawDown),
    max_drawdown_percent: fin(perf.maxStrategyDrawDownPercent),
    total_trades: fin(all.totalTrades) ?? ((all.numberOfWiningTrades || 0) + (all.numberOfLosingTrades || 0)),
    winning_trades: fin(all.numberOfWiningTrades), losing_trades: fin(all.numberOfLosingTrades),
    percent_profitable: fin(all.percentProfitable), avg_trade: fin(all.avgTrade),
    avg_winning_trade: fin(all.avgWinTrade), avg_losing_trade: fin(all.avgLosTrade),
    largest_win: fin(all.largestWinTrade), largest_loss: fin(all.largestLosTrade),
    commission_paid: fin(all.commissionPaid),
    sharpe_ratio: fin(perf.sharpeRatio), sortino_ratio: fin(perf.sortinoRatio),
    buy_hold_return: fin(perf.buyHoldReturn), buy_hold_return_percent: fin(perf.buyHoldReturnPercent),
    open_pl: fin(perf.openPL),
  };
  const sides = {};
  for (const k of ['long', 'short']) {
    const x = perf[k];
    if (x) sides[k] = {net_profit: fin(x.netProfit), total_trades: fin(x.totalTrades) ?? ((x.numberOfWiningTrades || 0) + (x.numberOfLosingTrades || 0)),
                       percent_profitable: fin(x.percentProfitable), profit_factor: fin(x.profitFactor)};
  }
  const out = {strategies: brief, selected: {id: chosen.id, title: chosen.title},
               currency: rd.currency || null, metrics: metrics, sides: sides,
               report_keys: Object.keys(rd).slice(0, 30)};
  if (p.orders > 0) {
    let od = null;
    try { od = unwrap(chosen.m.ordersData()); } catch (e) {}
    let bars = null;
    try { bars = ch._chartWidget.model().mainSeries().bars(); } catch (e) {}
    const timeOf = (i) => { try { const v = bars && bars.valueAt(i); return v ? v[0] : null; } catch (e) { return null; } };
    if (Array.isArray(od)) {
      out.total_orders = od.length;
      out.orders = od.slice(-p.orders).map(o => ({
        id: o.id ?? null, type: o.tp ?? null, side: o.b === true ? 'buy' : (o.b === false ? 'sell' : null),
        entry: o.e ?? null, price: fin(o.p), qty: fin(o.q), bar_index: o.tm ?? null,
        time: typeof o.tm === 'number' ? timeOf(o.tm) : null,
      }));
      if (od.length) out.order_keys = Object.keys(od[od.length - 1]).slice(0, 20);
    } else {
      out.total_orders = 0;
      out.orders = [];
      out.orders_note = 'ordersData() is not an array on this build';
    }
  }
  resolve(out);
}))
"""


def read_strategy(page, orders: int, open_panel: bool, wait_ms: int) -> dict:
    payload = {"orders": orders, "open_panel": open_panel, "wait_ms": wait_ms}
    res = page.eval(_STRATEGY_JS.replace("__PAYLOAD__", json.dumps(payload)),
                    await_promise=True)
    if not res or res.get("no_api"):
        raise ToolError(_NO_API)
    if not res.get("strategies"):
        raise ToolError(
            "No strategy() script is on the active chart. Add one (Pine editor "
            "'Add to chart' via tv_desktop_pine_compile, or from Indicators) "
            "and retry."
        )
    if res.get("report") is None and "metrics" not in res:
        hidden = [s["title"] for s in res["strategies"] if s.get("visible") is False]
        hint = (
            f" Hidden strategies never compute: {hidden}; ask the user to toggle "
            "them visible."
            if hidden else
            " The Strategy Tester computes only the strategy selected in its "
            "panel - open the panel, select the strategy, and retry."
        )
        raise ToolError(
            "Strategy present but its report is not computed yet." + hint
        )
    return res
