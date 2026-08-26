"""Journal watch-folder scanner + FX Replay normalizer tests.

Uses the genuine export fixture (tests/fixtures/journal/fxreplay_sample.csv) - the
owner's 10-trade EURUSD dataset - to gate the normalizer and reproduce the M3
validation stats (WR=30%, expectancy≈-0.33).
"""

import csv
import shutil
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tvmcp import journal
from tvmcp.config import Settings
from tvmcp.journal.normalize import parse_file

import asyncio
import json

FIXTURE = Path(__file__).parent / "fixtures" / "journal" / "fxreplay_sample.csv"


def _settings(tmp_path) -> Settings:
    return Settings(
        toolsets=frozenset({"journal"}),
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


def _build(tmp_path):
    mcp = FastMCP(name="test")
    journal.register(mcp, _settings(tmp_path))
    return mcp


def _write_csv(path: Path, header, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _data(mcp, name, args):
    r = asyncio.run(mcp.call_tool(name, args))
    return json.loads(r.content[0].text)


def _error(mcp, name, args):
    with pytest.raises(ToolError) as ei:
        asyncio.run(mcp.call_tool(name, args))
    return str(ei.value)


def test_scan_reports_csv_columns_and_rows(tmp_path):
    _write_csv(tmp_path / "journal" / "export.csv",
               ["Date", "Symbol", "Buy/Sell", "P/L", "Lot"],
               [["2026-01-01", "EURUSD", "Buy", "25.5", "0.1"],
                ["2026-01-02", "GBPUSD", "Sell", "-12", "0.2"]])
    data = _data(_build(tmp_path), "tv_journal_scan", {})
    assert data["count"] == 1
    f = data["files"][0]
    assert f["name"] == "export.csv"
    assert f["rows"] == 2
    assert "Buy/Sell" in f["columns"]
    assert f["fxreplay_likely"] is True


def test_scan_flags_non_fxreplay(tmp_path):
    _write_csv(tmp_path / "journal" / "random.csv", ["a", "b", "c"], [[1, 2, 3]])
    data = _data(_build(tmp_path), "tv_journal_scan", {})
    assert data["files"][0]["fxreplay_likely"] is False


def test_scan_missing_dir_raises_toolerror(tmp_path):
    text = _error(_build(tmp_path), "tv_journal_scan", {})
    assert "TV_JOURNAL_DIR" in text


def test_scan_empty_dir_reports_none(tmp_path):
    (tmp_path / "journal").mkdir(parents=True, exist_ok=True)
    data = _data(_build(tmp_path), "tv_journal_scan", {})
    assert data["files"] == []


def test_scan_ignores_non_csv(tmp_path):
    (tmp_path / "journal").mkdir(parents=True, exist_ok=True)
    (tmp_path / "journal" / "notes.txt").write_text("not csv")
    data = _data(_build(tmp_path), "tv_journal_scan", {})
    assert data["files"] == []


# ---------------- FX Replay normalizer (genuine export fixture) ----------------

def test_parse_reproduces_validation_stats():
    records, summary = parse_file(FIXTURE)
    assert summary["trades"] == 10
    assert summary["wins"] == 3
    assert summary["win_rate_pct"] == 30.0
    assert summary["expectancy"] == pytest.approx(-0.33, abs=0.01)
    assert summary["total_pnl"] == pytest.approx(-3.26, abs=0.01)
    assert summary["avg_r"] == pytest.approx(-0.003, abs=0.01)  # 7 losers -1R, 3 winners


def test_parse_maps_rows_to_vault_conventions():
    records, _ = parse_file(FIXTURE)
    r = records[0]
    assert r["symbol"] == "EURUSD"          # OANDA:EURUSD -> canonical
    assert r["tv_symbol"] == "OANDA:EURUSD"
    assert r["side"] == "short"             # sell -> short
    assert r["day"] == "Friday"             # day code 5 -> Friday
    assert r["entry_time"].startswith("2026-01-02T16:09")
    assert r["lots"] == pytest.approx(1.64, abs=0.01)  # 164000 / 100000
    assert r["r"] == pytest.approx(-1.0, abs=0.01)     # SL hit, full risk
    assert r["risk"] == pytest.approx(100.04, abs=0.1)


def test_parse_win_trade_r_matches_export():
    records, _ = parse_file(FIXTURE)
    win = next(r for r in records if r["r_pnl"] > 0)
    # export says avgRiskReward 1.79 for that trade
    assert win["avg_risk_reward"] == pytest.approx(win["r"], abs=0.01)


def test_parse_sessions_and_tags():
    records, _ = parse_file(FIXTURE)
    # trade 1 entered 07:43 UTC -> London open kill zone; trade 0 at 16:09 -> off
    assert records[1]["session"] == "London open kill zone"
    assert records[0]["session"] == "off"
    assert records[7]["session"] == "London close kill zone"  # 14:47
    assert records[0]["tags"] == ["SETUP_A", "SETUP_B", "SETUP_C", "SETUP_D",
                                  "SETUP_E", "SETUP_F", "SETUP_G"]
    assert records[4]["tags"] == []  # blank tags


def test_parse_rejects_unknown_side(tmp_path):
    bad = tmp_path / "bad.csv"
    with FIXTURE.open(newline="", encoding="utf-8") as f:
        content = f.read()
    bad.write_text(content.replace(",sell,", ",sideways,"), encoding="utf-8")
    with pytest.raises(ValueError):
        parse_file(bad)


def test_parse_missing_columns_raises(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_file(bad)


def test_parse_row_limit_guard(tmp_path):
    with pytest.raises(ValueError):
        parse_file(FIXTURE, max_rows=5)


def test_parse_byte_limit_guard(tmp_path):
    with pytest.raises(ValueError):
        parse_file(FIXTURE, max_bytes=100)


def test_tool_parse_end_to_end(tmp_path):
    jdir = tmp_path / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, jdir / "fxreplay_sample.csv")
    mcp = _build(tmp_path)
    data = _data(mcp, "tv_journal_parse", {"filename": "fxreplay_sample.csv"})
    assert data["trades"] == 10
    assert data["summary"]["win_rate_pct"] == 30.0
    assert data["records"][0]["side"] == "short"


def test_tool_parse_path_traversal_raises(tmp_path):
    (tmp_path / "journal").mkdir(parents=True, exist_ok=True)
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_journal_parse", {"filename": "..\\outside.csv"})
    assert "directly inside" in text


def test_tool_parse_sibling_prefix_traversal_raises(tmp_path):
    # a sibling dir whose name is a prefix of journal must NOT be reachable
    (tmp_path / "journal").mkdir(parents=True, exist_ok=True)
    (tmp_path / "journal_evil").mkdir(parents=True, exist_ok=True)
    (tmp_path / "journal_evil" / "evil.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_journal_parse", {"filename": "..\\journal_evil\\evil.csv"})
    assert "directly inside" in text


def test_tool_parse_truncates_large_exports(tmp_path):
    jdir = tmp_path / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    # build a 150-row export from the fixture (unique ids, one header)
    with FIXTURE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with (jdir / "big.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for i in range(150):
            r = dict(rows[i % len(rows)])
            r["id"] = str(i + 1)
            w.writerow(r)
    mcp = _build(tmp_path)
    data = _data(mcp, "tv_journal_parse", {"filename": "big.csv"})
    assert data["total_count"] == 150
    assert data["returned_count"] == 100
    assert data["truncated"] is True
    assert data["summary"]["trades"] == 150  # summary over the WHOLE csv


def test_tool_parse_missing_file_raises(tmp_path):
    (tmp_path / "journal").mkdir(parents=True, exist_ok=True)
    mcp = _build(tmp_path)
    text = _error(mcp, "tv_journal_parse", {"filename": "nope.csv"})
    assert "No such file" in text



