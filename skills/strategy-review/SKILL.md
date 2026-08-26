---
name: strategy-review
description: "Audit a trading strategy against the owner's 5-block framework and 27-item checklist (source: vault note 02). Use when reviewing a strategy definition, a new setup, or a backtested system for structural soundness. Zero tools - judgment against a fixed framework. Triggers: review this strategy, audit the system, check the 5 blocks, 27 checklist, is this strategy sound."
---

# Strategy review — the 5-block audit

Audit a strategy against the owner's framework (vault note `02 - Каркас 5 блоков и
чек-лист`). This is **the owner's checklist, not a generic one** — do not substitute
a textbook framework. The governing rule:

> **An empty field is not "I'll fill it later" — it is a found gap.**

## The 5 blocks

| # | Block | The question it answers |
|---|---|---|
| 1 | **Периметр** (perimeter) | WHAT do I trade and WHEN must I not trade? |
| 2 | **Контекст** (context) | WHERE are we going, decided before the session, and on what basis? |
| 3 | **Модель входа** (entry model) | HOW exactly do I enter and where is the stop? |
| 4 | **Риск и ведение позиции** (risk & management) | HOW MUCH is on the line and how does the trade end? |
| 5 | **Обратная связь** (feedback) | DOES it work, and how is the system allowed to change? |

Block 5 is the only one that separates a system from a set of notes.

## Audit method

1. Walk the 27-item checklist in `references/27-item-checklist.md`.
2. Score each: **Да = 1**, **Частично = 0.5**. Total out of 27
   (`=COUNTIF(...,"Да") + 0.5*COUNTIF(...,"Частично")` — keep the formula cell out
   of the range to avoid circular-reference errors).
3. For every item scored below 1, name the concrete gap and a fix.
4. Check the 7 systemic gaps (`references/27-item-checklist.md` §Gaps) — the most
   common failures across the owner's collection.

## Required-fields quick check (per block)

- **Периметр**: closed instrument list · sessions/OTT windows in clock hours ·
  allowed weekdays · news blackout · account/base volume. Done when something is
  fully banned at a specific time.
- **Контекст**: LT/IT/ST ladder, one job per level · named context element (liquidity
  sweep / BOS / period open) · target set before entry · validation condition ·
  **invalidation condition** (what kills the idea — missing here is the #2 gap).
- **Вход**: 2–3 named setups max · ordered trigger sequence · confirmation checklist ·
  order type · exact SL rule · minimum RR. Done when a stranger can find the same entry.
- **Риск**: risk per trade % · risk per day/week % · max trades/day · trading-stop
  trigger · BE rule · partial-close rule · target/exit rule · add-on rule.
- **Обратная связь**: trade journal · per-setup WR • avg RR • sample size · backtest
  with own stats · pre-trade routine · error review · review cadence · rule-change
  protocol. Done when a setup under ~30 trades is not traded at real risk.

## Judgment rules

- Fewer than 30 trades per setup = no real-risk conclusion (WR/avg RR are noise).
- Rules are reviewed weekly; rule edits monthly; **no edits between reviews**.
- Any setup list wider than 2–3 splits the sample to uselessness.
- An edge that is never measured is an opinion.

## References

- `references/27-item-checklist.md` — the full checklist + the 7 systemic gaps.