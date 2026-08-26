"""Offline tests for the `pine` toolset: issue extraction + tool wiring.

Fixture payloads copied verbatim from live translate_light responses (2026-08-26).
The live endpoint is exercised by the `network`-marked test at the bottom.
"""

import asyncio

import pytest

from tvmcp.pine.toolset import _extract_issues

GOOD_PAYLOAD = {"success": True, "result": {"functions2": [], "types": [], "enums": []}}

BAD_PAYLOAD = {
    "success": True,
    "result": {
        "errors2": [
            {
                "end": {"column": 13, "line": 3},
                "message": "Syntax error at input 'end of line without line continuation'",
                "start": {"column": 13, "line": 3},
            }
        ],
        "functions2": [],
        "types": [],
        "enums": [],
    },
}


def test_extract_no_issues():
    assert _extract_issues(GOOD_PAYLOAD) == []


def test_extract_errors2_with_position():
    issues = _extract_issues(BAD_PAYLOAD)
    assert len(issues) == 1
    e = issues[0]
    assert e["severity"] == "error"
    assert e["line"] == 3
    assert e["column"] == 13
    assert "Syntax error" in e["message"]


def test_extract_top_level_reason_on_failure():
    issues = _extract_issues({"success": False, "reason": "line 2: something broke"})
    assert issues == [
        {"message": "line 2: something broke", "line": 2, "column": None, "severity": "error"}
    ]


def _server_with_pine(tmp_path, compiler):
    from fastmcp import FastMCP

    from tvmcp.config import Settings
    from tvmcp.pine.toolset import register

    settings = Settings(
        toolsets=frozenset({"pine"}),
        extra_tools=frozenset(),
        read_only=False,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=None,
        oanda_env="practice",
        session_id=None,
    )
    mcp = FastMCP(name="test")
    register(mcp, settings, compiler=compiler)
    return mcp


def test_tool_success_requires_no_errors(tmp_path):
    mcp = _server_with_pine(tmp_path, lambda src: {"success": True, "errors": [], "warnings": []})
    res = asyncio.run(mcp.call_tool("tv_pine_compile", {"source": "//@version=5\nindicator('x')\nplot(close)"}))
    assert res.structured_content["success"] is True


def test_tool_rejects_empty_source(tmp_path):
    from fastmcp.exceptions import ToolError

    mcp = _server_with_pine(tmp_path, lambda src: {"success": True, "errors": [], "warnings": []})
    with pytest.raises(ToolError, match="empty"):
        asyncio.run(mcp.call_tool("tv_pine_compile", {"source": "   "}))


@pytest.mark.network
def test_live_translate_light_roundtrip():
    from tvmcp.pine.toolset import compile_source

    good = compile_source('//@version=5\nindicator("t")\nplot(close)\n', None)
    assert good["success"] is True and good["errors"] == []

    bad = compile_source('//@version=5\nindicator("t")\nplot(close +\n', None)
    assert bad["success"] is False
    assert bad["errors"] and bad["errors"][0]["line"] == 3

