"""M8 desktop-3: `tv_desktop_launch` / desktop.launcher with every OS and network
touchpoint monkeypatched (no spawn, no HTTP, no tasklist).

Covers: already running (chart tab / still logging in), busy port owned by
another CDP app, flagless TradingView.exe refusal with the doctor's fix, exe
discovery order, happy path with polling, timeout, and registration gating.
"""

import asyncio
import json

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import desktop
from tvmcp.config import Settings, load_settings
from tvmcp.desktop import launcher

TV_VERSION = {"Browser": "Chrome/128.0.6613.186", "Protocol-Version": "1.3"}
CHART = [{"id": "1", "type": "page", "url": "https://www.tradingview.com/chart/abc/"}]
LOGIN = [{"id": "1", "type": "page", "url": "https://www.tradingview.com/accounts/signin/"}]


def _settings(tmp_path, cdp_url="http://127.0.0.1:9223", exe=None, read_only=False) -> Settings:
    return Settings(
        toolsets=frozenset({"desktop"}),
        extra_tools=frozenset(),
        read_only=read_only,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=None,
        oanda_env="practice",
        session_id=None,
        cdp_url=cdp_url,
        desktop_exe=exe,
    )


class Net:
    """Scripted /json/version + /json/list answers; `after_spawn` flips them."""

    def __init__(self, version=None, targets=None):
        self.version, self.targets = version, targets or []
        self.spawned = []
        self.after_spawn = None
        self.clock = 0.0
        self.slept = 0.0
        self.version_at = None  # clock time when /json/version starts answering

    def http(self, url, timeout=2.0):
        if self.spawned and self.version_at is not None and self.clock >= self.version_at and self.after_spawn:
            self.version, self.targets = self.after_spawn
        if url.endswith("/json/version"):
            return self.version
        if url.endswith("/json/list"):
            return self.targets
        return None

    def spawn(self, exe, port):
        self.spawned.append((exe, port))
        return 4242

    def sleep(self, s):
        self.slept += s
        self.clock += s


@pytest.fixture
def net(monkeypatch):
    n = Net()
    monkeypatch.setattr(launcher, "_http_json", n.http)
    monkeypatch.setattr(launcher, "_spawn", n.spawn)
    monkeypatch.setattr(launcher, "_sleep", n.sleep)
    monkeypatch.setattr(launcher, "_now", lambda: n.clock)
    monkeypatch.setattr(launcher, "_tv_process_running", lambda: False)
    monkeypatch.setattr(launcher, "_powershell", lambda cmd: "")
    monkeypatch.setattr(launcher, "_exists", lambda p: False)
    return n


def test_port_from_url():
    assert launcher.port_from_url("http://127.0.0.1:9223") == 9223
    assert launcher.port_from_url("http://localhost:9222/") == 9222
    assert launcher.port_from_url("http://127.0.0.1") == 9223
    assert load_settings({}).cdp_url == "http://127.0.0.1:9223"  # default matches the launcher


def test_already_running_with_chart(tmp_path, net):
    net.version, net.targets = TV_VERSION, CHART
    res = launcher.launch(_settings(tmp_path))
    assert res == {"launched": False, "already_running": True, "port": 9223,
                   "browser": TV_VERSION["Browser"], "chart_tab": True, "waited_ms": 0}
    assert net.spawned == []


def test_already_running_still_logging_in(tmp_path, net):
    net.version, net.targets = TV_VERSION, LOGIN
    res = launcher.launch(_settings(tmp_path))
    assert res["already_running"] is True and res["chart_tab"] is False


def test_busy_port_owned_by_other_app_refuses(tmp_path, net):
    net.version = {"Browser": "wmux/1.2 Electron"}
    net.targets = [{"id": "9", "type": "page", "url": "http://localhost:3000/panel"}]
    with pytest.raises(ToolError, match="owned by another CDP app \\(wmux/1.2 Electron\\).*TV_CDP_URL"):
        launcher.launch(_settings(tmp_path))
    assert net.spawned == []


def test_flagless_running_refuses_with_doctor_fix(tmp_path, net, monkeypatch):
    monkeypatch.setattr(launcher, "_tv_process_running", lambda: True)
    with pytest.raises(ToolError) as ei:
        launcher.launch(_settings(tmp_path))
    msg = str(ei.value)
    assert "WITHOUT the debug flag" in msg
    assert "Stop-Process -Name TradingView" in msg and "Ask the user" in msg
    assert net.spawned == []  # never killed, never spawned


def test_exe_not_found_lists_tried(tmp_path, net):
    with pytest.raises(ToolError, match="not found.*TV_DESKTOP_EXE"):
        launcher.launch(_settings(tmp_path, exe=r"C:\nope\TradingView.exe"))


