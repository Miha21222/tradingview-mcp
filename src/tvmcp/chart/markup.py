"""markup_json schema + validation for tv_chart_render.

Compact, versioned spec: candles come from the symbol/timeframe; `markup` is a list
of typed primitives anchored to bar times. All times are ISO-8601 UTC strings in the
user-facing schema; the renderer converts them (and bars) to Unix seconds for the
browser so output is deterministic across timezones.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Union

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, field_validator, model_validator

MARKUP_VERSION = 1

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _check_color(v: str | None) -> str | None:
    if v is not None and not _HEX_COLOR.match(v):
        raise ValueError(f"color must be a hex #rrggbb string, got {v!r}")
    return v


class BoxMarkup(BaseModel):
    """FVG or order-block rectangle anchored at a bar."""

    type: Literal["fvg", "ob"]
    time: str
    direction: Literal["bullish", "bearish"]
    top: float
    bottom: float
    color: str | None = None  # hex #rrggbb; overrides the direction color
    label: str | None = None

    _color = field_validator("color")(_check_color)

    @field_validator("top", "bottom")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("top/bottom must be > 0")
        return v

    @model_validator(mode="after")
    def _top_ge_bottom(self) -> "BoxMarkup":
        if self.top < self.bottom:
            raise ValueError("top must be >= bottom")
        return self


class LineMarkup(BaseModel):
    """Horizontal level line (BOS/CHoCH or generic) anchored at a bar."""

    type: Literal["line", "bos", "choch"]
    time: str
    level: float
    label: str | None = None
    color: str | None = None  # hex #rrggbb; overrides the type color

    _color = field_validator("color")(_check_color)

    @field_validator("level")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("level must be > 0")
        return v


class KillzoneMarkup(BaseModel):
    """Vertical session/killzone band between two times."""

    type: Literal["killzone"]
    start: str
    end: str
    label: str | None = None
    color: str | None = None  # hex #rrggbb; overrides the default blue

    _color = field_validator("color")(_check_color)


class TextMarkup(BaseModel):
    """Free text annotation anchored at a bar time + price."""

    type: Literal["text"]
    time: str
    price: float
    text: str = Field(min_length=1, max_length=80)
    color: str | None = None  # hex #rrggbb

    _color = field_validator("color")(_check_color)

    @field_validator("price")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price must be > 0")
        return v


class MarkerMarkup(BaseModel):
    """Up/down arrow marker at a bar time + price (e.g. entry/exit points)."""

    type: Literal["marker"]
    time: str
    price: float
    direction: Literal["up", "down"]
    label: str | None = None
    color: str | None = None  # hex #rrggbb; default green (up) / red (down)

    _color = field_validator("color")(_check_color)

    @field_validator("price")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price must be > 0")
        return v


MarkupItem = Annotated[
    Union[BoxMarkup, LineMarkup, KillzoneMarkup, TextMarkup, MarkerMarkup],
    Field(discriminator="type"),
]


class Markup(BaseModel):
    version: int = MARKUP_VERSION
    grid: bool = True
    markup: list[MarkupItem] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != MARKUP_VERSION:
            raise ValueError(f"unsupported markup version {v}; expected {MARKUP_VERSION}")
        return v


def parse_markup(raw: str) -> Markup:
    """Parse + validate a markup_json string. Raises ToolError with an actionable message."""
    if not raw or not raw.strip():
        return Markup()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"markup_json is not valid JSON: {exc}") from exc
    try:
        return Markup.model_validate(data)
    except Exception as exc:
        raise ToolError(f"invalid markup_json: {exc}") from exc
