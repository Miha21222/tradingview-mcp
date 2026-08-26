"""Pure forex / metal math used by the `backtest` toolset.

No I/O, no framework. All functions are deterministic and table-testable. The
account-currency conversion is explicit: `quote_to_account_rate` defaults to 1.0
(valid only when the account currency equals the quote currency); callers whose
account differs MUST pass the rate (e.g. 1/USDJPY for a USD account on USDJPY) -
there is no universal pip-value formula.
"""

from __future__ import annotations

from ..symbols import resolve

# Pip size per quote currency / metal base. FX default is 0.0001; JPY quote is 0.01.
_METAL_PIP = {"XAU": 0.1, "XAG": 0.01}
_CONTRACT = {"XAU": 100, "XAG": 5000}  # ounces per standard lot

# Sessions as fixed UTC hour windows (matching the pinned scan library's windows,
# which are NOT DST-aware). (start, end) with wrap-around when start > end.
SESSIONS: dict[str, tuple[int, int]] = {
    "Sydney": (21, 6),
    "Tokyo": (0, 9),
    "London": (7, 16),
    "New York": (13, 22),
    "Asian kill zone": (0, 4),
    "London open kill zone": (6, 9),
    "New York kill zone": (11, 14),
    "London close kill zone": (14, 16),
}
_KILLZONES = ("Asian kill zone", "London open kill zone", "New York kill zone", "London close kill zone")


def pip_size(symbol: str) -> float:
    """Price units of one pip for `symbol` (0.0001, JPY 0.01, XAU 0.1, XAG 0.01)."""
    sym = resolve(symbol)
    if sym.kind == "fx":
        return 0.01 if sym.canonical.endswith("JPY") else 0.0001
    if sym.kind == "metal":
        return _METAL_PIP.get(sym.canonical[:3], 0.01)
    raise ValueError(f"{symbol!r} (kind={sym.kind}) has no defined pip size")


def contract_size(symbol: str) -> float:
    """Units per standard lot: 100k FX, 100 oz XAU, 5000 oz XAG."""
    sym = resolve(symbol)
    if sym.kind == "fx":
        return 100_000
    if sym.kind == "metal":
        return _CONTRACT.get(sym.canonical[:3], 100)
    raise ValueError(f"{symbol!r} (kind={sym.kind}) has no defined contract size")


def pip_value_per_lot(symbol: str, quote_to_account_rate: float = 1.0) -> float:
    """Value of one pip per standard lot in account currency.

    Default rate 1.0 is only correct when the account currency == quote currency.
    """
    return pip_size(symbol) * contract_size(symbol) * quote_to_account_rate


def risk_size(symbol: str, risk_amount: float, stop_pips: float) -> float:
    """Position `size` (backtesting.py units) that risks `risk_amount` over `stop_pips`.

    The result is independent of the quote-to-account rate because backtesting.py
    prices P&L in its single account currency: setting size = risk / (stop_pips *
    pip_size) makes a full stop-loss move cost exactly `risk_amount` account units.
    """
    if risk_amount <= 0:
        raise ValueError("risk_amount must be > 0")
    if stop_pips <= 0:
        raise ValueError("stop_pips must be > 0")
    return risk_amount / (stop_pips * pip_size(symbol))


def position_units(symbol: str, risk_amount: float, stop_pips: float, quote_to_account_rate: float = 1.0) -> float:
    """Raw instrument units for a risk-based position (for reporting lots)."""
    return risk_size(symbol, risk_amount, stop_pips) / quote_to_account_rate


def spread_price(spread_pips: float, symbol: str) -> float:
    """The spread expressed in price units (for reference/reporting, not for backtesting)."""
    return spread_pips * pip_size(symbol)


def spread_relative(spread_pips: float, symbol: str, ref_price: float) -> float:
    """Relative spread rate for backtesting.py, exact at `ref_price`.

    backtesting.py applies `spread` multiplicatively on the ENTRY fill only
    (`price * (1 + spread)`); exits (SL/TP/close) are filled unadjusted. So a round
    trip costs `spread * price * size` in account currency. Setting
    `spread = pips * pip_size / ref_price` makes that equal the intended
    `pips * pip_size * size` round-trip cost, including for JPY-quoted pairs. Exact
    when the entry fills at `ref_price`; an approximation otherwise.
    """
    if ref_price <= 0:
        raise ValueError("ref_price must be > 0")
    return spread_pips * pip_size(symbol) / ref_price


def quote_currency(symbol: str) -> str | None:
    """Quote currency (last 3 chars of a 6-char FX/metal canonical), else None."""
    canonical = resolve(symbol).canonical
    return canonical[3:] if len(canonical) >= 6 else None


def r_multiple(entry: float, exit: float, stop: float) -> float:
    """Signed R-multiple for a trade given entry, exit and stop price.

    R = profit / initial risk. Positive when the trade profited, negative when it
    lost more than one risk unit. Works for both long (stop < entry) and short
    (stop > entry).
    """
    if stop == entry:
        raise ValueError("stop cannot equal entry (infinite risk)")
    if stop < entry:  # long
        risk = entry - stop
        return (exit - entry) / risk
    # short
    risk = stop - entry
    return (entry - exit) / risk


def active_sessions(hour_utc: int) -> list[str]:
    """Names of all sessions active at the given UTC hour (0-23)."""
    h = hour_utc % 24
    out = []
    for name, (start, end) in SESSIONS.items():
        if start <= end:
            if start <= h < end:
                out.append(name)
        else:  # overnight window
            if h >= start or h < end:
                out.append(name)
    return out


def killzone(hour_utc: int) -> str | None:
    """First active killzone at `hour_utc`, else None."""
    for name in _KILLZONES:
        if name in active_sessions(hour_utc):
            return name
    return None