def test_exe_discovery_order(tmp_path, net, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    store = r"C:\Program Files\WindowsApps\TradingView.Desktop_3.4.0_x64__abc"
    monkeypatch.setattr(launcher, "_powershell", lambda cmd: store if "Get-AppxPackage" in cmd else "")
    local = r"C:\Users\u\AppData\Local\Programs\TradingView\TradingView.exe"
    # 1) env wins when it exists
    monkeypatch.setattr(launcher, "_exists", lambda p: p == r"D:\tv\TradingView.exe")
    exe, tried = launcher.find_exe(_settings(tmp_path, exe=r"D:\tv\TradingView.exe"))
    assert exe == r"D:\tv\TradingView.exe"
    # 2) then the Store package
    monkeypatch.setattr(launcher, "_exists", lambda p: p == store + r"\TradingView.exe")
    exe, tried = launcher.find_exe(_settings(tmp_path, exe=r"D:\tv\TradingView.exe"))
    assert exe == store + r"\TradingView.exe" and tried[0].startswith("TV_DESKTOP_EXE=")
    # 3) then %LOCALAPPDATA%\Programs
    monkeypatch.setattr(launcher, "_exists", lambda p: p == local)
    exe, tried = launcher.find_exe(_settings(tmp_path))
    assert exe == local


def test_happy_path_polls_until_chart(tmp_path, net, monkeypatch):
    monkeypatch.setattr(launcher, "_exists", lambda p: p == r"D:\tv\TradingView.exe")
    net.after_spawn = (TV_VERSION, [])
    net.version_at = 1.0  # CDP answers after two polls; chart tab appears later
    calls = {"n": 0}
    real_http = net.http

    def http(url, timeout=2.0):
        r = real_http(url, timeout)
        if url.endswith("/json/list"):
            calls["n"] += 1
            if calls["n"] >= 3:
                net.targets = CHART
                return CHART
        return r

    monkeypatch.setattr(launcher, "_http_json", http)
    res = launcher.launch(_settings(tmp_path, exe=r"D:\tv\TradingView.exe"), wait_seconds=10)
    assert net.spawned == [(r"D:\tv\TradingView.exe", 9223)]
    assert res["launched"] is True and res["pid"] == 4242 and res["port"] == 9223
    assert res["browser"] == TV_VERSION["Browser"] and res["chart_tab"] is True
    assert res["waited_ms"] == int(net.clock * 1000) > 0 and res["note"] is None


def test_happy_path_no_chart_yet_reports_false(tmp_path, net, monkeypatch):
    monkeypatch.setattr(launcher, "_exists", lambda p: p == "/Applications/TradingView.app")
    net.after_spawn = (TV_VERSION, LOGIN)
    net.version_at = 0.0
    res = launcher.launch(_settings(tmp_path, exe="/Applications/TradingView.app"), wait_seconds=2)
    assert res["launched"] is True and res["chart_tab"] is False
    assert "logging" in res["note"]
    assert net.clock >= 2  # kept polling for the chart tab until the deadline


def test_timeout_when_port_never_answers(tmp_path, net, monkeypatch):
    monkeypatch.setattr(launcher, "_exists", lambda p: True)
    with pytest.raises(ToolError, match="did not answer within 3s"):
        launcher.launch(_settings(tmp_path, exe="x.exe"), wait_seconds=3)


def test_spawn_failure_is_actionable(tmp_path, net, monkeypatch):
    monkeypatch.setattr(launcher, "_exists", lambda p: True)

    def boom(exe, port):
        raise PermissionError("access denied")

    monkeypatch.setattr(launcher, "_spawn", boom)
    with pytest.raises(ToolError, match="Could not start x.exe: access denied"):
        launcher.launch(_settings(tmp_path, exe="x.exe"))


def test_tool_registered_write_gated(tmp_path, net):
    net.version, net.targets = TV_VERSION, CHART
    mcp = FastMCP(name="t")
    desktop.register(mcp, _settings(tmp_path))
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "tv_desktop_launch" in names
    r = asyncio.run(mcp.call_tool("tv_desktop_launch", {"wait_seconds": 5}))
    data = json.loads(r.content[0].text)
    assert data["already_running"] is True and data["provider"] == "desktop"
    ro = FastMCP(name="ro")
    desktop.register(ro, _settings(tmp_path, read_only=True))
    assert "tv_desktop_launch" not in {t.name for t in asyncio.run(ro.list_tools())}


def test_doctor_fix_mentions_launch_tool(tmp_path, monkeypatch):
    from tvmcp import doctor

    monkeypatch.setattr(doctor, "_tv_desktop_running", lambda: False)
    c = doctor._cdp_check(_settings(tmp_path, cdp_url="http://127.0.0.1:1"))
    assert c["ok"] is False and "tv_desktop_launch" in c["fix"]
    monkeypatch.setattr(doctor, "_tv_desktop_running", lambda: True)
    c = doctor._cdp_check(_settings(tmp_path, cdp_url="http://127.0.0.1:1"))
    assert "Stop-Process" in c["fix"] and "tv_desktop_launch" in c["fix"]
