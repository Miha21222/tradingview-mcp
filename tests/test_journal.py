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
from tvmcp.journal import schema
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


# ---------------- M9: the canonical record schema ----------------

def test_fxreplay_records_carry_the_canonical_schema():
    records, _ = parse_file(FIXTURE, currency="USD")
    canonical = schema.to_canonical(records[0])
    assert set(canonical) == set(schema.FIELDS)
    assert canonical["date"] == "2026-01-02"            # session date, not a timestamp
    assert canonical["opened_at"] == "2026-01-02T16:09:25"
    assert canonical["side"] == "short"
    assert canonical["size"] == pytest.approx(1.64) and canonical["size_unit"] == "lots"
    assert canonical["pnl"] == pytest.approx(-100.04) and canonical["pnl_currency"] == "USD"
    assert canonical["r_multiple"] == pytest.approx(-1.0)
    assert canonical["source"] == "fxreplay"
    assert canonical["raw_ref"] == "fxreplay_sample.csv#2"
    assert canonical["derived"] == []                   # both numbers came from the export


def test_fxreplay_legacy_keys_survive_the_refit():
    records, summary = parse_file(FIXTURE)
    assert {"r_pnl", "entry_time", "lots", "session", "day", "tv_symbol", "risk"} <= set(records[0])
    assert summary["win_rate_pct"] == 30.0


def test_schema_stats_report_r_and_currency_separately():
    records, _ = parse_file(FIXTURE, currency="USD")
    s = schema.stats(records)
    assert s["trades"] == 10 and s["wins"] == 3 and s["win_rate_pct"] == 30.0
    assert s["expectancy_currency"] == pytest.approx(-0.33, abs=0.01)
    assert s["expectancy_r"] == pytest.approx(-0.003, abs=0.01)
    assert s["trading_days"] == 7
    assert s["best"]["pnl"] == pytest.approx(281.75)
    assert s["worst"]["pnl"] == pytest.approx(-100.04)
    assert s["by_symbol"]["EURUSD"]["count"] == 10
    assert s["fail_closed"] is False


def test_schema_derives_only_when_the_data_supports_it():
    bare = schema.make_record(source="t", date="2026-01-02", pnl=-250.0)
    assert bare["r_multiple"] is None and bare["derived"] == []
    with_r = schema.make_record(source="t", date="2026-01-02", pnl=-250.0, risk_per_r=100.0)
    assert with_r["r_multiple"] == -2.5
    assert with_r["derived"] == ["r_multiple from pnl / risk_per_r"]
    money = schema.make_record(source="t", date="2026-01-02", r_multiple=-2.0, risk_amount=125.0)
    assert money["pnl"] == -250.0
    assert money["derived"] == ["pnl from r_multiple * risk_amount"]


def test_schema_rejects_junk_numbers_and_sides():
    with pytest.raises(ValueError):
        schema.make_record(source="t", date="2026-01-02", r_multiple="abc")
    with pytest.raises(ValueError):
        schema.make_record(source="t", date="2026-01-02", pnl=1.0, side="sideways")


# ---------------- M9: tv_journal_load over any shape ----------------

BROKER_CSV = (
    "Ticket;Open Time;Close Time;Symbol;Type;Lots;Open Price;Close Price;Commission;Net P/L\n"
    "881;2026.09.14 10:00:00;2026-09-14 11:30:00;EURUSD;Buy;0,50;1,17000;1,17250;-3,5;125,00\n"
    "882;2026-09-15 09:00:00;2026-09-15 09:45:00;XAUUSD;Sell;0,10;3410,5;3415,0;-1,0;-45,00\n"
)
WORKFLOW_CSV = (
    "date,verdict,entered,result_r,root_cause\n"
    "2026-09-14,TRADE,yes,-1.0,execution\n"
    "2026-09-15,STOP,no,,na\n"
    "2026-09-16,TRADE,yes,2.5,na\n"
)


def _load(tmp_path, name, text, args=None):
    jdir = tmp_path / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    (jdir / name).write_text(text, encoding="utf-8")
    return _data(_build(tmp_path), "tv_journal_load", {"path_or_name": name, **(args or {})})


def test_load_sniffs_a_semicolon_broker_export(tmp_path):
    data = _load(tmp_path, "broker.csv", BROKER_CSV, {"currency": "EUR"})
    assert data["delimiter"] == ";"
    assert data["fail_closed"] is False and data["problems"] == []
    m = data["mapping_used"]
    assert m["pnl"] == "Net P/L" and m["side"] == "Type" and m["size"] == "Lots"
    first = data["records"][0]
    assert first["symbol"] == "EURUSD" and first["side"] == "long"
    assert first["size"] == 0.5 and first["size_unit"] == "lots"
    assert first["pnl"] == 125.0 and first["pnl_currency"] == "EUR"   # decimal comma parsed
    assert first["fees"] == -3.5
    assert first["date"] == "2026-09-14" and first["opened_at"] == "2026-09-14T10:00:00"
    # R derived from the row's own risk? no stop column -> nothing invented
    assert first["r_multiple"] is None and first["derived"] == ["date from opened_at"]
    assert data["stats"]["by_symbol"]["XAUUSD"]["count"] == 1


