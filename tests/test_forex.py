"""Table-driven tests for pure forex/metal math (no I/O, no framework)."""

import pytest

from tvmcp.backtest import forex


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("EURUSD", 0.0001),
        ("GBPUSD", 0.0001),
        ("USDJPY", 0.01),
        ("EURJPY", 0.01),
        ("XAUUSD", 0.1),
        ("XAGUSD", 0.01),
    ],
)
def test_pip_size(symbol, expected):
    assert forex.pip_size(symbol) == pytest.approx(expected)


@pytest.mark.parametrize(
    "symbol,expected",
    [("EURUSD", 100_000), ("USDJPY", 100_000), ("XAUUSD", 100), ("XAGUSD", 5000)],
)
def test_contract_size(symbol, expected):
    assert forex.contract_size(symbol) == expected


def test_pip_value_per_lot_same_currency():
    assert forex.pip_value_per_lot("EURUSD") == pytest.approx(10.0)  # $10/pip/lot
    assert forex.pip_value_per_lot("USDJPY") == pytest.approx(1000.0)  # 1000 JPY/pip/lot


def test_pip_value_per_lot_requires_explicit_rate_for_jpy():
    # USD account on USDJPY: must pass 1/USDJPY, not assume 1.0
    v = forex.pip_value_per_lot("USDJPY", quote_to_account_rate=1 / 150.0)
    assert v == pytest.approx(1000 / 150.0, rel=1e-9)


def test_risk_size_independent_of_rate():
    # size = risk / (stop_pips * pip_size); same for any quote-account pairing
    s_eur = forex.risk_size("EURUSD", 100.0, 20.0)
    s_jpy = forex.risk_size("USDJPY", 100.0, 20.0)
    assert s_eur == pytest.approx(100 / (20 * 0.0001))
    assert s_jpy == pytest.approx(100 / (20 * 0.01))


def test_risk_size_full_stop_costs_risk():
    # for EURUSD, size such that a 20-pip move = $100
    size = forex.risk_size("EURUSD", 100.0, 20.0)
    assert 20 * 0.0001 * size == pytest.approx(100.0)


def test_risk_size_rejects_bad_inputs():
    with pytest.raises(ValueError):
        forex.risk_size("EURUSD", 0.0, 20.0)
    with pytest.raises(ValueError):
        forex.risk_size("EURUSD", 100.0, 0.0)


def test_position_units():
    # units = size / rate
    size = forex.risk_size("USDJPY", 100.0, 20.0)
    units = forex.position_units("USDJPY", 100.0, 20.0, quote_to_account_rate=1 / 150.0)
    assert units == pytest.approx(size / (1 / 150.0))


def test_spread_price():
    assert forex.spread_price(2.0, "EURUSD") == pytest.approx(0.0002)
    assert forex.spread_price(2.0, "USDJPY") == pytest.approx(0.02)


def test_r_multiple_long():
    assert forex.r_multiple(1.10, 1.12, 1.09) == pytest.approx(2.0)
    assert forex.r_multiple(1.10, 1.095, 1.09) == pytest.approx(-0.5)


def test_r_multiple_short():
    assert forex.r_multiple(1.10, 1.08, 1.11) == pytest.approx(2.0)
    assert forex.r_multiple(1.10, 1.105, 1.11) == pytest.approx(-0.5)


def test_r_multiple_rejects_zero_risk():
    with pytest.raises(ValueError):
        forex.r_multiple(1.10, 1.12, 1.10)


@pytest.mark.parametrize(
    "hour,expected",
    [
        (2, ["Asian kill zone", "Tokyo", "Sydney"]),
        (7, ["London open kill zone", "London", "Tokyo"]),
        (12, ["New York kill zone", "London"]),
        (15, ["London close kill zone", "London", "New York"]),
        (20, ["New York"]),
        (23, ["Sydney"]),
    ],
)
def test_active_sessions(hour, expected):
    got = set(forex.active_sessions(hour))
    assert got == set(expected)


@pytest.mark.parametrize(
    "hour,expected",
    [(2, "Asian kill zone"), (7, "London open kill zone"), (12, "New York kill zone"), (15, "London close kill zone"), (17, None)],
)
def test_killzone(hour, expected):
    assert forex.killzone(hour) == expected
