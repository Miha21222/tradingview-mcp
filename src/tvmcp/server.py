"""tvmcp server assembly: builds the FastMCP app with toolset-gated registration.

Toolsets register only when enabled via TV_TOOLSETS (default: public,data) - the
gate is registration itself, so disabled toolsets cost zero context. See
docs/PLAN.md for the toolset matrix and CLAUDE.md for the hard rules.
"""

from __future__ import annotations

import sys

from fastmcp import FastMCP

from .config import Settings, load_settings

SERVER_NAME = "tradingview-mcp"

_INSTRUCTIONS = """\
TradingView MCP: market data, screener, and (in later milestones) SMC/ICT pattern
scanning, chart rendering, backtesting. Not affiliated with TradingView, Inc.

Conventions: bars come as arrays [time,open,high,low,close,volume] in UTC; every
price/level names its data feed via the `provider` field - feeds disagree, treat
levels as feed-specific. Default toolsets need no TradingView account.
"""


def build_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or load_settings()
    mcp = FastMCP(name=SERVER_NAME, instructions=_INSTRUCTIONS)

    if settings.toolset_enabled("public"):
        from . import doctor
        from .toolsets import screener

        screener.register(mcp, settings)
        doctor.register(mcp, settings)
    if settings.toolset_enabled("data"):
        from .toolsets import data

        data.register(mcp, settings)
    if settings.toolset_enabled("scan"):
        from . import scan

        scan.register(mcp, settings)
    if settings.toolset_enabled("chart"):
        from . import chart

        chart.register(mcp, settings)
    if settings.toolset_enabled("backtest"):
        from . import backtest

        backtest.register(mcp, settings)
    if settings.toolset_enabled("journal"):
        from . import journal

        journal.register(mcp, settings)
    if settings.toolset_enabled("session"):
        from . import session

        session.register(mcp, settings)
    if settings.toolset_enabled("pine"):
        from . import pine

        pine.register(mcp, settings)
    if settings.toolset_enabled("desktop"):
        from . import desktop

        desktop.register(mcp, settings)
    if settings.toolset_enabled("strategy"):
        from . import strategy

        strategy.register(mcp, settings)
    # Future toolset (webhook) registers here behind its flag when it lands.
    return mcp


def main() -> None:
    settings = load_settings()
    mcp = build_server(settings)

    if "--check" in sys.argv:
        import asyncio

        async def _check() -> None:
            tools = await mcp.list_tools()
            print(f"{SERVER_NAME}: {len(tools)} tools registered")
            print(f"toolsets enabled: {', '.join(sorted(settings.toolsets))}")
            for t in sorted(tools, key=lambda t: t.name):
                print(f"  {t.name}")

        asyncio.run(_check())
        return

    mcp.run()  # stdio


if __name__ == "__main__":
    main()
