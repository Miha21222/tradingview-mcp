"""Launch TradingView Desktop with the CDP flag (`tv_desktop_launch`, M8).

The desktop tier needs the app started with `--remote-debugging-port=<port>`;
a copy started normally (Start menu, autostart) is single-instance and a second
launch only focuses it. This module:

1. asks the port first (`/json/version`): if it answers and `/json/list` has a
   TradingView tab, the app is already up (`already_running`); if it answers
   but nothing there is TradingView, another CDP app owns the port and the
   caller must pick another `TV_CDP_URL` (we never spawn into a busy port -
   Chromium silently skips binding it and the app would run with no CDP);
2. refuses when a flagless `TradingView.exe` is running - closing the user's
   app is their call; the error carries the doctor's Stop-Process fix text;
3. finds the executable (`TV_DESKTOP_EXE` env → Store package via
   `Get-AppxPackage` → `%LOCALAPPDATA%\\Programs\\TradingView` → macOS
   `/Applications/TradingView.app`) and spawns it detached with the flag;
4. polls `/json/version` every 0.5 s, then `/json/list` for a chart tab
   (`chart_tab: false` while the user is still logging in).

Deliberately NOT used: AUMID / `shell:AppsFolder` activation
(`explorer shell:AppsFolder\\TradingView.Desktop_...!App`). It starts the Store
app cleanly but cannot pass command-line flags, so the app would come up
without CDP - exactly the state we are trying to avoid.

Every function that touches the OS/network is module-level and small so tests
monkeypatch them (no real spawn, no real HTTP).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError

from ..config import Settings
from ..doctor import flagless_fix

DEFAULT_PORT = 9223
_POLL_S = 0.5


def port_from_url(cdp_url: str) -> int:
    u = urlparse(cdp_url if "://" in cdp_url else "http://" + cdp_url)
    return u.port or DEFAULT_PORT


# --- OS / network touchpoints (monkeypatched in tests) ---------------------------

def _http_json(url: str, timeout: float = 2.0):
    """GET `url` and decode JSON; None when the port is closed or answers junk."""
    import httpx

    try:
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _tv_process_running() -> bool:
    from ..doctor import _tv_desktop_running

    return _tv_desktop_running()


def _powershell(cmd: str) -> str:
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
    except Exception:
        return ""


def _exists(path: str) -> bool:
    return os.path.exists(path)


def _spawn(exe: str, port: int) -> int:
    """Start the app detached with the CDP flag; returns the pid."""
    flag = f"--remote-debugging-port={port}"
    if sys.platform == "darwin" and exe.endswith(".app"):
        proc = subprocess.Popen(["open", "-a", exe, "--args", flag],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return proc.pid
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    proc = subprocess.Popen([exe, flag], **kwargs)
    return proc.pid


_sleep = time.sleep
_now = time.monotonic


# --- discovery ----------------------------------------------------------------------

def find_exe(settings: Settings) -> tuple[str | None, list[str]]:
    """(path, tried) - the first existing candidate in the documented order."""
    tried: list[str] = []
    if settings.desktop_exe:
        tried.append(f"TV_DESKTOP_EXE={settings.desktop_exe}")
        if _exists(settings.desktop_exe):
            return settings.desktop_exe, tried
    if sys.platform == "win32":
        loc = _powershell("(Get-AppxPackage -Name 'TradingView.Desktop').InstallLocation")
        if loc:
            cand = os.path.join(loc, "TradingView.exe")
            tried.append(cand)
            if _exists(cand):
                return cand, tried
        else:
            tried.append("Get-AppxPackage TradingView.Desktop (not installed)")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cand = os.path.join(local, "Programs", "TradingView", "TradingView.exe")
            tried.append(cand)
            if _exists(cand):
                return cand, tried
    if sys.platform == "darwin":
        cand = "/Applications/TradingView.app"
        tried.append(cand)
        if _exists(cand):
            return cand, tried
    return None, tried


# --- probes ---------------------------------------------------------------------------

def _version(base: str) -> dict | None:
    v = _http_json(base + "/json/version")
    return v if isinstance(v, dict) else None


def _targets(base: str) -> list[dict]:
    t = _http_json(base + "/json/list")
    return [x for x in t if isinstance(x, dict)] if isinstance(t, list) else []


def _is_tv(targets: list[dict]) -> bool:
    return any("tradingview.com" in (t.get("url") or "") for t in targets)


def _has_chart(targets: list[dict]) -> bool:
    return any(t.get("type") == "page" and "tradingview.com/chart" in (t.get("url") or "")
               for t in targets)


# --- entry point ------------------------------------------------------------------------

def launch(settings: Settings, wait_seconds: int = 30) -> dict:
    base = settings.cdp_url.rstrip("/")
    port = port_from_url(settings.cdp_url)
    ver = _version(base)
    if ver is not None:
        targets = _targets(base)
        if not _is_tv(targets):
            raise ToolError(
                f"Port {port} is already owned by another CDP app ({ver.get('Browser', 'unknown')}) "
                f"- not TradingView Desktop. Set TV_CDP_URL to a free port (e.g. "
                f"http://127.0.0.1:{port + 1}) and call tv_desktop_launch again; never "
                "launch into a busy port (Chromium skips binding it silently)."
            )
        return {"launched": False, "already_running": True, "port": port,
                "browser": ver.get("Browser"), "chart_tab": _has_chart(targets),
                "waited_ms": 0}
    if _tv_process_running():
        raise ToolError(
            f"TradingView Desktop is running WITHOUT the debug flag (no CDP at {base}). "
            "It is single-instance, so a flagged launch would only focus that copy. "
            "Ask the user before closing it (the layout autosaves), then: "
            + flagless_fix()
        )
    exe, tried = find_exe(settings)
    if exe is None:
        raise ToolError(
            "TradingView Desktop executable not found. Tried: " + "; ".join(tried)
            + ". Set TV_DESKTOP_EXE to the full path of TradingView.exe "
            "(Store: (Get-AppxPackage TradingView.Desktop).InstallLocation) or "
            "install it from https://www.tradingview.com/desktop/."
        )
    try:
        pid = _spawn(exe, port)
    except Exception as exc:
        raise ToolError(
            f"Could not start {exe}: {exc}. Start it by hand with "
            f"--remote-debugging-port={port} (scripts/start-tv-desktop.ps1) or fix "
            "TV_DESKTOP_EXE."
        ) from exc
    t0 = _now()
    deadline = t0 + max(1, int(wait_seconds))
    ver = None
    while _now() < deadline:
        ver = _version(base)
        if ver is not None:
            break
        _sleep(_POLL_S)
    if ver is None:
        raise ToolError(
            f"Started {exe} (pid {pid}) but {base}/json/version did not answer within "
            f"{wait_seconds}s. Another instance may be starting, the port may be blocked, "
            "or the app is slow - check with tv_desktop_status in a moment, or raise "
            "wait_seconds."
        )
    chart = False
    while True:
        chart = _has_chart(_targets(base))
        if chart or _now() >= deadline:
            break
        _sleep(_POLL_S)
    return {
        "launched": True, "exe": exe, "pid": pid, "port": port,
        "browser": ver.get("Browser"), "chart_tab": chart,
        "waited_ms": int((_now() - t0) * 1000),
        "note": None if chart else (
            "CDP is up but no chart tab yet - the user is probably still logging "
            "in; retry tv_desktop_status once a chart is open."
        ),
    }
