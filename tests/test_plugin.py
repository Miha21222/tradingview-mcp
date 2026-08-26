"""Plugin packaging contract tests: manifest schema, sensitive userConfig, no secrets."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _load(path: str):
    with (ROOT / path).open(encoding="utf-8") as f:
        return json.load(f)


def test_plugin_manifest_fields():
    p = _load(".claude-plugin/plugin.json")
    assert p["name"] == "tradingview-mcp"
    assert p["version"] == "0.1.0"
    assert p["license"] == "MIT"
    assert "mcpServers" in p and "tradingview" in p["mcpServers"]


def test_user_config_sensitive_credentials():
    uc = _load(".claude-plugin/plugin.json")["userConfig"]
    assert "OANDA_API_KEY" in uc and uc["OANDA_API_KEY"]["sensitive"] is True
    assert "TV_SESSIONID" in uc and uc["TV_SESSIONID"]["sensitive"] is True
    # secrets must not have defaults or be embedded in the manifest
    for key in ("OANDA_API_KEY", "TV_SESSIONID"):
        assert "default" not in uc[key]


def test_mcp_server_env_bridges_user_config():
    env = _load(".claude-plugin/plugin.json")["mcpServers"]["tradingview"]["env"]
    assert env.get("OANDA_API_KEY") == "${OANDA_API_KEY}"
    assert env.get("TV_SESSIONID") == "${TV_SESSIONID}"


def test_marketplace_references_plugin():
    m = _load(".claude-plugin/marketplace.json")
    assert m["name"] == "tradingview-mcp-marketplace"
    assert any(e["name"] == "tradingview-mcp" for e in m["plugins"])


def test_no_hardcoded_secrets_in_repo():
    # a crude secret redaction scan: long high-entropy tokens that look like
    # API keys / session cookies must not be present in tracked source
    suspect = re.compile(r"(?i)([a-z0-9_-]{2,}_(?:api|token|key|secret)\s*=\s*['\"][A-Za-z0-9+/=_-]{16,}['\"])")
    hits = []
    for p in ROOT.rglob("*.py"):
        if "__pycache__" in p.parts or ".venv" in p.parts:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if suspect.search(line) and "placeholder" not in line.lower():
                hits.append(f"{p.relative_to(ROOT)}:{i}")
    assert not hits, f"possible hardcoded secrets: {hits}"