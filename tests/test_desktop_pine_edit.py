"""M8 part B: pine_editor extensions - header read, own-copy flow, exact
find/replace (EOL normalization, occurrence + sha guards, saved-script
refusal), verified save/save_as, marker settling + compile dialogs, and
get_errors with hints. FakePage + FakeClock; no CDP, no real sleeps.
"""

import asyncio
import hashlib
import json
from contextlib import contextmanager

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import desktop
from tvmcp.config import Settings
from tvmcp.desktop import clock, driver, pine_editor

from desktop_fake import FakeClock, FakePage

SRC = "//@version=6\nstrategy(\"My Strat\", overlay=true)\nma = ta.sma(close, 20)\nplot(ma)\n"


def _settings(tmp_path, read_only=False) -> Settings:
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
    )


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch):
    from tvmcp.desktop import warnings as w

    fc = FakeClock()
    monkeypatch.setattr(clock, "now", fc.now)
    monkeypatch.setattr(clock, "sleep", fc.sleep)
    w._printed = False
    driver._bound = None
    yield fc
    w._printed = False
    driver._bound = None


def _build(tmp_path, page=None, read_only=False):
    page = page or FakePage()

    @contextmanager
    def factory(cdp_url):
        yield page

    mcp = FastMCP(name="test")
    desktop.register(mcp, _settings(tmp_path, read_only), page_factory=factory)
    return mcp, page


def _data(mcp, name, args=None):
    r = asyncio.run(mcp.call_tool(name, args or {}))
    return json.loads(r.content[0].text)


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# --- header ----------------------------------------------------------------------

def test_read_header_declared_title_and_library_flag():
    page = FakePage()
    page.editor, page.script_name, page.saved = SRC, "My SMC", True
    h = pine_editor.read_header(page)
    assert h == {"script_name": "My SMC", "saved": True, "declared_title": "My Strat",
                 "is_library_script": True}
    page.script_name = "Scratch"
    assert pine_editor.read_header(page)["is_library_script"] is False


def test_get_source_carries_sha256():
    page = FakePage()
    page.editor = SRC
    assert pine_editor.get_source(page)["sha256"] == _sha(SRC)


# --- find / replace ------------------------------------------------------------------

def test_find_exact_counts_and_positions():
    page = FakePage()
    page.editor = SRC
    res = pine_editor.find_exact(page, "close")
    assert res["occurrences"] == 1 and res["positions"] == [{"line": 3, "column": 13}]
    assert res["eol"] == "LF" and res["sha256"] == _sha(SRC)
    assert pine_editor.find_exact(page, "nope")["occurrences"] == 0


def test_replace_exact_happy_path_is_verified():
    page = FakePage()
    page.editor = SRC
    before = _sha(SRC)
    res = pine_editor.replace_exact(page, "ta.sma(close, 20)", "ta.ema(close, 50)",
                                    expected_occurrences=1, expect_source_sha256=before)
    assert res["applied"] is True and res["verified"] is True
    assert res["occurrences_before"] == 1 and res["occurrences_after"] == 0
    assert res["sha256_before"] == before and res["sha256_after"] == _sha(page.editor)
    assert "ta.ema(close, 50)" in page.editor and "ta.sma" not in page.editor
    assert res["positions"] == [{"line": 3, "column": 6}]


def test_replace_exact_occurrence_mismatch_changes_nothing():
    page = FakePage()
    page.editor = "a = 1\nb = 1\nc = 1\n"
    res = pine_editor.replace_exact(page, "= 1", "= 2", expected_occurrences=1)
    assert res["applied"] is False and res["reason"] == "occurrence_mismatch"
    assert res["occurrences_before"] == 3 and res["occurrences_after"] == 3
    assert page.editor == "a = 1\nb = 1\nc = 1\n"
    assert "occurs 3 time(s), expected 1" in res["note"]
    res = pine_editor.replace_exact(page, "= 1", "= 2", expected_occurrences=3)
    assert res["applied"] is True and page.editor == "a = 2\nb = 2\nc = 2\n"


def test_replace_exact_sha_mismatch_refuses():
    page = FakePage()
    page.editor = SRC
    res = pine_editor.replace_exact(page, "close", "open", expect_source_sha256="deadbeef")
    assert res["applied"] is False and res["reason"] == "sha_mismatch"
    assert page.editor == SRC and "re-read" in res["note"]


