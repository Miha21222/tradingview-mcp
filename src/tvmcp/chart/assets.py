"""Locate the chart static assets (index.html, app.js, vendor bundle)."""

from __future__ import annotations

from pathlib import Path


def static_dir() -> Path:
    return Path(__file__).parent / "static"


def index_html() -> Path:
    return static_dir() / "index.html"
