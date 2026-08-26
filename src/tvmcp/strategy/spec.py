"""Declarative strategy spec loading + validation.

A strategy spec is a YAML file that composes vetted built-in primitives ONLY:
`strategy` must name a strategy in `backtest.strategies.STRATEGIES`, and `params`
must be its class-attribute parameters. No user/LLM code is ever executed - the
sandboxed Python escape hatch is a deferred M5 item (see docs/PLAN.md). The `name`
and `symbol` fields are authoritative (symbol is managed by the tool).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..backtest.strategies import STRATEGIES

_REQUIRED = ("name", "strategy")


def load_spec(path: Path) -> dict:
    """Load + validate one strategy YAML. Raises ValueError with an actionable message."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: spec must be a YAML mapping at the top level")
    for key in _REQUIRED:
        if not data.get(key):
            raise ValueError(f"{path.name}: missing required key {key!r}")
    name = str(data["name"]).strip()
    strategy = str(data["strategy"]).strip()
    if not name:
        raise ValueError(f"{path.name}: name cannot be empty")
    if strategy not in STRATEGIES:
        raise ValueError(f"{path.name}: unknown strategy {strategy!r}; available: {sorted(STRATEGIES)}")
    params = data.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(f"{path.name}: params must be a mapping")
    if "symbol" in params:
        raise ValueError(f"{path.name}: 'symbol' is managed by the tool, remove it from params")
    return {
        "name": name,
        "description": str(data.get("description") or "").strip() or None,
        "strategy": strategy,
        "params": dict(params),
    }