def test_load_derives_r_from_risk_per_r(tmp_path):
    data = _load(tmp_path, "broker.csv", BROKER_CSV, {"risk_per_r": 50.0})
    assert data["records"][0]["r_multiple"] == 2.5
    assert "r_multiple from pnl / risk_per_r" in data["records"][0]["derived"]


def test_load_skips_non_entered_rows_without_calling_them_problems(tmp_path):
    data = _load(tmp_path, "workflow.csv", WORKFLOW_CSV)
    assert data["total_count"] == 2
    assert data["problems"] == []
    assert data["fail_closed"] is False
    assert data["skipped"] == [{"row": 3, "reason": "not entered"}]
    assert data["stats"]["expectancy_r"] == 0.75
    assert data["records"][0]["r_multiple"] == -1.0


def test_load_fails_closed_on_an_unparseable_row(tmp_path):
    broken = WORKFLOW_CSV.replace("2026-09-16,TRADE,yes,2.5", "2026-09-16,TRADE,yes,oops")
    data = _load(tmp_path, "broken.csv", broken)
    assert data["fail_closed"] is True
    assert data["stats"]["fail_closed"] is True
    assert data["problems"] == [{"row": 4, "reason": "result_r='oops' is not a number"}]
    assert data["total_count"] == 1          # the good row is still there, the bad one is not lost


def test_load_flags_an_entry_with_no_result(tmp_path):
    missing = WORKFLOW_CSV.replace("2026-09-16,TRADE,yes,2.5,na", "2026-09-16,TRADE,yes,,na")
    data = _load(tmp_path, "missing.csv", missing)
    assert data["fail_closed"] is True
    assert "no result" in data["problems"][0]["reason"]


def test_load_accepts_an_explicit_mapping(tmp_path):
    odd = "when;what;outcome\n2026-09-14;ES;-120.5\n"
    data = _load(tmp_path, "odd.csv", odd,
                 {"mapping": {"date": "when", "symbol": "what", "pnl": "outcome"}, "currency": "USD"})
    assert data["records"][0]["symbol"] == "ES" and data["records"][0]["pnl"] == -120.5
    assert data["mapping_used"]["pnl"] == "outcome"


def test_load_rejects_a_mapping_naming_absent_columns(tmp_path):
    jdir = tmp_path / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    (jdir / "odd.csv").write_text("when;what;outcome\n2026-09-14;ES;-120.5\n", encoding="utf-8")
    text = _error(_build(tmp_path), "tv_journal_load",
                  {"path_or_name": "odd.csv", "mapping": {"pnl": "Nope"}})
    assert "absent" in text


def test_load_refuses_a_file_with_no_result_column(tmp_path):
    jdir = tmp_path / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    (jdir / "notes.csv").write_text("date,note\n2026-09-14,hello\n", encoding="utf-8")
    text = _error(_build(tmp_path), "tv_journal_load", {"path_or_name": "notes.csv"})
    assert "no result column" in text


def test_load_reads_json_journals(tmp_path):
    payload = json.dumps({"trades": [
        {"date": "2026-09-14", "symbol": "NQ", "side": "short", "r": -1.0, "strategy": "IB fade"},
        {"date": "2026-09-15", "symbol": "NQ", "side": "long", "r": 2.0, "strategy": "IB fade"},
    ]})
    data = _load(tmp_path, "trades.json", payload)
    assert data["delimiter"] == "json"
    assert data["stats"]["by_strategy"]["IB fade"]["count"] == 2
    assert data["records"][1]["r_multiple"] == 2.0


def test_load_auto_detects_the_fxreplay_schema(tmp_path):
    data = _load(tmp_path, "fxr.csv", FIXTURE.read_text(encoding="utf-8"), {"currency": "USD"})
    assert data["source"] == "fxreplay"
    assert data["stats"]["trades"] == 10 and data["stats"]["win_rate_pct"] == 30.0
    assert data["records"][0]["source"] == "fxreplay"


def test_load_takes_a_full_path_outside_the_watch_folder(tmp_path):
    outside = tmp_path / "vault" / "journal.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text(WORKFLOW_CSV, encoding="utf-8")
    data = _data(_build(tmp_path), "tv_journal_load", {"path_or_name": str(outside)})
    assert data["total_count"] == 2


def test_load_rejects_unsupported_suffixes(tmp_path):
    text = _error(_build(tmp_path), "tv_journal_load", {"path_or_name": "trades.xlsx"})
    assert "expected one of" in text


def test_load_truncates_but_keeps_whole_file_stats(tmp_path):
    rows = "".join(f"2026-09-{(i % 28) + 1:02d},TRADE,yes,1.0,na\n" for i in range(120))
    data = _load(tmp_path, "many.csv", "date,verdict,entered,result_r,root_cause\n" + rows,
                 {"limit": 25})
    assert data["total_count"] == 120
    assert data["returned_count"] == 25
    assert data["truncated"] is True
    assert data["stats"]["trades"] == 120



