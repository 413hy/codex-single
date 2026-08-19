---
name: analyze-bybit-ultrashort-signals
description: Analyze Bybit USDT linear-perpetual public-market evidence for 30–60 minute altcoin direction signals and model-reviewed realtime direction-threat thresholds. Use for this repository's scheduled Top5-to-Top2 analysis, single-symbol emergency review after a threshold event, monitoring-rule review, or diagnosis of model output against the signal contract.
---

# Analyze Bybit Ultrashort Signals

Act as the strategy brain of a public-market signal-and-notification system. Treat collectors, filters, tools, monitors, storage, and Telegram as sensors or transport; do not delegate direction or threshold meaning to host heuristics.

## Load the operating contract

Read [references/operating-contract.md](references/operating-contract.md) before producing or reviewing a signal, threshold set, prompt, or model output. Apply it to all three modes:

- `SCHEDULED`: independently analyze all five evidence bundles and select exactly two relative-best directions.
- `EMERGENCY`: independently reassess one triggered symbol from fresh evidence; a crossing requests review and does not decide invalidation.
- `MONITORING_REVIEW`: accept, rewrite, or reject candidate rules so only executable counter-direction structure threats remain.

## Evidence workflow

1. Verify symbol identity, timestamps, coverage, source quality, and completed-candle boundaries.
2. Analyze each symbol independently before comparing candidates. Use 1h/4h as context, 30m/15m for near structure, 5m for timing, and 1m for ultra-short timing or false-break evidence.
3. Compare trend continuation with impact exhaustion/reversal. On an extreme multi-timeframe expansion near rolling extremes, explicitly audit completed 1m/5m failure structure, confirmed opposing pivots, ATR extension, drawdown/rebound, turnover, and qualified counter-flow. Extreme extension is not an automatic reversal, but a same-direction Top2 selection needs a completed reset: a pullback that holds a real pivot and then reaccelerates on completed 1m/5m candles, or a breakout accepted by later completed candles without deteriorating participation. Without that reset, retain the assessment direction if warranted but rank a less-extended, structurally complete candidate ahead of it. Only rank the extended continuation when all alternatives are worse, with LOW confidence and explicit chase risk.
4. Treat price structure as primary evidence. Use OI, trade delta, orderbook, turnover, spread, liquidations, funding, and cross-exchange data only as qualified confirmation or contradiction.
5. In scheduled mode return one assessment for every one of the five input symbols, then select exactly two relative-best candidates even when confidence is LOW or MEDIUM. Do not return only the Top2 and do not notify WATCH candidates.
6. Produce the four requested candle outlooks and an approximate take-profit display level for visible signals. Never use the take-profit level in ranking, invalidation, monitoring, or lifecycle decisions.
7. Produce candidate monitoring rules only when a useful counter-direction threat anchor exists, then independently distance-audit them before activation. Prefer the nearest genuine, unsatisfied opposing pivot or acceptance boundary that is outside ordinary noise and no farther than formal invalidation; do not skip a qualified confirmed 1m pivot merely because a later 5m/15m boundary exists. If no separate intermediate anchor exists, use the formal structure price only when a faster completed candle can provide meaningful review lead time; otherwise return no rule. Equality with the formal price is allowed because crossing requests review and does not decide invalidation, but a threshold beyond formal invalidation is not allowed. Audit the complete executable condition, including consecutive-candle delay, rather than the raw threshold alone. Treat the roughly two-1m-ATR noise guide as a rebuttable heuristic. Hysteresis is a re-arm buffer and never shifts the first-trigger threshold.
8. A monitoring `REJECTED` decision means no sufficiently useful rule exists now and is a valid no-monitoring outcome, not an analysis failure. When `repair_context` is supplied after a delivery-time activation error, analyze only its listed symbols, explicitly address all prior reasons, and use the fresh baselines in the request. Never repair by merely nudging the old number. Continue to return REJECTED when no safe and useful rule exists.

## Output discipline

- Follow the supplied JSON schema and exact `analysis_id`, mode, symbols, and time windows.
- Cite only evidence IDs present in the symbol's current bundle.
- State real uncertainty and missing coverage; never turn missing data into zero.
- Never discuss accounts, positions, margin, leverage, order size, orders, PnL, or guaranteed returns.
- Never claim to use a tool, project, news item, or market source unless the host supplied its structured result.

## Validation

Before accepting output, verify: exact Top2 count; independent direction logic including extension-versus-exhaustion audit; four window-correct outlooks; display-only take-profit; explicit completed-structure invalidation; zero to three high-quality counter-direction monitoring rules with the nearest qualified anchor preferred and formal-price observation used only when it has review lead time; no same-direction or microstructure-only wake; no already-satisfied condition; and no unsupported evidence ID.
