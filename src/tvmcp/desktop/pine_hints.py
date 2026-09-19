"""Landmine-derived Pine hints attached to compile errors (M8 part B, pure).

`hints_for(errors, warnings, source)` maps Monaco marker messages (and a few
source patterns) to short, actionable hints. Hints are attached only when the
compile produced at least one error - a clean compile never gets advice - and
at most three unique hints come back, in table order.

Each table row is `(id, regex, hint, warn_only)`:
- `HINTS` regexes run over marker messages. `warn_only=False` rows match
  error messages only; `warn_only=True` rows are advisory and match error and
  warning messages alike.
- `SOURCE_HINTS` regexes run over the script source (DOTALL) and are always
  advisory.

Texts are ours; the patterns come from the errors the owner's scripts really
hit while porting to Pine v6.
"""

from __future__ import annotations

import re

MAX_HINTS = 3

_NAMESPACES = (
    "strategy|ta|math|str|array|input|color|matrix|map|label|line|box|table|"
    "request|syminfo|barstate|timeframe|session|plot|shape|location|size|"
    "format|display|dayofweek|position|text|font|xloc|yloc|extend|order|scale|"
    "currency|alert|adjustment|earnings|dividends|splits|ticker|linefill|"
    "polyline|chart|log|runtime|hline|backadjustment|settlement_as_close|"
    "splits|dividends|barmerge|scale|ta"
)

HINTS: list[tuple[str, re.Pattern, str, bool]] = [
    (
        "strategy_fixed_const",
        re.compile(r"undeclared identifier\s*[\"'`]?strategy\.fixed", re.I),
        "`strategy.fixed` exists only inside a strategy() script, as the "
        "`default_qty_type` constant of the strategy() call itself. It is undeclared "
        "in an indicator() and cannot be stored in a variable or an input.",
        False,
    ),
    (
        "strategy_const_args",
        re.compile(r"cannot call\s*[\"'`]?strategy[\"'`]?\s*with argument.*?call\s*[\"'`]?input\.", re.I | re.S),
        "strategy() arguments must be compile-time constants: an `input.*()` result "
        "is not allowed there. Hard-code the value in strategy() and read inputs "
        "later (e.g. as the `qty` of strategy.entry).",
        False,
    ),
    (
        "input_time_defval",
        re.compile(r"input\.time.*?(defval|expected .*?(int|const)|string)", re.I | re.S),
        "`input.time` takes a unix-millisecond constant as defval - write "
        "`timestamp(\"2024-01-01 00:00 +0000\")`, not a date string.",
        False,
    ),
    (
        "condition_must_be_bool",
        re.compile(r"(if|while)\b.*?(condition|expression).*?\bbool\b|cannot use .* as (a )?condition|"
                   r"condition.*?must be (of type )?(series |simple )?bool", re.I | re.S),
        "Pine v6 requires a bool in `if`/`while` conditions: compare explicitly "
        "(`x != 0`, `not na(x)`) instead of relying on a number being truthy.",
        False,
    ),
    (
        "ta_in_conditional",
        re.compile(r"\bta\.\w+.*?(inside|within|conditional|local scope|not executed on each bar|"
                   r"inconsistent)|(local scope|conditional).*?\bta\.\w+", re.I | re.S),
        "A ta.* call inside an if/ternary branch runs only on some bars, so its history "
        "is inconsistent. Compute the ta.* value on every bar in the global scope and "
        "use the variable inside the condition.",
        True,
    ),
    (
        "alertcondition_in_strategy",
        re.compile(r"alertcondition.*?strateg", re.I | re.S),
        "alertcondition() is ignored in strategy() scripts - use alert() or the "
        "`alert_message` argument of strategy.entry/exit instead.",
        True,
    ),
    (
        "shorttitle_too_long",
        re.compile(r"shorttitle.*?(too long|long|characters|symbols|chars)|"
                   r"(short title|shorttitle).*?(10|ten)", re.I | re.S),
        "`shorttitle` must be 10 characters or fewer; TradingView refuses the "
        "compile otherwise.",
        True,
    ),
    (
        "undeclared_identifier",
        re.compile(r"undeclared identifier\s*[\"'`]?(?!(?:" + _NAMESPACES + r")\.)([A-Za-z_][\w.]*)", re.I),
        "Undeclared identifier: the name is misspelled or used before it is assigned. "
        "In v6 most built-ins are namespaced (ta.sma, math.abs, str.tostring) and "
        "variables must be declared (`var x = ...` or `x = ...`) before use.",
        False,
    ),
]

SOURCE_HINTS: list[tuple[str, re.Pattern, str, bool]] = [
    (
        "outdated_version",
        re.compile(r"^\s*//@version=([1-5])\b", re.M),
        "The script targets an old Pine version (//@version=1-5); v6 is current. "
        "Update the header to //@version=6 and expect namespace/typing changes "
        "(bool conditions, ta.* prefixes).",
        True,
    ),
    (
        "calc_on_every_tick_absent",
        re.compile(r"\A(?!.*calc_on_every_tick).*\bstrategy\s*\(", re.S),
        "strategy() has no calc_on_every_tick: fills are computed on bar close only. "
        "Say so when reporting results, or set calc_on_every_tick=true when intrabar "
        "entries matter.",
        True,
    ),
]


def _messages(markers) -> list[str]:
    out = []
    for m in markers or []:
        msg = m.get("message") if isinstance(m, dict) else str(m)
        if msg:
            out.append(str(msg))
    return out


def hints_for(errors, warnings, source: str | None = None) -> list[str]:
    """<= MAX_HINTS unique hints, table order; empty unless `errors` is non-empty."""
    err_msgs = _messages(errors)
    if not err_msgs:
        return []
    warn_msgs = _messages(warnings)
    out: list[str] = []

    def add(hint: str) -> None:
        if hint not in out and len(out) < MAX_HINTS:
            out.append(hint)

    for _id, rx, hint, warn_only in HINTS:
        pool = err_msgs + warn_msgs if warn_only else err_msgs
        if any(rx.search(msg) for msg in pool):
            add(hint)
    src = source or ""
    if src:
        for _id, rx, hint, _warn_only in SOURCE_HINTS:
            if rx.search(src):
                add(hint)
    return out


def matched_ids(errors, warnings, source: str | None = None) -> list[str]:
    """Row ids that fire (for tests/diagnostics); same gating as `hints_for`."""
    err_msgs = _messages(errors)
    if not err_msgs:
        return []
    warn_msgs = _messages(warnings)
    ids = [i for i, rx, _h, warn_only in HINTS
           if any(rx.search(m) for m in (err_msgs + warn_msgs if warn_only else err_msgs))]
    if source:
        ids += [i for i, rx, _h, _w in SOURCE_HINTS if rx.search(source)]
    return ids