def test_crlf_buffer_matches_lf_needle_and_keeps_crlf():
    page = FakePage()
    page.editor = "x = 1\r\ny = 2\r\nz = 3\r\n"
    res = pine_editor.find_exact(page, "x = 1\ny = 2")
    assert res["eol"] == "CRLF" and res["occurrences"] == 1
    res = pine_editor.replace_exact(page, "x = 1\ny = 2", "x = 9\ny = 8")
    assert res["applied"] is True and res["eol"] == "CRLF"
    assert page.editor == "x = 9\r\ny = 8\r\nz = 3\r\n"


def test_replace_exact_saved_script_guard():
    page = FakePage()
    page.editor, page.saved = SRC, True
    with pytest.raises(ToolError, match="SAVED scripts"):
        pine_editor.replace_exact(page, "close", "open")
    assert page.editor == SRC
    # dry_run is a read: allowed on a saved script, edits nothing
    res = pine_editor.replace_exact(page, "close", "open", dry_run=True)
    assert res["applied"] is False and res["reason"] == "dry_run" and page.editor == SRC
    res = pine_editor.replace_exact(page, "close", "open", overwrite_saved=True)
    assert res["applied"] is True and "open" in page.editor


def test_expect_script_matches_header_or_declared_title():
    page = FakePage()
    page.editor, page.script_name = SRC, "Scratch"
    assert pine_editor.find_exact(page, "close", expect_script="Scratch")["occurrences"] == 1
    assert pine_editor.find_exact(page, "close", expect_script="My Strat")["occurrences"] == 1
    with pytest.raises(ToolError, match="expect_script='Other'"):
        pine_editor.replace_exact(page, "close", "open", expect_script="Other")
    assert page.editor == SRC


def test_replace_tool_round_trip(tmp_path):
    mcp, page = _build(tmp_path)
    page.editor = SRC
    found = _data(mcp, "tv_desktop_pine_find_exact", {"needle": "ta.sma"})
    assert found["occurrences"] == 1 and found["provider"] == "desktop"
    data = _data(mcp, "tv_desktop_pine_replace_exact", {
        "needle": "ta.sma", "replacement": "ta.ema", "expect_source_sha256": found["sha256"]})
    assert data["applied"] is True and "ta.ema" in page.editor
    assert "tv_desktop_pine_find_exact" in {t.name for t in asyncio.run(
        _build(tmp_path, read_only=True)[0].list_tools())}


# --- own copy / save -------------------------------------------------------------

def test_ensure_own_copy_modes():
    page = FakePage()
    page.editor = ""
    assert pine_editor.ensure_own_copy(page, "agent-x")["mode"] == "unsaved"
    page.editor, page.script_name, page.saved = SRC, "agent-x", True
    assert pine_editor.ensure_own_copy(page, "agent-x")["mode"] == "own"


def test_ensure_own_copy_copies_foreign_saved_script_and_verifies():
    page = FakePage()
    page.editor, page.script_name, page.saved = SRC, "My SMC", True
    res = pine_editor.ensure_own_copy(page, "agent-x")
    assert res["mode"] == "copied" and res["verified"] is True
    assert page.menu_clicks == ["copy"]
    assert page.dialog_calls[-1]["name"] == "agent-x"
    assert page.script_name == "agent-x" and page.saved is True
    assert any(s["name"] == "agent-x" for s in page.scripts)
    assert res["header"]["is_library_script"] is True


def test_ensure_own_copy_falls_back_to_create_new():
    page = FakePage()
    page.editor, page.script_name, page.saved = SRC, "My SMC", True
    page.copy_succeeds = False
    res = pine_editor.ensure_own_copy(page, "agent-x")
    assert res["mode"] == "created" and res["verified"] is True
    assert page.menu_clicks == ["copy", "new"]
    assert page.saved is False and page.editor == ""
    assert res["detail"]["copy"]["menu"]["clicked"] is None


def test_ensure_own_copy_raises_when_no_safe_buffer():
    page = FakePage()
    page.editor, page.script_name, page.saved = SRC, "My SMC", True
    page.menu_available = False
    with pytest.raises(ToolError, match="agent-owned buffer"):
        pine_editor.ensure_own_copy(page, "agent-x")
    assert page.editor == SRC and page.saved is True  # untouched


def test_save_as_unsaved_buffer_via_ctrl_s_dialog():
    page = FakePage()
    page.editor = SRC
    res = pine_editor.save_as(page, "agent-x")
    assert res == {"saved": True, "verified": True, "via": "Ctrl+S", "header": res["header"]}
    assert page.pressed[-1] == "s" and page.modifiers == pine_editor.CTRL
    assert page.script_name == "agent-x" and res["header"]["is_library_script"] is True


