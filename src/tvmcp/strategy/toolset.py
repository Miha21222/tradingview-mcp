"""`strategy` toolset: declarative, YAML-defined strategies run through the backtest engine.

Strategies live as YAML files in `TV_STRATEGY_DIR` and compose vetted built-in
primitives only (see `strategy/spec.py`). `tv_strategy_list` scans the folder;
`tv_strategy_run` loads a spec, merges params, and runs it through
`backtest.engine.run`. No user/LLM code is executed in the server process - the
sandboxed Python escape hatch is deferred.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Any, Callable

import pandas as pd
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..backtest import engine
from ..backtest.forex import quote_currency
from ..backtest.toolset import _load as _bt_load, _validate_params as _bt_validate_params
from ..cache import BarCache
from ..config import Settings
from ..symbols import resolve_timeframe
from .spec import load_spec


def _load(settings: Settings, cache: BarCache, symbol: str, timeframe: str, count: int, provider: str):
    return _bt_load(settings, cache, symbol, timeframe, count, provider)


def register(mcp: Any, settings: Settings, loader: Callable | None = None) -> None:
    cache = BarCache(settings.cache_dir)
    load = loader or (lambda s, tf, c, p: _load(settings, cache, s, tf, c, p))

    def _resolve_spec(name: str) -> tuple:
        root = settings.strategy_dir.resolve()
        if not root.exists():
            raise ToolError(
                f"Strategy folder {root} does not exist; set TV_STRATEGY_DIR and add "
                "a YAML strategy spec"
            )
        target = (root / name).resolve()
        if target.suffix.lower() not in (".yaml", ".yml") or target.parent != root:
            raise ToolError(f"{name!r} is not a YAML file directly inside {root}")
        if not target.exists():
            raise ToolError(f"No strategy spec named {name!r} in {root}")
        try:
            return target, load_spec(target)
        except ValueError as exc:
            raise ToolError(f"Invalid strategy spec {name}: {exc}") from exc

    @mcp.tool(tags={"strategy"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_strategy_list(
        limit: Annotated[int, Field(description="Max specs to return", ge=1, le=200)] = 100,
    ) -> dict:
        """List declarative strategy YAML specs in TV_STRATEGY_DIR.

        Each entry reports the spec's name, description, the vetted strategy it
        composes, and its default params; malformed specs list an `error`. Read-only.
        """
        root = settings.strategy_dir
        if not root.exists():
            return {"strategy_dir": str(root), "count": 0, "strategies": [],
                    "note": "folder missing; set TV_STRATEGY_DIR and add YAML specs"}
        files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in (".yaml", ".yml"))
        out = []
        for p in files[:limit]:
            try:
                spec = load_spec(p)
                out.append({**spec, "file": p.name})
            except ValueError as exc:
                out.append({"file": p.name, "error": str(exc)})
        return {"strategy_dir": str(root), "count": len(out), "total_files": len(files), "strategies": out}

    @mcp.tool(tags={"strategy"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_strategy_run(
        name: Annotated[str, Field(description="Strategy spec file name (YAML) in TV_STRATEGY_DIR")],
        symbol: Annotated[str, Field(description="Any alias: EURUSD, OANDA:EURUSD, EUR_USD, eurusd")] = "EURUSD",
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1")] = "M15",
        count: Annotated[int, Field(description="Most-recent bars to backtest", ge=50)] = 300,
        provider: Annotated[str, Field(description="auto | dukascopy | oanda")] = "auto",
        cash: Annotated[float, Field(description="Starting account balance (account currency)", gt=0)] = 10_000.0,
        spread_pips: Annotated[float, Field(description="Round-trip spread cost in pips", ge=0)] = 1.0,
        trade_on_close: Annotated[bool, Field(description="Fill at current bar close (True) vs next bar open (False)")] = False,
        margin: Annotated[float, Field(description="Fraction of notional held as margin (0.02 = 1:50)", gt=0.0, le=1.0)] = 0.02,
        account_currency: Annotated[str, Field(description="3-letter account currency, e.g. USD")] = "USD",
        quote_to_account_rate: Annotated[float | None, Field(description="Explicit quote->account rate when they differ (e.g. 1/150 for USD account on USDJPY)")] = None,
        params_json: Annotated[str, Field(description="JSON object overriding the spec's default params, e.g. {\"rr\":2.5}")] = "{}",
    ) -> dict:
        """Run a declarative strategy spec through the backtest engine.

        Loads the YAML spec from TV_STRATEGY_DIR, composes only vetted built-in
        primitives (never user/LLM code), merges `params_json` overrides, and runs
        via the same engine as tv_backtest_run. Returns stats + capped trades with
        the same fill-model / currency semantics. Read-only.
        """
        target, spec = _resolve_spec(name)
        try:
            overrides = json.loads(params_json) if params_json.strip() else {}
        except json.JSONDecodeError as exc:
            raise ToolError(f"params_json is not valid JSON: {exc}") from exc
        if not isinstance(overrides, dict):
            raise ToolError("params_json must be a JSON object")
        if "symbol" in overrides:
            raise ToolError("'symbol' is managed by the tool; remove it from params_json")

        account_currency = account_currency.strip().upper()
        if len(account_currency) != 3 or not account_currency.isalpha():
            raise ToolError(f"account_currency must be a 3-letter code, got {account_currency!r}")
        if quote_to_account_rate is not None and (not math.isfinite(quote_to_account_rate) or quote_to_account_rate <= 0):
            raise ToolError("quote_to_account_rate must be a finite number > 0")

        params = {**spec["params"], **overrides}
        try:
            _bt_validate_params(spec["strategy"], params)
        except ToolError:
            raise

        sym, tf, df = load(symbol, timeframe, count, provider)
        count = int(len(df))

        quote = quote_currency(sym.canonical)
        if quote_to_account_rate is None and quote is not None and quote != account_currency:
            raise ToolError(
                f"quote currency {quote} != account currency {account_currency}; "
                "pass quote_to_account_rate explicitly"
            )
        try:
            summary, trades, meta = engine.run(
                df, spec["strategy"], params, sym.canonical,
                cash=cash, spread_pips=spread_pips, trade_on_close=trade_on_close,
                margin=margin, account_currency=account_currency,
                quote_to_account_rate=quote_to_account_rate,
            )
        except (TypeError, AttributeError, ValueError) as exc:
            raise ToolError(f"Backtest failed: {exc}") from exc

        return {
            "strategy_name": spec["name"],
            "strategy": spec["strategy"],
            "description": spec["description"],
            "spec_file": target.name,
            "symbol": sym.canonical,
            "tv_symbol": sym.tv,
            "provider": provider if provider != "auto" else ("oanda" if settings.oanda_api_key else "dukascopy"),
            "timeframe": tf.canonical,
            "bars": count,
            "cash": cash,
            "spread_pips": spread_pips,
            "trade_on_close": trade_on_close,
            "fill_model": "current_close" if trade_on_close else "next_open",
            "margin": margin,
            "account_currency": account_currency,
            "quote_currency": meta["quote_currency"],
            "quote_to_account_rate": meta["quote_to_account_rate"],
            "params": params,
            "total_trades": summary.pop("_trades_count", len(trades)),
            "summary": summary,
            "trades_returned": len(trades),
            "trades": trades,
        }