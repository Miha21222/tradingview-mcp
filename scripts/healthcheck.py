"""One-command install health check for tradingview-mcp (see AGENT_SETUP.md).

    uv run python scripts/healthcheck.py [--json]

Checks: Python, uv, Node/npx, Chromium (Playwright), server boot + tool count,
tv_setup_doctor findings, MCP registration in Claude Code (if `claude` is on
PATH). Exit 0 = every required check passed; 1 = a required check failed.
Stdlib only; never reads or prints credentials.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_PY = (3, 12)
CHECKS: list[dict] = []


def add(name: str, ok: bool, detail: str, required: bool = True, fix: str | None = None) -> None:
    CHECKS.append({"name": name, "ok": ok, "required": required, "detail": detail, "fix": fix})


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def check_python() -> None:
    v = sys.version_info
    add("python", v >= REQUIRED_PY, f"{v.major}.{v.minor}.{v.micro}",
        fix="uv python install 3.12 && uv sync")


def check_binary(name: str, args: list[str], required: bool, fix: str) -> None:
    path = shutil.which(name)
    if not path:
        add(name, False, "not on PATH", required, fix)
        return
    code, out = run([name, *args], timeout=20)
    add(name, code == 0, out.splitlines()[0] if out else path, required, fix)


def check_chromium() -> None:
    # separate process: playwright's sync API + a later asyncio.run in this process print shutdown noise
    probe = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    b = p.chromium.launch(headless=True); v = b.version; b.close()\n"
        "print(v)\n"
    )
    code, out = run([sys.executable, "-c", probe], timeout=90)
    if code == 0:
        add("chromium", True, f"headless Chromium {out.splitlines()[-1]} launches", required=False)
    else:
        add("chromium", False, (out.splitlines()[-1] if out else "launch failed")[:120], required=False,
            fix="uv run playwright install chromium")


def check_server() -> dict:
    """Boot the server in-process with the configured toolsets; return the doctor result."""
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from tvmcp.config import load_settings
        from tvmcp.server import build_server
    except Exception as e:  # noqa: BLE001
        add("server_import", False, f"{e}", fix="uv sync")
        return {}
    settings = load_settings()
    try:
        mcp = build_server(settings)
        tools = asyncio.run(mcp.list_tools())
    except Exception as e:  # noqa: BLE001
        add("server_boot", False, f"{e}", fix="uv sync; uv run python -m tvmcp --check")
        return {}
    names = sorted(t.name for t in tools)
    add("server_boot", True,
        f"{len(names)} tools, toolsets={','.join(sorted(settings.toolsets))} "
        f"(TV_TOOLSETS={os.environ.get('TV_TOOLSETS', 'unset -> default')})")
    if "tv_setup_doctor" not in names:
        add("doctor", False, "tv_setup_doctor not registered", fix="update the repo: doctor must always register")
        return {}
    try:
        res = asyncio.run(mcp.call_tool("tv_setup_doctor", {}))
        payload = _tool_payload(res)
    except Exception as e:  # noqa: BLE001
        add("doctor", False, f"call failed: {e}")
        return {}
    for c in payload.get("checks", []):
        add(f"doctor.{c['name']}", bool(c["ok"]), c.get("detail", ""), required=not c.get("optional", False),
            fix=c.get("fix"))
    return payload


def _tool_payload(res) -> dict:
    # fastmcp returns either a ToolResult with .structured_content / .content, or a list of content blocks
    sc = getattr(res, "structured_content", None)
    if isinstance(sc, dict):
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    content = getattr(res, "content", res)
    for block in content or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return {}


def check_claude_registration() -> None:
    if not shutil.which("claude"):
        add("claude_cli", True, "claude CLI not on PATH - registration not checked (fine for other clients)", required=False)
        return
    code, out = run(["claude", "mcp", "list"], timeout=90)
    if code != 0:
        add("claude_mcp_list", False, out[:200], required=False, fix="claude mcp list")
        return
    lines = {ln.split(":")[0].strip(): ln for ln in out.splitlines() if ":" in ln}
    for name, fix in (
        ("tradingview", 'claude mcp add -s user tradingview -e TV_TOOLSETS=hybrid -- uv --directory "<repo path>" run python -m tvmcp'),
        ("mcp-tradingview", "claude mcp add -s user --transport http mcp-tradingview https://mcp.tradingview.com/mcp"),
    ):
        ln = lines.get(name)
        if not ln:
            add(f"registered.{name}", False, "not registered in Claude Code", required=False, fix=fix)
        else:
            ok = "Connected" in ln or "needs authentication" in ln.lower()
            add(f"registered.{name}", ok, ln.split(" - ", 1)[-1] if " - " in ln else ln, required=False,
                fix=None if ok else fix)


def main() -> int:
    as_json = "--json" in sys.argv
    check_python()
    check_binary("uv", ["--version"], True, "https://docs.astral.sh/uv/ then open a new shell")
    check_binary("node", ["--version"], True, "install Node 22+ (Dukascopy provider needs npx)")
    check_chromium()
    check_server()
    check_claude_registration()
    healthy = all(c["ok"] for c in CHECKS if c["required"])
    if as_json:
        print(json.dumps({"healthy": healthy, "checks": CHECKS}, indent=1))
    else:
        for c in CHECKS:
            mark = "OK " if c["ok"] else ("FAIL" if c["required"] else "warn")
            print(f"[{mark}] {c['name']:<28} {c['detail']}")
            if not c["ok"] and c.get("fix"):
                print(f"       fix: {c['fix']}")
        print(f"\nHEALTH: {'OK' if healthy else 'FAILED'}  ({sum(1 for c in CHECKS if not c['ok'] and not c['required'])} optional warnings)")
    return 0 if healthy else 1


if __name__ == "__main__":
    rc = main()
    # tv_setup_doctor launches Playwright in-process; interpreter teardown then prints
    # harmless "Task was destroyed" noise. Flush and leave without teardown.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
