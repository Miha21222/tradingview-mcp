import pandas as pd

from tvmcp.cache import BAR_COLUMNS, BarCache


def _bars(times):
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times, utc=True),
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 100.0,
        }
    )[BAR_COLUMNS]


def test_store_and_slice_roundtrip(tmp_path):
    cache = BarCache(tmp_path)
    cache.store("test", "EURUSD", "M15", _bars(["2026-01-01 00:00", "2026-01-01 00:15"]))
    df = cache.load("test", "EURUSD", "M15")
    assert len(df) == 2


def test_merge_dedupes_on_timestamp(tmp_path):
    cache = BarCache(tmp_path)
    cache.store("test", "EURUSD", "M15", _bars(["2026-01-01 00:00", "2026-01-01 00:15"]))
    merged = cache.store(
        "test", "EURUSD", "M15", _bars(["2026-01-01 00:15", "2026-01-01 00:30"])
    )
    assert len(merged) == 3
    assert merged["time"].is_monotonic_increasing


def test_slice_window(tmp_path):
    cache = BarCache(tmp_path)
    cache.store(
        "test", "EURUSD", "M15",
        _bars(["2026-01-01 00:00", "2026-01-01 00:15", "2026-01-01 00:30"]),
    )
    df = cache.slice(
        "test", "EURUSD", "M15",
        start=pd.Timestamp("2026-01-01 00:10", tz="UTC"),
        end=pd.Timestamp("2026-01-01 00:20", tz="UTC"),
    )
    assert len(df) == 1


def test_empty_load(tmp_path):
    cache = BarCache(tmp_path)
    assert cache.load("test", "NOPE", "M15").empty