def test_save_as_foreign_saved_script_copies_and_already_named_is_noop():
    page = FakePage()
    page.editor, page.script_name, page.saved = SRC, "My SMC", True
    res = pine_editor.save_as(page, "agent-x")
    assert res["via"] == "copy" and res["verified"] is True and page.script_name == "agent-x"
    res = pine_editor.save_as(page, "agent-x")
    assert res["via"] == "already" and res["verified"] is True
    assert page.menu_clicks == ["copy"]


def test_save_as_raises_when_copy_cannot_be_verified():
    page = FakePage()
    page.editor, page.script_name, page.saved = SRC, "My SMC", True
    page.copy_succeeds = False
    with pytest.raises(ToolError, match="could not be verified"):
        pine_editor.save_as(page, "agent-x")
    assert page.editor == SRC


def test_save_reports_verification_against_the_library():
    page = FakePage()
    page.editor = SRC
    res = pine_editor.save(page)
    assert res["via"] == "Ctrl+S" and res["verified"] is False  # "Untitled script" is not saved
    page.script_name, page.saved = "My SMC", True
    res = pine_editor.save(page)
    assert res["verified"] is True and res["script_name"] == "My SMC"


def test_modified_parsing():
    assert pine_editor._modified_ts(None) is None
    assert pine_editor._modified_ts(1_700_000_000) == 1_700_000_000
    assert pine_editor._modified_ts(1_700_000_000_000) == 1_700_000_000
    assert pine_editor._modified_ts("2023-11-14T22:13:20Z") == 1_700_000_000
    assert pine_editor._modified_ts("garbage") is None


# --- compile / errors ------------------------------------------------------------

def test_compile_waits_for_markers_to_settle(_fake_clock):
    page = FakePage()
    page.editor = SRC
    err = {"line": 2, "column": 1, "severity": "error", "message": "Undeclared identifier 'foo'"}
    page.marker_sequence = [[], [], [err], [err], [err]]
    res = pine_editor.compile_on_chart(page)
    assert res["ok"] is False and res["errors"] == [err] and res["settled"] is True
    assert res["dialogs_handled"] == []
    assert not any(s >= 2.5 for s in _fake_clock.slept)  # no fixed sleep anymore


def test_compile_handles_save_prompt_and_rename_dialogs():
    page = FakePage()
    page.editor = SRC
    page.save_prompt_on_compile = True
    res = pine_editor.compile_on_chart(page, save_name="agent-x")
    kinds = [d["kind"] for d in res["dialogs_handled"]]
    assert kinds == ["save_prompt", "rename"]
    assert page.script_name == "agent-x" and page.saved is True
    assert res["ok"] is True and res["study_added"] is True


def test_compile_tool_accepts_save_name(tmp_path):
    mcp, page = _build(tmp_path)
    page.editor = SRC
    page.save_prompt_on_compile = True
    data = _data(mcp, "tv_desktop_pine_compile", {"save_name": "agent-x"})
    assert data["ok"] is True and len(data["dialogs_handled"]) == 2


def test_get_errors_attaches_hints_only_on_errors(tmp_path):
    mcp, page = _build(tmp_path)
    page.editor = "//@version=5\nstrategy('x')\n"
    page.markers = [{"line": 1, "column": 1, "severity": "warning", "message": "unused"}]
    data = _data(mcp, "tv_desktop_pine_get_errors")
    assert data["ok"] is True and data["hints"] == [] and len(data["warnings"]) == 1
    page.markers.append({"line": 3, "column": 2, "severity": "error",
                         "message": "Undeclared identifier 'foo'"})
    data = _data(mcp, "tv_desktop_pine_get_errors")
    assert data["ok"] is False and data["errors"][0]["line"] == 3
    assert any("Undeclared identifier" in h for h in data["hints"])
    assert any("//@version=6" in h for h in data["hints"])
    assert len(page.exprs) and "/*tvmcp:pine_errors*/" in page.exprs[-1]
    assert not any("/*tvmcp:pine_click*/" in e for e in page.exprs)


def test_save_as_tool_registered_write_only(tmp_path):
    mcp, page = _build(tmp_path)
    page.editor = SRC
    data = _data(mcp, "tv_desktop_pine_save_as", {"name": "agent-x"})
    assert data["verified"] is True
    names = {t.name for t in asyncio.run(_build(tmp_path, read_only=True)[0].list_tools())}
    assert "tv_desktop_pine_save_as" not in names and "tv_desktop_pine_get_errors" in names
