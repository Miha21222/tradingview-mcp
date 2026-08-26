"""Offline contract tests for the `tv` CLI (JSON stdout, error stderr, no network)."""

import json
import subprocess
import sys

import pytest
from fastmcp.exceptions import ToolError

from tvmcp.cli import _parse_args


def _run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "tvmcp.cli", *args],
        capture_output=True, text=True, env=env,
        cwd="C:/Users/Admin/Projects/tradingview-mcp",
    )


def test_parse_args_single_json_object():
    assert _parse_args(['{"symbol":"EURUSD","count":200}']) == {"symbol": "EURUSD", "count": 200}


def test_parse_args_key_value():
    assert _parse_args(["symbol=EURUSD", "count=200", "flag=true"]) == {
        "symbol": "EURUSD", "count": 200, "flag": True,
    }


def test_parse_args_rejects_bad_key_value():
    with pytest.raises(ToolError):
        _parse_args(["naked"])


def test_check_emits_json_tool_list():
    r = _run_cli("--check")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["server"] == "tradingview-mcp"
    assert "tv_data_get_bars" in data["tools"]
    assert "tv_screener_run" in data["tools"]
    # default surface: session/pine/desktop/strategy tools must NOT be registered
    assert "tv_session_ohlcv" not in data["tools"]


def test_offline_tool_call_emits_json():
    r = _run_cli("tv_data_providers_status")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert "providers" in data


def test_unknown_tool_errors_on_stderr():
    r = _run_cli("tv_nope")
    assert r.returncode == 1
    assert r.stdout.strip() == ""
    assert "error" in r.stderr.lower()


def test_bad_args_errors_on_stderr():
    r = _run_cli("tv_data_providers_status", "naked")
    assert r.returncode == 2
    assert r.stdout.strip() == ""
    assert "key=value" in r.stderr