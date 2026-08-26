# 27-item audit checklist (owner's framework, vault note 02)

Score: Да = 1, Частично = 0.5. Total out of 27. A blank field is a found gap.

## 1. Периметр (perimeter)
1. Closed instrument list; nothing outside it is traded without editing the strategy
2. Session/OTT windows recorded in clock hours, not words like "morning"
3. Allowed weekdays and a news-blackout rule

## 2. Контекст (context)
4. Timeframe ladder (LT/IT/ST), one job per level
5. Named context-forming element (sweep / BOS / period open)
6. Target set before entry (point B / FTA)
7. Validation condition written
8. **Invalidation condition written** (what kills the idea — the #2 gap if missing)

## 3. Вход (entry model)
9. Finite list of named setups
10. Ordered trigger sequence per setup
11. Order type specified (market / limit)
12. Exact SL placement rule (not "behind structure")
13. Minimum RR below which the trade is skipped

## 4. Риск и ведение позиции (risk & management)
14. Risk per trade in %
15. Risk per day and per week in %
16. Maximum trades per day
17. Trading-stop trigger (loss streak, daily cap)
18. Move-to-break-even rule
19. Partial-close rule
20. Target and exit rule

## 5. Обратная связь (feedback)
21. Journal entry for every trade
22. Win Rate / Average RR / sample size per setup
23. Backtest with own statistics
24. Pre-trade checklist
25. Error review
26. Fixed review cadence
27. Rule-change protocol

## The 7 systemic gaps (most common across the collection)

| # | Gap | Why it matters | Fix |
|---|---|---|---|
| 1 | Edge never measured | rules without numbers are opinions | per-setup WR/avg RR/sample; ~30 min |
| 2 | No invalidation condition | an idea can never be wrong → losses get held | one event that kills the idea |
| 3 | No rule-change protocol | rules drift after every loss | stats weekly, edits monthly, frozen between |
| 4 | Content locked in images | screenshots aren't searchable/checklist-able | rules as text, charts as illustration |
| 5 | Risk as topic, not number | position size is the survival variable | risk/trade, risk/day, trade cap, stop trigger on page 1 |
| 6 | Too-wide setup front | attention splits, samples stay tiny | trim to 2–3 until each has a sample |
| 7 | No pre-trade checklist | the gap between written and followed system | one page, <10 lines, read pre-session |

## Reduced version

If only three blocks fit: **Контекст, Модель входа, Риск**. Perimeter folds into
Context; Feedback is added after the first ~20 trades.