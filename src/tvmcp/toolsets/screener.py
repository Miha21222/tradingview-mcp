"""`public` toolset: TradingView unauthenticated scanner + symbol search.

No TradingView account involved -> no ban surface. Built on `tradingview-screener`
(pure JSON POST to scanner.tradingview.com, no page scraping).
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import Settings

_TF_SUFFIX = {
    "1D": "",  # daily fields have no suffix
    "M1": "|1", "M5": "|5", "M15": "|15", "M30": "|30",
    "H1": "|60", "H2": "|120", "H4": "|240", "W1": "|1W", "MN1": "|1M",
}

_RECOMMEND_LABELS = [
    (0.5, "STRONG_BUY"), (0.1, "BUY"), (-0.1, "NEUTRAL"), (-0.5, "SELL"),
]


def _label(value: float | None) -> str | None:
    if value is None:
        return None
    for threshold, label in _RECOMMEND_LABELS:
        if value >= threshold:
            return label
    return "STRONG_SELL"


# Non-equity scanner markets. tradingview-screener seeds every Query with
# stock-only default filters (is_primary + a filter2 block) that silently zero
# out results on these markets - they must be stripped there.
_NON_STOCK_MARKETS = {
    "forex", "crypto", "crypto_dex", "coin", "futures", "bond", "cfd", "options",
}


def _strip_stock_defaults(q: Any, market: str) -> Any:
    if market.lower() in _NON_STOCK_MARKETS:
        q.query["filter"] = []
        q.query.pop("filter2", None)
    return q


def register(mcp: Any, settings: Settings) -> None:
    from tradingview_screener import Query, col

    @mcp.tool(
        tags={"public"},
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    def tv_screener_run(
        market: Annotated[str, Field(description="Scanner market: forex, crypto, america, futures, bond, cfd, coin, ...")] = "forex",
        columns: Annotated[list[str] | None, Field(description="Scanner fields to return, e.g. ['name','close','change','RSI','Recommend.All']. Default: name/close/change/volume.")] = None,
        filters: Annotated[list[list[Any]] | None, Field(description="Filter triples [field, op, value]; ops: > >= < <= == != between isin. Example: [['RSI','<',30], ['close','>',1.05]]")] = None,
        order_by: Annotated[str | None, Field(description="Field to sort by (descending)")] = None,
        limit: Annotated[int, Field(description="Max rows (1-200)", ge=1, le=200)] = 50,
    ) -> dict:
        """Run a TradingView screener query (unauthenticated scanner, ~3000 fields).

        Use for: ranking/filtering instruments by indicator or fundamental values,
        finding oversold pairs, volume leaders, etc. US-equity quotes are 15-min
        delayed; forex/crypto are near-realtime. Timeframe-scoped fields take a
        suffix, e.g. 'RSI|15' for M15 RSI.
        """
        cols = columns or ["name", "close", "change", "volume"]
        q = _strip_stock_defaults(Query().set_markets(market).select(*cols).limit(limit), market)
        if filters:
            conds = []
            for f in filters:
                if len(f) != 3:
                    raise ToolError(f"Filter must be [field, op, value], got {f!r}")
                field, op, value = f
                c = col(field)
                try:
                    if op == ">":
                        conds.append(c > value)
                    elif op == ">=":
                        conds.append(c >= value)
                    elif op == "<":
                        conds.append(c < value)
                    elif op == "<=":
                        conds.append(c <= value)
                    elif op == "==":
                        conds.append(c == value)
                    elif op == "!=":
                        conds.append(c != value)
                    elif op == "between":
                        conds.append(c.between(value[0], value[1]))
                    elif op == "isin":
                        conds.append(c.isin(value))
                    else:
                        raise ToolError(f"Unknown filter op {op!r}")
                except TypeError as exc:
                    raise ToolError(f"Bad filter {f!r}: {exc}") from exc
            q = q.where(*conds)
        if order_by:
            q = q.order_by(order_by, ascending=False)
        try:
            total, df = q.get_scanner_data()
        except Exception as exc:  # scanner returns opaque errors; surface them
            raise ToolError(f"Scanner query failed: {exc}") from exc
        return {
            "market": market,
            "total_matches": int(total),
            "returned": len(df),
            "rows": df.to_dict("records"),
        }

    @mcp.tool(
        tags={"public"},
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    def tv_ta_summary(
        symbols: Annotated[list[str], Field(description="TradingView tickers, e.g. ['OANDA:EURUSD','OANDA:GBPUSD'] (bare 'EURUSD' is auto-prefixed with OANDA:)")],
        timeframe: Annotated[str, Field(description="1D, M15, H1, H4, W1 ...")] = "1D",
        market: Annotated[str, Field(description="Scanner market the tickers belong to: forex (default), crypto, america, ...")] = "forex",
    ) -> dict:
        """Technical-analysis summary per symbol: overall/MA/oscillator ratings plus RSI and close.

        Ratings are TradingView's precomputed Recommend.* fields mapped to
        STRONG_BUY..STRONG_SELL. Use for a quick bias read, not as a trade signal.
        """
        suffix = _TF_SUFFIX.get(timeframe.upper().replace("1D", "1D"))
        if suffix is None:
            raise ToolError(f"Unsupported timeframe {timeframe!r}; use one of {sorted(_TF_SUFFIX)}")
        tickers = [s if ":" in s else f"OANDA:{s.upper()}" for s in symbols]
        fields = [f"Recommend.All{suffix}", f"Recommend.MA{suffix}", f"Recommend.Other{suffix}", f"RSI{suffix}", "close"]
        q = _strip_stock_defaults(
            Query().set_markets(market).set_tickers(*tickers).select("name", *fields),
            market,
        )
        try:
            _, df = q.get_scanner_data()
        except Exception as exc:
            raise ToolError(f"Scanner query failed: {exc}") from exc
        out = []
        for r in df.to_dict("records"):
            out.append(
                {
                    "ticker": r.get("ticker"),
                    "close": r.get("close"),
                    "rsi": r.get(f"RSI{suffix}"),
                    "summary": _label(r.get(f"Recommend.All{suffix}")),
                    "ma": _label(r.get(f"Recommend.MA{suffix}")),
                    "oscillators": _label(r.get(f"Recommend.Other{suffix}")),
                }
            )
        return {"timeframe": timeframe, "symbols": out}

    @mcp.tool(
        tags={"public"},
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    def tv_symbol_search(
        text: Annotated[str, Field(description="Search text, e.g. 'EURUSD' or 'gold'")],
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict:
        """Search TradingView's symbol directory. Returns exchange-qualified tickers with descriptions and types."""
        try:
            resp = httpx.get(
                "https://symbol-search.tradingview.com/symbol_search/v3/",
                params={"text": text, "hl": "0", "lang": "en", "search_type": "undefined"},
                headers={
                    "Origin": "https://www.tradingview.com",
                    "Referer": "https://www.tradingview.com/",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"Symbol search failed: {exc}") from exc
        if resp.status_code != 200:
            raise ToolError(f"Symbol search HTTP {resp.status_code}")
        items = resp.json().get("symbols", [])[:limit]
        return {
            "query": text,
            "symbols": [
                {
                    "ticker": f"{i.get('exchange', '')}:{i.get('symbol', '')}".strip(":"),
                    "description": (i.get("description") or "").replace("<em>", "").replace("</em>", ""),
                    "type": i.get("type"),
                    "currency": i.get("currency_code"),
                }
                for i in items
            ],
        }
