"""Environment-driven settings. See CLAUDE.md and docs/PLAN.md for the contract."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TOOLSETS = frozenset({"public", "data"})
ALL_TOOLSETS = frozenset(
    {
        "public",
        "data",
        "scan",
        "chart",
        "backtest",
        "pine",
        "session",
        "desktop",
        "journal",
        "strategy",
    }
)


@dataclass(frozen=True)
class Settings:
    toolsets: frozenset[str]
    extra_tools: frozenset[str]
    read_only: bool
    cache_dir: Path
    chart_dir: Path
    journal_dir: Path
    strategy_dir: Path
    max_bars: int
    oanda_api_key: str | None
    oanda_env: str  # "practice" | "live"
    session_id: str | None  # TV_SESSIONID cookie for the opt-in `session` toolset
    cdp_url: str = "http://127.0.0.1:9222"  # TV_CDP_URL for the opt-in `desktop` toolset

    def toolset_enabled(self, name: str) -> bool:
        return name in self.toolsets


def _parse_toolsets(raw: str) -> frozenset[str]:
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    if not parts:
        return DEFAULT_TOOLSETS
    result: set[str] = set()
    for p in parts:
        if p == "default":
            result |= DEFAULT_TOOLSETS
        elif p == "all":
            result |= ALL_TOOLSETS
        elif p in ALL_TOOLSETS:
            result.add(p)
        # unknown names are ignored silently: forward-compat with future toolsets
    return frozenset(result) if result else DEFAULT_TOOLSETS


def load_settings(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    cache_dir = Path(
        e.get("TV_CACHE_DIR", str(Path.home() / ".tvmcp" / "cache"))
    ).expanduser()
    chart_dir = Path(
        e.get("TV_CHART_DIR", str(Path.home() / ".tvmcp" / "charts"))
    ).expanduser()
    journal_dir = Path(
        e.get("TV_JOURNAL_DIR", str(Path.home() / ".tvmcp" / "journal"))
    ).expanduser()
    strategy_dir = Path(
        e.get("TV_STRATEGY_DIR", str(Path.home() / ".tvmcp" / "strategies"))
    ).expanduser()
    return Settings(
        toolsets=_parse_toolsets(e.get("TV_TOOLSETS", "default")),
        extra_tools=frozenset(
            t.strip() for t in e.get("TV_TOOLS", "").split(",") if t.strip()
        ),
        read_only=e.get("TV_READ_ONLY", "") in ("1", "true", "yes"),
        cache_dir=cache_dir,
        chart_dir=chart_dir,
        journal_dir=journal_dir,
        strategy_dir=strategy_dir,
        max_bars=int(e.get("TV_MAX_BARS", "5000")),
        oanda_api_key=e.get("OANDA_API_KEY") or None,
        oanda_env=e.get("OANDA_ENV", "practice"),
        session_id=e.get("TV_SESSIONID") or None,
        cdp_url=e.get("TV_CDP_URL", "http://127.0.0.1:9222"),
    )
