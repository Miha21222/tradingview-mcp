from __future__ import annotations

from dataclasses import dataclass


class DataProviderError(RuntimeError):
    """Raised with an actionable message when a provider cannot deliver bars."""


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    available: bool
    detail: str
