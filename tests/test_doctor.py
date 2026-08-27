"""Tests for tv_setup_doctor: shape, fix commands, credential-manual policy."""

import asyncio
import json

from fastmcp import FastMCP

from tvmcp import doctor
from tvmcp.config import Settings


def _settings(tmp_path, **kw) -> Settings:
    return Settings(
        toolsets=frozenset({"public"}),
        extra_tools=frozenset(),
        read_only=False,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=kw.get("journal_dir", tmp_path / "journal"),
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=kw.get("oanda_api_key"),
        oanda_env="practice",
        session_id=kw.get("session_id"),
        cdp_url=kw.get("cdp_url", "http://127.0.0.1:1"),  # nothing listens: cdp check fails fast
    )


def _run(tmp_path, **kw):
    mcp = FastMCP(name="test")
    doctor.register(mcp, _settings(tmp_path, **kw))
    r = asyncio.run(mcp.call_tool("tv_setup_doctor", {}))
    return json.loads(r.content[0].text)


def test_registers_in_default_surface(tmp_path):
    from tvmcp.server import build_server

    s = _settings(tmp_path)
    s = Settings(**{**s.__dict__, "toolsets": frozenset({"public", "data"})})
    names = {t.name for t in asyncio.run(build_server(s).list_tools())}
    assert "tv_setup_doctor" in names


def test_every_broken_check_carries_a_fix(tmp_path):
    data = _run(tmp_path)
    for c in data["checks"]:
        if not c["ok"]:
            assert c.get("fix"), f"broken check {c['name']} has no fix command"


def test_optional_checks_do_not_break_health(tmp_path):
    data = _run(tmp_path)  # no credentials, no CDP, missing dirs - all optional
    assert all(c["optional"] for c in data["checks"] if not c["ok"] and c["name"] not in ("node", "chromium"))
    # required checks are exactly node + chromium
    required = [c["name"] for c in data["checks"] if not c["optional"]]
    assert set(required) == {"node", "chromium"}


def test_credential_fixes_stay_manual(tmp_path):
    data = _run(tmp_path)
    by_name = {c["name"]: c for c in data["checks"]}
    for name in ("oanda", "tv_session"):
        c = by_name[name]
        if not c["ok"]:
            assert "manual" in c["fix"], f"{name} fix must state credentials are manual"


def test_set_credentials_report_ok(tmp_path):
    data = _run(tmp_path, oanda_api_key="k", session_id="s")
    by_name = {c["name"]: c for c in data["checks"]}
    assert by_name["oanda"]["ok"] is True
    assert by_name["tv_session"]["ok"] is True


def test_cdp_down_app_running_says_close_first(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_tv_desktop_running", lambda: True)
    data = _run(tmp_path)
    c = {ch["name"]: ch for ch in data["checks"]}["desktop_cdp"]
    assert c["ok"] is False
    assert "without the debug flag" in c["detail"]
    assert "ask the user" in c["fix"].lower()
    assert "Stop-Process" in c["fix"]


def test_cdp_down_app_absent_plain_launch_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_tv_desktop_running", lambda: False)
    data = _run(tmp_path)
    c = {ch["name"]: ch for ch in data["checks"]}["desktop_cdp"]
    assert c["ok"] is False
    assert "Stop-Process" not in c["fix"]
    assert "start-tv-desktop.ps1" in c["fix"]


def test_cdp_fix_uses_absolute_launcher_path(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_tv_desktop_running", lambda: False)
    data = _run(tmp_path)
    c = {ch["name"]: ch for ch in data["checks"]}["desktop_cdp"]
    # repo checkout: the launcher exists, so the fix must carry its absolute path
    from pathlib import Path
    assert Path(c["fix"].split('"')[1]).is_absolute()


def test_dirs_reported_with_mkdir_fix(tmp_path):
    data = _run(tmp_path)
    by_name = {c["name"]: c for c in data["checks"]}
    assert by_name["journal_dir"]["ok"] is False
    assert by_name["journal_dir"]["fix"].startswith("mkdir")
    (tmp_path / "journal").mkdir()
    data2 = _run(tmp_path, journal_dir=tmp_path / "journal")
    assert {c["name"]: c for c in data2["checks"]}["journal_dir"]["ok"] is True
