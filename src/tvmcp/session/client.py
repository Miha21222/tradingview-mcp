"""Live TradingView session client for the opt-in `session` toolset.

Auth: the owner's `sessionid` cookie (TV_SESSIONID) is exchanged for a websocket
JWT via the JSON endpoint `https://www.tradingview.com/quote_token/` (an API call,
not page scraping - allowed surface per CLAUDE.md rule 1; `/accounts/current/` is
dead, 404 as of 2026-08-26). The JWT is then fed to `tvdatafeed-enhanced`'s
TvDatafeed.

The sessionid and JWT are secrets: never logged, never returned in tool output,
never written anywhere except tvdatafeed's own token cache (JWT only, in the
user's home). All calls are read-only.
"""

from __future__ import annotations

import httpx
import pandas as pd

from fastmcp.exceptions import ToolError

from ..symbols import Symbol, Timeframe

_TOKEN_URL = "https://www.tradingview.com/quote_token/"
_HEADERS = {"Referer": "https://www.tradingview.com", "User-Agent": "Mozilla/5.0"}

# tvmcp canonical timeframe -> tvdatafeed Interval name
_INTERVALS = {
    "M1": "in_1_minute",
    "M5": "in_5_minute",
    "M15": "in_15_minute",
    "M30": "in_30_minute",
    "H1": "in_1_hour",
    "H4": "in_4_hour",
    "D1": "in_daily",
}


def fetch_auth_token(session_id: str) -> str:
    """Exchange the sessionid cookie for a websocket auth JWT.

    Raises ToolError with an actionable message on any failure (expired cookie,
    network trouble, unexpected payload shape).
    """
    try:
        r = httpx.get(
            _TOKEN_URL,
            cookies={"sessionid": session_id},
            headers=_HEADERS,
            timeout=15,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise ToolError(f"Could not reach tradingview.com token endpoint: {exc}") from exc
    if r.status_code != 200:
        raise ToolError(
            f"TradingView rejected the session cookie (HTTP {r.status_code}). "
            "TV_SESSIONID is likely expired - re-copy the `sessionid` cookie from a "
            "logged-in browser and update the environment variable."
        )
    try:
        data = r.json()
    except ValueError as exc:
        raise ToolError(
            "Unexpected non-JSON response from the token endpoint; the session "
            "cookie may be invalid or TradingView changed the endpoint."
        ) from exc
    token = _find_auth_token(data)
    if not token:
        raise ToolError(
            "No auth token in the token payload - the session cookie authenticated "
            "but the response shape changed; see session/client.py"
        )
    return token


def _find_auth_token(data, _top: bool = True) -> str | None:
    # observed shape: a bare JSON string; tolerate {"token": ...}/nested dicts too.
    # Only a top-level string counts as the token itself - nested strings must sit
    # under a recognized key, or any dict value would false-positive.
    if _top and isinstance(data, str) and data:
        return data
    if isinstance(data, dict):
        for key in ("token", "auth_token", "authToken"):
            v = data.get(key)
            if isinstance(v, str) and v:
                return v
        for v in data.values():
            if isinstance(v, dict):
                found = _find_auth_token(v, _top=False)
                if found:
                    return found
    return None


class SessionClient:
    """Lazy tvdatafeed-enhanced wrapper authenticated via the sessionid cookie."""

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._tv = None

    def _client(self):
        if self._tv is None:
            from tvDatafeed import TvDatafeed

            token = fetch_auth_token(self._session_id)
            # TvDatafeed() logs "Using anonymous access" at construction because no
            # username/password is given - ignore it: the cookie-derived JWT below
            # replaces the anonymous token before any data call.
            tv = TvDatafeed()
            tv.token = token
            self._tv = tv
        return self._tv

    def get_bars(self, sym: Symbol, tf: Timeframe, count: int) -> pd.DataFrame:
        """Fetch the latest `count` bars for `sym`/`tf` through the TV websocket.

        Returns the tvmcp bar frame (time/open/high/low/close/volume, UTC).
        """
        from tvDatafeed import Interval

        interval = Interval[_INTERVALS[tf.canonical]]
        exchange, _, ticker = sym.tv.partition(":")
        df = self._client().get_hist(
            symbol=ticker or sym.canonical,
            exchange=exchange or "OANDA",
            interval=interval,
            n_bars=count,
        )
        if df is None or df.empty:
            raise ToolError(
                f"TradingView returned no bars for {sym.tv} {tf.canonical}. The "
                "symbol may not exist on this feed, or the session token lost "
                "realtime permission."
            )
        out = df.reset_index()
        # tvdatafeed columns: datetime (exchange-naive UTC), symbol, open..volume
        time_col = "datetime" if "datetime" in out.columns else out.columns[0]
        out = out.rename(columns={time_col: "time"})
        out["time"] = pd.to_datetime(out["time"], utc=True)
        return out[["time", "open", "high", "low", "close", "volume"]]

    def get_quote(self, sym: Symbol) -> dict:
        """Latest close-based quote derived from a 2-bar M1 fetch (no extra API)."""
        from ..symbols import resolve_timeframe

        df = self.get_bars(sym, resolve_timeframe("M1"), 2)
        last = df.iloc[-1]
        return {
            "time": pd.Timestamp(last["time"]).isoformat().replace("+00:00", "Z"),
            "close": round(float(last["close"]), 6),
            "open": round(float(last["open"]), 6),
            "high": round(float(last["high"]), 6),
            "low": round(float(last["low"]), 6),
            "volume": float(last["volume"]),
        }
