"""`tv_setup_doctor`: one tool that checks every prerequisite and hands back the
exact fix command for anything missing.

Design: the server never installs anything itself - a tool call should not spawn
surprise installers. Instead every check returns {ok, detail, fix} where `fix` is
a shell command the calling agent can run directly (or show the user). Credentials
(TV_SESSIONID, OANDA_API_KEY) are deliberately manual: the doctor reports whether
they are set and where to put them, never how to obtain them automatically.

Registered with the always-on `public` toolset so a fresh install can diagnose
itself before anything else works.
"""

from __future__ import annotations

import shutil
from typing import Any

import httpx

from .config import Settings

_WINGET_NODE = "winget install OpenJS.NodeJS.LTS"
_CHROMIUM_FIX = "uv run playwright install chromium"


def _check(name: str, ok: bool, detail: str, fix: str | None = None, optional: bool = False) -> dict:
    entry: dict = {"name": name, "ok": ok, "detail": detail, "optional": optional}
    if fix and not ok:
        entry["fix"] = fix
    return entry


def _node_check() -> dict:
    npx = shutil.which("npx")
    return _check(
        "node",
        npx is not None,
        "npx found - Dukascopy data works" if npx else "npx not on PATH - Dukascopy bar fetches will fail",
        fix=_WINGET_NODE + "   (then restart the terminal so PATH updates)",
    )


def _chromium_check() -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _check(
            "chromium",
            False,
            "playwright package missing - chart rendering unavailable",
            fix="uv sync && " + _CHROMIUM_FIX,
        )
    try:
        p = sync_playwright().start()
        try:
            import os

            path = p.chromium.executable_path
            present = bool(path) and os.path.exists(path)
        finally:
            p.stop()
    except Exception as exc:  # environment-specific launcher failures
        return _check("chromium", False, f"could not probe Playwright: {exc}", fix=_CHROMIUM_FIX)
    return _check(
        "chromium",
        present,
        "headless Chromium installed - tv_chart_render ready" if present
        else "Playwright installed but Chromium browser is not - tv_chart_render will fail",
        fix=_CHROMIUM_FIX,
    )


def _dir_check(name: str, path, purpose: str) -> dict:
    return _check(
        f"{name}_dir",
        path.exists(),
        f"{path} exists" if path.exists() else f"{path} missing - {purpose}",
        fix=f'mkdir "{path}"' if not path.exists() else None,
        optional=True,
    )


def _oanda_check(settings: Settings) -> dict:
    return _check(
        "oanda",
        settings.oanda_api_key is not None,
        "OANDA_API_KEY set - OANDA provider available" if settings.oanda_api_key
        else "OANDA_API_KEY not set - provider falls back to Dukascopy (fine for most uses)",
        fix="get a free practice key at oanda.com, then set the OANDA_API_KEY environment variable (credential - manual on purpose)",
        optional=True,
    )


def _session_check(settings: Settings) -> dict:
    return _check(
        "tv_session",
        settings.session_id is not None,
        "TV_SESSIONID set - `session` toolset usable" if settings.session_id
        else "TV_SESSIONID not set - `session` toolset off (opt-in, ToS risk)",
        fix="copy the sessionid cookie from a logged-in tradingview.com browser tab into the TV_SESSIONID environment variable (credential - manual on purpose)",
        optional=True,
    )


def _cdp_check(settings: Settings) -> dict:
    # A 200 from /json/version is NOT enough: any CDP-speaking app (another
    # Electron tool, a debug browser) could own the port. Only a chart tab in
    # /json/list proves it is TradingView Desktop.
    try:
        r = httpx.get(settings.cdp_url.rstrip("/") + "/json/list", timeout=2)
        r.raise_for_status()
        targets = r.json()
        is_tv = any(
            "tradingview.com" in (t.get("url") or "") for t in targets if isinstance(t, dict)
        )
        ok = is_tv
        detail = (
            "TradingView Desktop CDP reachable (chart tab found)" if is_tv
            else f"a CDP listener answers at {settings.cdp_url} but it is NOT TradingView "
            "Desktop (another app owns the port) - point TV_CDP_URL at the right port"
        )
    except Exception:
        ok = False
        detail = f"no CDP listener at {settings.cdp_url} - `desktop` toolset unavailable"
    return _check(
        "desktop_cdp",
        ok,
        detail,
        fix="powershell -File scripts/start-tv-desktop.ps1   (then set TV_CDP_URL=http://127.0.0.1:9223)",
        optional=True,
    )


def run_checks(settings: Settings) -> list[dict]:
    return [
        _node_check(),
        _chromium_check(),
        _oanda_check(settings),
        _session_check(settings),
        _cdp_check(settings),
        _dir_check("journal", settings.journal_dir, "tv_journal_scan needs it (set TV_JOURNAL_DIR to your FX Replay export folder)"),
        _dir_check("strategy", settings.strategy_dir, "tv_strategy_list reads YAML specs from it"),
    ]


def register(mcp: Any, settings: Settings) -> None:
    @mcp.tool(tags={"public"}, annotations={"readOnlyHint": True, "openWorldHint": False})
    def tv_setup_doctor() -> dict:
        """Diagnose this install: check every prerequisite, return exact fixes.

        Each check reports {name, ok, detail, optional} and - when broken - a `fix`
        holding the exact shell command to run. Run the fixes for anything with
        ok=false and optional=false, rerun the doctor, and the server is fully
        operational. Credential checks (OANDA key, TV session cookie) are optional
        and always require the human - never attempt to obtain credentials.
        """
        checks = run_checks(settings)
        required_broken = [c["name"] for c in checks if not c["ok"] and not c["optional"]]
        return {
            "healthy": not required_broken,
            "needs_attention": required_broken,
            "checks": checks,
        }
