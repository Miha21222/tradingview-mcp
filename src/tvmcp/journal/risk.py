"""Rules-driven risk guard over a normalized journal (bookkeeping, not advice).

This is the *mechanism* half of a trading circuit breaker: loss limits per day /
week / month, trade and losing-trade counts, a consecutive-loss cooldown, the room
left above a prop-firm floor, a rest period after a stop-out, an idle warning and
an optional profit-side guard. Every threshold arrives in a `rules` config and
every account number in an `account` block - this module holds no thresholds, no
firm names and no strategy. It reads a journal and reports whether the user's own
written rules were breached.

Conventions:

- Loss limits are given as `{"pct": x}` (percent of the account), `{"currency": x}`
  or `{"r": x}`, always as a **positive** number; a loss that reaches the limit
  exactly breaches it (`<= -limit`), one tick under does not.
- Count limits are `"max": n` = the largest allowed count; breach when `count > n`.
- A check that cannot be evaluated (no account, no currency, mixed units) reports
  `status: "unknown"` with the reason in `warnings` - it never reports `ok`. An
  unknown check whose configured severity is `stop` lifts the overall status to
  `flag`, so a missing number is visible instead of comfortable.
- `fail_closed` (a journal row that could not be parsed) forces `stop` through the
  always-present `journal_integrity` check.
"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path

from . import schema

DEFAULT_RULES_PATH = Path(__file__).with_name("risk_rules.example.json")

_RANK = {"ok": 0, "unknown": 1, "flag": 2, "stop": 3}
_LOSS_CHECKS = {"daily_loss": "day", "weekly_loss": "week", "monthly_loss": "month"}

KNOWN_CHECKS = frozenset({
    "daily_loss", "weekly_loss", "monthly_loss",
    "max_trades_per_day", "max_losses_per_day", "max_trades_per_week",
    "consecutive_losses", "floor_buffer", "min_rest", "idle",
    "profit_target_day", "profit_target_week",
})


# ---------------------------------------------------------------- rules I/O

def _validate_rules(rules: dict, where: str) -> None:
    """Fail with an actionable message, never a traceback, on a misshapen config.

    `checks` keyed by check id is easy to get wrong - a list of check objects is
    the obvious guess, and it used to die inside the accessor with
    `'list' object has no attribute 'get'`, which tells the caller nothing.
    """
    checks = rules.get("checks")
    if checks is None:
        return
    if isinstance(checks, list):
        named = [c.get("id") for c in checks if isinstance(c, dict) and c.get("id")]
        hint = ""
        if named:
            hint = " e.g. " + json.dumps({"checks": {str(named[0]): {"limit": {"pct": 2}}}})
        raise ValueError(
            f"{where}: `checks` must be an OBJECT keyed by check id, not a list. "
            f"Turn each entry's `id` into the key and drop the `id` field.{hint}"
        )
    if not isinstance(checks, dict):
        raise ValueError(f"{where}: `checks` must be an object keyed by check id")
    bad = sorted(k for k, v in checks.items() if not k.startswith("_") and not isinstance(v, dict))
    if bad:
        raise ValueError(f"{where}: every check must be an object; these are not: {', '.join(bad)}")
    unknown = sorted(k for k in checks if not k.startswith("_") and k not in KNOWN_CHECKS)
    if unknown:
        raise ValueError(
            f"{where}: unknown check(s) {', '.join(unknown)}. "
            f"Known checks: {', '.join(sorted(KNOWN_CHECKS))}"
        )


def load_rules(rules: dict | None = None, rules_path: str | Path | None = None) -> tuple[dict, list[str]]:
    """Resolve the rules config. Falls back to the shipped example, loudly."""
    warnings: list[str] = []
    if rules and rules_path:
        raise ValueError("pass either rules or rules_path, not both")
    if rules is not None:
        if not isinstance(rules, dict):
            raise ValueError("rules must be an object")
        _validate_rules(rules, "rules")
        return rules, warnings
    path = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
    if rules_path and not path.exists():
        raise ValueError(f"rules file not found: {path}")
    if not rules_path:
        warnings.append(
            "no rules supplied - using the shipped generic example "
            f"({DEFAULT_RULES_PATH.name}); its numbers are placeholders, not your limits"
        )
    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name}: rules must be a JSON object")
    _validate_rules(loaded, path.name)
    return loaded, warnings


def _check_cfg(rules: dict, name: str) -> dict | None:
    cfg = (rules.get("checks") or {}).get(name)
    if not isinstance(cfg, dict) or cfg.get("enabled") is False:
        return None
    return cfg


def parse_limit(spec, field: str = "limit") -> tuple[str, float]:
    """`{"pct": 2}` / `{"currency": 200}` / `{"r": 2}` -> ("pct", 2.0)."""
    if not isinstance(spec, dict):
        raise ValueError(f"{field} must name its unit, e.g. {{'pct': 2}} / {{'currency': 200}} / {{'r': 2}}")
    units = [u for u in ("pct", "currency", "r") if spec.get(u) is not None]
    if len(units) != 1:
        raise ValueError(f"{field} must carry exactly one of pct / currency / r; got {sorted(spec)}")
    value = float(spec[units[0]])
    if value < 0:
        raise ValueError(f"{field} must be a positive size, got {value}")
    return units[0], value


# ---------------------------------------------------------------- unit math

class Units:
    """Converts window totals between R, account currency and percent."""

    def __init__(self, account: dict, rules: dict, records: list[dict]):
        self.account = account or {}
        self.currency = self.account.get("currency") or schema.stats(records)["currency"]
        if self.currency == "mixed":
            self.currency = None
        self.balance = _f(self.account.get("balance"))
        self.starting = _f(self.account.get("starting_balance"))
        base_name = rules.get("percent_base", "starting_balance")
        primary, secondary = (self.starting, self.balance) if base_name == "starting_balance" else (self.balance, self.starting)
        self.percent_base = primary if primary is not None else secondary
        self.percent_base_name = base_name if primary is not None else (
            "balance" if base_name == "starting_balance" else "starting_balance"
        )
        self.risk_per_r = _f(self.account.get("risk_per_r"))
        rpr = rules.get("risk_per_r") or {}
        if self.risk_per_r is None and rpr.get("currency") is not None:
            self.risk_per_r = _f(rpr["currency"])
        if self.risk_per_r is None and rpr.get("pct_of_account") is not None and self.percent_base:
            self.risk_per_r = self.percent_base * float(rpr["pct_of_account"]) / 100.0

    def to_unit(self, totals: dict, unit: str) -> tuple[float | None, str]:
        """Window total expressed in `unit`; (None, reason) when unconvertible."""
        if unit == "r":
            if totals["all_have_r"] and totals["r"] is not None:
                return totals["r"], "journal R"
            if totals["all_have_pnl"] and totals["pnl"] is not None and self.risk_per_r:
                return totals["pnl"] / self.risk_per_r, "journal money / risk_per_r"
            return None, "not every trade in the window carries an R-multiple, and no risk_per_r to convert money with"
        money, basis = self._currency(totals)
        if money is None:
            return None, basis
        if unit == "currency":
            return money, basis
        if not self.percent_base:
            return None, "no account size (account.starting_balance / balance) to take a percent of"
        return money / self.percent_base * 100.0, f"{basis} / {self.percent_base_name}"

    def _currency(self, totals: dict) -> tuple[float | None, str]:
        if totals["all_have_pnl"] and totals["pnl"] is not None:
            return totals["pnl"], f"journal {self.currency or 'currency'}"
        if totals["all_have_r"] and totals["r"] is not None and self.risk_per_r:
            return totals["r"] * self.risk_per_r, "journal R x risk_per_r"
        if totals["count"] == 0:
            return 0.0, "empty window"
        return None, "no money on the trades and no risk_per_r to convert R with"

    def limit_currency(self, unit: str, value: float) -> float | None:
        if unit == "currency":
            return value
        if unit == "pct":
            return self.percent_base * value / 100.0 if self.percent_base else None
        return value * self.risk_per_r if self.risk_per_r else None


def _f(v) -> float | None:
    return None if v is None else float(v)


# ---------------------------------------------------------------- windows

def _as_date(value, field: str = "today") -> _date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    parsed = schema.parse_date(value, field=field)
    if parsed is None:
        raise ValueError(f"{field} is required")
    return parsed


def _rec_date(rec: dict) -> _date | None:
    d = rec.get("date")
    return schema.parse_date(d) if d else None


def _rec_time(rec: dict) -> datetime | None:
    for key in ("closed_at", "opened_at"):
        if rec.get(key):
            return schema.parse_datetime(rec[key], field=key)
    return None


def _week_bounds(today: _date, mode: str, rolling_days: int, week_start: str) -> tuple[_date, _date]:
    if mode == "rolling":
        return today - timedelta(days=max(rolling_days, 1) - 1), today
    offset = (today.weekday() + 1) % 7 if week_start == "sunday" else today.weekday()
    return today - timedelta(days=offset), today


def _month_bounds(today: _date, mode: str, rolling_days: int) -> tuple[_date, _date]:
    if mode == "rolling":
        return today - timedelta(days=max(rolling_days, 1) - 1), today
    return today.replace(day=1), today


def _totals(records: list[dict]) -> dict:
    rs = [r["r_multiple"] for r in records if r.get("r_multiple") is not None]
    ps = [r["pnl"] for r in records if r.get("pnl") is not None]
    outcomes = [schema.outcome(r) for r in records]
    return {
        "count": len(records),
        "wins": outcomes.count("win"),
        "losses": outcomes.count("loss"),
        "r": round(sum(rs), 4) if rs else (0.0 if not records else None),
        "pnl": round(sum(ps), 2) if ps else (0.0 if not records else None),
        "all_have_r": len(rs) == len(records),
        "all_have_pnl": len(ps) == len(records),
    }


def sessions_between(a: _date, b: _date, trading_days: set[int], journal_dates: set[_date] | None = None) -> int:
    """Trading sessions strictly after `a` up to and including `b` (0 when b <= a).

    Sessions, not calendar days: with the default Mon-Fri calendar a Friday loss is
    one session away from Monday, not three days.
    """
    if b <= a:
        return 0
    if journal_dates is not None:
        return len([d for d in journal_dates if a < d <= b])
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() in trading_days:
            n += 1
    return n


# ---------------------------------------------------------------- the guard

def evaluate(
    records: list[dict],
    rules: dict,
    today=None,
    account: dict | None = None,
    now=None,
    fail_closed: bool = False,
    problems: list | None = None,
) -> dict:
    """Run every configured check over `records`. Pure: no I/O, no clock reads."""
    account = dict(account or {})
    problems = list(problems or [])
    warnings: list[str] = []

    when = now if now is not None else today
    now_dt: datetime | None = None
    if isinstance(when, datetime):
        now_dt = when
    elif isinstance(when, str) and len(when.strip()) > 10:
        now_dt = schema.parse_datetime(when, field="today")
    session = _as_date(today if today is not None else when)

    session_cfg = rules.get("session") or {}
    trading_days = set(session_cfg.get("trading_days") or [0, 1, 2, 3, 4])
    week_start = str(session_cfg.get("week_start", "monday")).lower()
    calendar_mode = str(session_cfg.get("session_calendar", "weekdays")).lower()

    dated = [(r, _rec_date(r)) for r in records]
    undated = [r for r, d in dated if d is None]
    if undated:
        warnings.append(f"{len(undated)} record(s) have no session date and sit outside every window")
    past = sorted([(r, d) for r, d in dated if d is not None and d <= session], key=lambda rd: (rd[1], rd[0].get("closed_at") or ""))
    journal_dates = {d for _, d in past} if calendar_mode == "journal" else None

    units = Units(account, rules, [r for r, _ in past])

    def window(start: _date, end: _date) -> list[dict]:
        return [r for r, d in past if start <= d <= end]

    week_cfg = _check_cfg(rules, "weekly_loss") or {}
    month_cfg = _check_cfg(rules, "monthly_loss") or {}
    week_from, week_to = _week_bounds(
        session, str(week_cfg.get("window", "calendar")).lower(),
        int(week_cfg.get("rolling_days", 7)), week_start,
    )
    month_from, month_to = _month_bounds(
        session, str(month_cfg.get("window", "calendar")).lower(),
        int(month_cfg.get("rolling_days", 30)),
    )
    spans = {
        "day": (session, session),
        "week": (week_from, week_to),
        "month": (month_from, month_to),
    }
    buckets = {name: window(*span) for name, span in spans.items()}
    totals = {name: _totals(rows) for name, rows in buckets.items()}

    checks: list[dict] = []

    def add(id_: str, name: str, status: str, detail: str, numbers: dict | None = None) -> None:
        checks.append({"id": id_, "name": name, "status": status, "detail": detail, "numbers": numbers or {}})

    def unknown(id_: str, name: str, cfg: dict, reason: str, numbers: dict | None = None) -> None:
        warnings.append(f"{id_}: not evaluated - {reason}")
        add(id_, name, "unknown", reason, {**(numbers or {}), "configured_status": cfg.get("status", "stop")})

    # E0 generalized: a journal that could not be fully parsed never reports ok.
    add(
        "journal_integrity",
        "journal parses cleanly",
        "stop" if fail_closed else "ok",
        (f"{len(problems)} unparseable row(s); fix the journal and rerun: "
         + "; ".join(str(p.get("reason", p)) for p in problems[:3])) if fail_closed
        else f"{len(records)} record(s) parsed, no problems",
        {"problems": len(problems), "records": len(records)},
    )

    # ---- loss limits per day / week / month
    for check_id, span_name in _LOSS_CHECKS.items():
        cfg = _check_cfg(rules, check_id)
        if cfg is None:
            continue
        start, end = spans[span_name]
        t = totals[span_name]
        title = f"{span_name} loss limit"
        try:
            unit, value = parse_limit(cfg.get("limit"), f"{check_id}.limit")
        except ValueError as exc:
            unknown(check_id, title, cfg, str(exc))
            continue
        total, basis = units.to_unit(t, unit)
        if total is None:
            unknown(check_id, title, cfg, basis, {"window": [start.isoformat(), end.isoformat()], "trades": t["count"]})
            continue
        breached = total <= -value
        add(
            check_id, title,
            cfg.get("status", "stop") if breached else "ok",
            (f"{span_name} result {round(total, 3)} {unit} reached the -{value} {unit} limit"
             if breached else
             f"{span_name} result {round(total, 3)} {unit}, limit -{value} {unit}"),
            {
                "window": [start.isoformat(), end.isoformat()],
                "window_mode": str(cfg.get("window", "calendar")).lower() if check_id != "daily_loss" else "session",
                "trades": t["count"], "total": round(total, 4), "unit": unit,
                "limit": value, "remaining": round(value + total, 4), "basis": basis,
                "total_r": t["r"], "total_currency": t["pnl"],
            },
        )

    # ---- count limits
    for check_id, span_name, losers_only, title in (
        ("max_trades_per_day", "day", False, "trades per day"),
        ("max_losses_per_day", "day", True, "losing trades per day"),
        ("max_trades_per_week", "week", False, "trades per week"),
    ):
        cfg = _check_cfg(rules, check_id)
        if cfg is None:
            continue
        limit = cfg.get("max", cfg.get("limit"))
        if limit is None:
            unknown(check_id, title, cfg, f"{check_id}.max is not set")
            continue
        limit = int(limit)
        count = totals[span_name]["losses"] if losers_only else totals[span_name]["count"]
        breached = count > limit
        add(
            check_id, title,
            cfg.get("status", "stop") if breached else "ok",
            f"{count} of at most {limit} {'losing ' if losers_only else ''}trade(s) this {span_name}",
            {"count": count, "max": limit, "window": [spans[span_name][0].isoformat(), spans[span_name][1].isoformat()]},
        )

    # ---- consecutive-loss cooldown
    cfg = _check_cfg(rules, "consecutive_losses")
    if cfg is not None:
        need = int(cfg.get("losses", 2))
        cooldown = int(cfg.get("cooldown_sessions", 1))
        reset_tag = cfg.get("reset_tag")
        streak, last_loss = 0, None
        for rec, d in reversed(past):
            if schema.outcome(rec) == "loss":
                streak += 1
                last_loss = last_loss or d
            elif schema.outcome(rec) == "unknown":
                continue
            else:
                break
        reset_done = False
        if reset_tag and last_loss is not None:
            reset_done = any(
                reset_tag in (rec.get("tags") or []) and d >= last_loss for rec, d in past
            )
        elapsed = sessions_between(last_loss, session, trading_days, journal_dates) if last_loss else None
        breached = bool(streak >= need and last_loss is not None and elapsed is not None
                        and elapsed <= cooldown and not reset_done)
        add(
            "consecutive_losses", "cooldown after consecutive losses",
            cfg.get("status", "stop") if breached else "ok",
            (f"{streak} loss(es) in a row, last on {last_loss}; {elapsed} of {cooldown} cooldown session(s) passed"
             if last_loss else "no losing streak"),
            {"streak": streak, "losses_required": need, "cooldown_sessions": cooldown,
             "sessions_since_last_loss": elapsed, "last_loss": last_loss.isoformat() if last_loss else None,
             "reset_tag": reset_tag, "reset_seen": reset_done,
             "session_calendar": calendar_mode},
        )

    # ---- distance to the account floor
    cfg = _check_cfg(rules, "floor_buffer")
    account_block = _account_block(past, account, units, warnings)
    if cfg is not None:
        try:
            unit, value = parse_limit(cfg.get("buffer"), "floor_buffer.buffer")
        except ValueError as exc:
            unknown("floor_buffer", "room above the account floor", cfg, str(exc))
        else:
            buffer_currency = units.limit_currency(unit, value)
            room = account_block.get("room_to_floor")
            if room is None or buffer_currency is None:
                unknown("floor_buffer", "room above the account floor", cfg,
                        account_block.get("note") or "no account balance / floor to measure the room to",
                        {k: account_block.get(k) for k in ("equity", "floor", "room_to_floor")})
            else:
                breached = room < buffer_currency
                add(
                    "floor_buffer", "room above the account floor",
                    cfg.get("status", "stop") if breached else "ok",
                    (f"{round(room, 2)} left above the floor, below the {round(buffer_currency, 2)} buffer"
                     if breached else
                     f"{round(room, 2)} left above the floor (buffer {round(buffer_currency, 2)})"),
                    {"equity": account_block.get("equity"), "floor": account_block.get("floor"),
                     "floor_type": account_block.get("floor_type"),
                     "room_to_floor": round(room, 2), "room_to_floor_r": account_block.get("room_to_floor_r"),
                     "buffer": value, "buffer_unit": unit, "buffer_currency": round(buffer_currency, 2)},
                )

    # ---- minimum rest after a stop-out
    cfg = _check_cfg(rules, "min_rest")
    if cfg is not None:
        last_loss_rec = next(((rec, d) for rec, d in reversed(past) if schema.outcome(rec) == "loss"), None)
        if last_loss_rec is None:
            add("min_rest", "rest after a stop-out", "ok", "no losing trade on record", {})
        elif cfg.get("minutes") is not None:
            minutes = float(cfg["minutes"])
            loss_time = _rec_time(last_loss_rec[0])
            if now_dt is None or loss_time is None:
                unknown("min_rest", "rest after a stop-out", cfg,
                        "needs a timestamped `today` (ISO datetime) and a closed_at on the last loss",
                        {"minutes_required": minutes})
            else:
                elapsed = (now_dt - loss_time).total_seconds() / 60.0
                breached = elapsed < minutes
                add("min_rest", "rest after a stop-out",
                    cfg.get("status", "stop") if breached else "ok",
                    f"{round(elapsed, 1)} min since the last stop-out, {minutes} min required",
                    {"minutes_elapsed": round(elapsed, 1), "minutes_required": minutes,
                     "last_loss_at": loss_time.isoformat()})
        else:
            need = int(cfg.get("sessions", 1))
            elapsed = sessions_between(last_loss_rec[1], session, trading_days, journal_dates)
            breached = elapsed < need
            add("min_rest", "rest after a stop-out",
                cfg.get("status", "stop") if breached else "ok",
                f"{elapsed} of {need} rest session(s) since the last stop-out",
                {"sessions_elapsed": elapsed, "sessions_required": need,
                 "last_loss": last_loss_rec[1].isoformat()})

    # ---- idle warning (never a stop)
    cfg = _check_cfg(rules, "idle")
    if cfg is not None:
        last = past[-1][1] if past else None
        if last is None:
            add("idle", "idle since the last trade", "ok", "journal is empty - no history, no idle window", {})
        else:
            if cfg.get("days") is not None:
                need, elapsed, unit_name = int(cfg["days"]), (session - last).days, "day"
            else:
                need = int(cfg.get("sessions", 20))
                elapsed, unit_name = sessions_between(last, session, trading_days, journal_dates), "session"
            breached = elapsed >= need
            status = cfg.get("status", "flag")
            if status == "stop":
                status = "flag"
                warnings.append("idle is a soft check; its configured `stop` was downgraded to flag")
            add("idle", "idle since the last trade", status if breached else "ok",
                f"{elapsed} {unit_name}(s) since the last trade on {last}, warn at {need}",
                {f"{unit_name}s_idle": elapsed, "warn_at": need, "last_trade": last.isoformat()})

    # ---- profit-side guard
    for check_id, span_name, title in (
        ("profit_target_day", "day", "daily profit target"),
        ("profit_target_week", "week", "weekly profit target"),
    ):
        cfg = _check_cfg(rules, check_id)
        if cfg is None:
            continue
        try:
            unit, value = parse_limit(cfg.get("target"), f"{check_id}.target")
        except ValueError as exc:
            unknown(check_id, title, cfg, str(exc))
            continue
        total, basis = units.to_unit(totals[span_name], unit)
        if total is None:
            unknown(check_id, title, cfg, basis)
            continue
        reached = total >= value
        add(check_id, title,
            cfg.get("status", "flag") if reached else "ok",
            (f"{span_name} result {round(total, 3)} {unit} reached the {value} {unit} target - "
             "your own rules say to stop here" if reached else
             f"{span_name} result {round(total, 3)} {unit}, target {value} {unit}"),
            {"total": round(total, 4), "unit": unit, "target": value, "basis": basis})

    status = "ok"
    for c in checks:
        effective = c["status"]
        if effective == "unknown":
            effective = "flag" if c["numbers"].get("configured_status") == "stop" else "ok"
        if _RANK[effective] > _RANK[status]:
            status = effective

    return {
        "status": status,
        "checks": checks,
        "account": account_block,
        "window": {
            "today": session.isoformat(),
            "now": now_dt.isoformat() if now_dt else None,
            "session_calendar": calendar_mode,
            "trading_days": sorted(trading_days),
            "day": {"span": [spans["day"][0].isoformat(), spans["day"][1].isoformat()], **totals["day"]},
            "week": {"span": [week_from.isoformat(), week_to.isoformat()],
                     "mode": str(week_cfg.get("window", "calendar")).lower(), **totals["week"]},
            "month": {"span": [month_from.isoformat(), month_to.isoformat()],
                      "mode": str(month_cfg.get("window", "calendar")).lower(), **totals["month"]},
            "records_in_scope": len(past),
        },
        "fail_closed": bool(fail_closed),
        "warnings": warnings,
    }


def _account_block(past: list[tuple[dict, _date]], account: dict, units: Units, warnings: list[str]) -> dict:
    """Equity, floor (static or trailing) and the room between them - all optional."""
    block: dict = {
        "currency": units.currency,
        "balance": units.balance,
        "starting_balance": units.starting,
        "risk_per_r": round(units.risk_per_r, 4) if units.risk_per_r else None,
        "percent_base": units.percent_base,
        "equity": None, "floor": None, "floor_type": None, "high_water_mark": None,
        "room_to_floor": None, "room_to_floor_r": None, "note": None,
    }
    if not past and units.balance is None and units.starting is None:
        block["note"] = "no account block supplied"
        return block

    asof = schema.parse_date(account.get("balance_asof")) if account.get("balance_asof") else None

    def money(rec: dict) -> float | None:
        if rec.get("pnl") is not None:
            return rec["pnl"]
        if rec.get("r_multiple") is not None and units.risk_per_r:
            return rec["r_multiple"] * units.risk_per_r
        return None

    values = [(money(rec), d) for rec, d in past]
    if any(v is None for v, _ in values):
        block["note"] = "some trades carry neither money nor a convertible R; equity is not estimated"
        return block

    realized_since = sum(v for v, d in values if asof is None or d > asof)
    if units.balance is not None:
        equity = units.balance + (realized_since if asof else 0.0)
    elif units.starting is not None:
        equity = units.starting + sum(v for v, _ in values)
    else:
        block["note"] = "no account.balance / starting_balance to estimate equity from"
        return block
    block["equity"] = round(equity, 2)

    floor_type = str(account.get("floor_type", "static")).lower()
    block["floor_type"] = floor_type
    max_loss = None
    if account.get("max_loss_pct") is not None and (units.starting or units.balance):
        max_loss = (units.starting or units.balance) * float(account["max_loss_pct"]) / 100.0
    floor = _f(account.get("floor"))
    if floor_type == "trailing":
        start = units.starting if units.starting is not None else equity - sum(v for v, _ in values)
        running, peak = start, start
        for v, _ in values:
            running += v
            peak = max(peak, running)
        peak = max(peak, equity, _f(account.get("high_water_mark")) or peak)
        block["high_water_mark"] = round(peak, 2)
        if max_loss is None and floor is not None and units.starting is not None:
            max_loss = units.starting - floor  # an explicit starting floor defines the trail distance
        if max_loss is None:
            block["note"] = "trailing floor needs account.max_loss_pct (or floor + starting_balance)"
            return block
        floor = peak - max_loss
    elif floor is None:
        if max_loss is None:
            block["note"] = "no account.floor and no account.max_loss_pct - floor unknown"
            return block
        floor = (units.starting if units.starting is not None else equity) - max_loss

    block["floor"] = round(floor, 2)
    room = equity - floor
    block["room_to_floor"] = round(room, 2)
    if units.risk_per_r:
        block["room_to_floor_r"] = round(room / units.risk_per_r, 2)
    else:
        warnings.append("room to the floor is not expressed in R: no risk_per_r configured")
    return block
