"""Golden-image test: deterministic render must byte-match the committed reference.

Marked `browser` (excluded by default). Runs headless Chromium with a fixed
viewport, DPR=1, UTC times, Arial font, and no animation. If rendering changes in a
way that matters, this fails; regenerate the reference with
`python -m tests.regenerate_chart_golden` (or the script in this file's docstring).
"""

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tvmcp.chart.markup import Markup
from tvmcp.chart.renderer import build_spec, render_png

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "chart_golden"
GOLDEN_HASH = "feeef5ce91c1b8c572d65cd91aa5ee90b4b4af42e2a3545d67f841e7c9122ada"


def golden_df() -> pd.DataFrame:
    n = 90
    times = pd.date_range("2026-08-01", periods=n, freq="h", tz="UTC")
    rng = np.random.RandomState(42)
    trend = 1.1000 + np.linspace(0, 0.02, n) + 0.004 * np.sin(np.linspace(0, 6 * np.pi, n)) + rng.normal(0, 0.001, n)
    op = np.roll(trend, 1)
    cl = trend
    return pd.DataFrame(
        {
            "time": times,
            "open": op,
            "high": np.maximum(op, cl) + 0.0025,
            "low": np.minimum(op, cl) - 0.0025,
            "close": cl,
            "volume": 100 + rng.rand(n) * 50,
        }
    )


def golden_markup() -> Markup:
    return Markup.model_validate(
        {
            "version": 1,
            "grid": True,
            "markup": [
                {"type": "fvg", "time": "2026-08-01T06:00:00Z", "direction": "bullish", "top": 1.1040, "bottom": 1.1010},
                {"type": "ob", "time": "2026-08-02T02:00:00Z", "direction": "bearish", "top": 1.1000, "bottom": 1.0960},
                {"type": "bos", "time": "2026-08-03T08:00:00Z", "level": 1.1080, "label": "BOS"},
                {"type": "killzone", "start": "2026-08-02T06:00:00Z", "end": "2026-08-02T09:00:00Z", "label": "London"},
            ],
        }
    )


@pytest.mark.browser
def test_golden_render(tmp_path):
    spec = build_spec(golden_df(), golden_markup(), 1000, 600)
    # Warm-up render first: Chromium caches fonts/GPU state on first launch, which
    # otherwise makes the byte-hash flaky. The measured render is the second one.
    warm = tmp_path / "warm.png"
    render_png(spec, warm, dpr=1.0)
    out = tmp_path / "render.png"
    render_png(spec, out, dpr=1.0)
    h = hashlib.sha256(out.read_bytes()).hexdigest()
    assert out.stat().st_size > 5000, "render suspiciously small - chart may be blank"
    assert h == GOLDEN_HASH, (
        f"render hash changed ({h}). If the change is intended, regenerate the golden "
        f"reference and update GOLDEN_HASH."
    )
    # reference file exists for eyeballing
    assert (GOLDEN_DIR / "expected.png").exists()
