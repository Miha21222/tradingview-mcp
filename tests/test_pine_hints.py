"""M8 part B: `desktop/pine_hints.py` - landmine hints from marker messages and
source patterns; attached only when errors exist; <= 3 unique, table order.
"""

from tvmcp.desktop import pine_hints as H


def _err(msg):
    return {"line": 1, "column": 1, "severity": "error", "message": msg}


def _warn(msg):
    return {"line": 1, "column": 1, "severity": "warning", "message": msg}


def test_no_errors_means_no_hints_even_with_warnings_and_old_version():
    assert H.hints_for([], [_warn("shorttitle is too long")], "//@version=4\nstrategy('x')") == []
    assert H.hints_for(None, None, None) == []


def test_strategy_fixed_specific_beats_generic_undeclared():
    ids = H.matched_ids([_err("Undeclared identifier 'strategy.fixed'")], [], "")
    assert ids == ["strategy_fixed_const"]
    hints = H.hints_for([_err("Undeclared identifier 'strategy.fixed'")], [], "")
    assert len(hints) == 1 and "default_qty_type" in hints[0]


def test_strategy_const_args_from_input():
    msg = 'Cannot call "strategy" with argument "default_qty_value"="call "input.int"". An argument of "series int" type was used but a "const int" is expected'
    assert "strategy_const_args" in H.matched_ids([_err(msg)], [], "")


def test_input_time_defval_string():
    msg = 'Cannot call "input.time" with argument "defval"="\'2024-01-01\'". An argument of "const string" type was used but a "const int" is expected'
    ids = H.matched_ids([_err(msg)], [], "")
    assert "input_time_defval" in ids
    assert any("timestamp(" in h for h in H.hints_for([_err(msg)], [], ""))


def test_v6_bool_condition():
    for msg in ("The if statement condition must be of type bool, but it is of type series int",
                "Cannot use 'series float' as condition. Expected series bool"):
        assert "condition_must_be_bool" in H.matched_ids([_err(msg)], [], ""), msg


def test_warn_only_rows_match_warnings_when_an_error_exists():
    warn = _warn("The function 'ta.sma' should be called on each calculation for consistency. It is recommended to extract the call from the conditional scope")
    assert H.matched_ids([], [warn], "") == []
    ids = H.matched_ids([_err("Mismatched input")], [warn], "")
    assert "ta_in_conditional" in ids
    assert "alertcondition_in_strategy" in H.matched_ids(
        [_err("x")], [_warn("alertcondition() is not supported in strategy scripts")], "")
    assert "shorttitle_too_long" in H.matched_ids(
        [_err("x")], [_warn("The shorttitle is too long (max 10 characters)")], "")


def test_generic_undeclared_skips_namespaced_names():
    assert "undeclared_identifier" in H.matched_ids([_err("Undeclared identifier 'myVar'")], [], "")
    assert "undeclared_identifier" not in H.matched_ids([_err("Undeclared identifier 'ta.smaa'")], [], "")
    assert "undeclared_identifier" not in H.matched_ids([_err("Undeclared identifier 'math.abz'")], [], "")


def test_source_hints_version_and_calc_on_every_tick():
    src5 = "//@version=5\nstrategy('x')\n"
    ids = H.matched_ids([_err("x")], [], src5)
    assert "outdated_version" in ids and "calc_on_every_tick_absent" in ids
    src6 = "//@version=6\nstrategy('x', calc_on_every_tick=true)\n"
    ids = H.matched_ids([_err("x")], [], src6)
    assert "outdated_version" not in ids and "calc_on_every_tick_absent" not in ids
    assert "calc_on_every_tick_absent" not in H.matched_ids([_err("x")], [], "//@version=6\nindicator('x')\n")


def test_cap_three_unique_in_table_order():
    errors = [_err("Undeclared identifier 'strategy.fixed'"),
              _err('Cannot call "strategy" with argument "x"="call "input.int""'),
              _err("Cannot call \"input.time\" with argument \"defval\"=\"'2024'\" (const string)"),
              _err("Undeclared identifier 'foo'"),
              _err("Undeclared identifier 'foo'")]
    hints = H.hints_for(errors, [], "//@version=4\nstrategy('x')")
    assert len(hints) == 3 and len(set(hints)) == 3
    assert "default_qty_type" in hints[0] and "compile-time constants" in hints[1]
    assert "timestamp(" in hints[2]


def test_table_shape():
    for table in (H.HINTS, H.SOURCE_HINTS):
        for row in table:
            assert len(row) == 4
            _id, rx, hint, warn_only = row
            assert isinstance(_id, str) and hasattr(rx, "search")
            assert isinstance(hint, str) and hint and isinstance(warn_only, bool)
    ids = [r[0] for r in H.HINTS] + [r[0] for r in H.SOURCE_HINTS]
    assert len(ids) == len(set(ids))
