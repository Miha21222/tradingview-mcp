import pytest

from tvmcp.symbols import resolve, resolve_timeframe


@pytest.mark.parametrize(
    "alias", ["EURUSD", "eurusd", "OANDA:EURUSD", "EUR_USD", "EUR/USD", "eur-usd"]
)
def test_fx_aliases_resolve_identically(alias):
    s = resolve(alias)
    assert s.canonical == "EURUSD"
    assert s.kind == "fx"
    assert s.tv == "OANDA:EURUSD"
    assert s.dukascopy == "eurusd"
    assert s.oanda == "EUR_USD"


def test_jpy_pair():
    s = resolve("USDJPY")
    assert s.oanda == "USD_JPY"


def test_metal():
    s = resolve("XAUUSD")
    assert s.kind == "metal"
    assert s.dukascopy == "xauusd"


def test_exchange_prefix_preserved():
    s = resolve("FX:EURUSD")
    assert s.tv == "FX:EURUSD"


def test_non_fx_passthrough():
    s = resolve("NASDAQ:AAPL")
    assert s.kind == "other"
    assert s.tv == "NASDAQ:AAPL"
    assert s.dukascopy is None


@pytest.mark.parametrize(
    "raw,canonical,minutes",
    [("M15", "M15", 15), ("15m", "M15", 15), ("15", "M15", 15), ("1h", "H1", 60),
     ("4H", "H4", 240), ("D", "D1", 1440), ("daily", "D1", 1440)],
)
def test_timeframes(raw, canonical, minutes):
    tf = resolve_timeframe(raw)
    assert tf.canonical == canonical
    assert tf.minutes == minutes


def test_unknown_timeframe_raises():
    with pytest.raises(ValueError):
        resolve_timeframe("7m")
