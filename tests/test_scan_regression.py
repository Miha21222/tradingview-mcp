"""M1 trust gate: regression suite over labeled setups in tests/fixtures/labeled_setups/.

Gates the pinned `smartmoneyconcepts` version: each fixture pins a fixed bar set +
detector params + expected detection output. A dependency bump that changes
detection (or an adapter regression) fails the suite.

Fixtures authored as `kind: "synthetic"` lock the library's current behaviour;
the owner backfills real hand-labeled setups (`kind: "labeled"`) from their own
charts to gate detection correctness itself. Every fixture runs offline.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from tvmcp.scan import detectors

FIXTURES = Path(__file__).parent / "fixtures" / "labeled_setups"

_DETECTORS = {
    "fvg": detectors.scan_fvg,
    "ob": detectors.scan_ob,
    "structure": detectors.scan_structure,
    "liquidity": detectors.scan_liquidity,
    "sessions": detectors.scan_sessions,
    "prev_hl": detectors.scan_prev_hl,
}


def _frame(bars: list) -> pd.DataFrame:
    df = pd.DataFrame(
        [{"time": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]} for b in bars]
    )
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def _assert_close(actual, expected) -> None:
    if isinstance(expected, dict):
        assert set(actual.keys()) == set(expected.keys())
        for k in expected:
            _assert_close(actual[k], expected[k])
    elif isinstance(expected, list):
        assert len(actual) == len(expected), f"len {len(actual)} != {len(expected)}"
        for a, e in zip(actual, expected):
            _assert_close(a, e)
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-6, abs=1e-9), f"{actual} != {expected}"
    elif isinstance(expected, bool):
        assert bool(actual) == expected
    else:
        assert actual == expected, f"{actual!r} != {expected!r}"


def _all_fixtures():
    return sorted(str(p) for p in FIXTURES.glob("*.json"))


def test_fixtures_present():
    assert _all_fixtures(), "no labeled setups found - regression gate is empty"


@pytest.mark.parametrize("path", _all_fixtures())
def test_fixture(path):
    fx = json.loads(Path(path).read_text(encoding="utf-8"))
    assert fx["id"] == Path(path).stem
    detector = _DETECTORS.get(fx["detector"])
    assert detector is not None, f"no detector for {fx['detector']!r}"
    df = _frame(fx["bars"])
    actual = detector(df, **fx["params"])
    _assert_close(actual, fx["expected"])
