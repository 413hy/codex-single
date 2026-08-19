# Model-centered operating contract

## System purpose and authority

The system sends public-market direction analysis to Telegram for human judgment. It never reads private account data and never trades. The model is the only judge of direction and threshold meaning. The host may validate schema, observability, freshness, already-satisfied conditions, target references, identity, version, and executable expressions; it must not invent or numerically tune strategy thresholds.

## Scheduled flow

1. The filter reduces the Bybit USDT perpetual universe to five high-opportunity, sufficiently tradable candidates; its score is not a direction vote.
2. The collector supplies completed and forming 1m/5m/15m/30m/1h/4h price action, turnover, trades, depth, spread, OI series, funding, liquidations, cross-exchange corroboration, BTC/ETH, breadth, source timestamps, coverage, and failures when available.
3. Independently assess all five, then choose exactly two relative-best 30–60 minute directions.
4. The two signal messages are valid once model output and freshness pass. Monitoring generation or activation failure must not erase them.
5. A separate model review converts candidate thresholds into zero or one valid set of one to three rules per symbol. `REJECTED` means no qualified rule exists now and is a normal no-monitoring result, not a failure.
6. Delivery-time completed-candle data is mechanically rebaselined in either direction while the fixed condition remains unmet. Only a condition already crossed or otherwise unexecutable before activation causes that symbol to be re-collected and re-reviewed for up to three rounds. A repair review may return a new rule or a normal no-rule result. Only exhausted technical/infrastructure errors create an actionable failure notice.

## Direction analysis

- Use 1h/4h only as background; completed 30m/15m define the near structure; completed 5m confirms timing; 1m refines ultra-short timing and false breaks.
- Compare continuation against impact exhaustion/reversal. Do not copy the higher-timeframe direction when completed 15m/5m structure has turned. At a mature multi-timeframe extreme, same-direction Top2 ranking needs a completed pullback-and-reacceleration or breakout-acceptance reset; without it, prefer a less-extended structurally complete candidate even if the assessment direction remains with the older trend.
- RSI, OI, funding, trade delta, orderbook, turnover, spread, liquidations, and cross-exchange values cannot decide direction alone.
- Candidate scores and high volatility identify what deserves analysis, not whether to go long or short.
- Output one direction for each visible signal, confidence, market state, approximate take-profit for display only, forming 15m/30m/1h, next complete 15m, completed-structure invalidation, concise summary, details, evidence, and uncertainties.
- In scheduled mode output ranks 1 and 2 exactly once. Other assessments are internal only and have no outlooks or monitoring rules.

## Monitoring meaning

A monitoring rule means: “fresh market evidence now threatens the current direction enough that the model should reassess.” It does not mean the direction is already invalid. The emergency model must return a fresh direction assessment and a fresh rule set whether the old direction is maintained, reversed, or indeterminate.

Each selected symbol may have zero to three rules when monitoring is enabled. No qualified rule is a valid no-monitoring outcome. Every emitted rule must:

- oppose the current direction rather than describe continuation, acceleration, new highs for LONG, new lows for SHORT, buyer strength for LONG, or seller strength for SHORT;
- use a completed price structure or confirmed pivot as the primary condition;
- use microstructure/OI/turnover/funding/liquidation evidence only as optional conjunctions with opposing price structure, never as a standalone wake;
- include a structural anchor, real evidence IDs, same-metric generation baseline, comparator, threshold, hysteresis, confirmation semantics, validity, and a causal threat explanation;
- encode any claimed consecutive completed-candle confirmation in `required_consecutive_observations` (1–3), so prose and executable behavior are identical;
- be unsatisfied at generation, outside ordinary noise, near enough to warn before structural meaning is lost, and useful enough to justify a full recollection and model call;
- prefer the nearest genuine opposing confirmed pivot, range boundary, or re-acceptance level that is unsatisfied, outside ordinary noise, and no farther than the declared formal direction-invalidation price. A confirmed 1m pivot at roughly two 1m ATRs or more may be an early primary structure without higher-timeframe confirmation; closer 1m levels need stronger price-structure or qualified conjunction evidence;
- when no separate intermediate anchor exists, consider the formal structure price only if a faster completed candle can create meaningful review lead time; otherwise return no monitoring rule. Equality with formal invalidation is allowed because crossing only requests review, while thresholds beyond it are rejected. Thus LONG requires formal invalidation <= threshold < current completed baseline and SHORT requires current completed baseline < threshold <= formal invalidation;
- survive an explicit distance audit against the current completed close, confirmed pivot/range, ATR, recent 1m/5m range, spread, and liquidity; the review rationale must explain why an ordinary next one or two 1m candles should not satisfy it and why it is still early enough to be useful;
- treat less than roughly two completed 1m ATRs of separation as presumptive ordinary noise, not an automatic rejection. Keep such a closer level when a confirmed opposing pivot plus one completed higher-timeframe price candle, multi-observation, or price-conjoined confirmation makes the full condition structurally meaningful, and state that exception explicitly;
- audit the full executable trigger. If `required_consecutive_observations > 1`, compare the remaining threshold-to-formal-invalidation gap with ordinary 1m/5m movement. Do not claim early warning when the extra completed-candle wait can reasonably let formal invalidation occur first; use a different earlier structure, an immediate price-conjoined confirmation, or reject monitoring;
- treat `hysteresis` only as the safe-side re-arm buffer after a crossing. The first trigger compares the observation directly with `threshold`; never add or subtract hysteresis when auditing trigger distance;
- prefer completed 5m/15m structure; allow a completed 1m primary to execute against a confirmed 5m/15m pivot, broken range boundary, or re-acceptance level when separation is non-noise and a completed 5m observation risks arriving at formal invalidation. The anchor timeframe and primary observation timeframe need not match;
- exclude take-profit, target arrival, ordinary target-area volatility, and execution/position concepts.

The host consumes the first complete rule hit atomically and permanently retires the entire `(analysis_id, symbol)` set. Sibling rules never fire afterward. It then notifies, recollects the symbol plus market context, and runs emergency review. New rules use a new analysis ID. A failed review never revives old rules.

Automatic repair never authorizes the host to invent or tune a threshold. A valid model `REJECTED` decision is final for that review and is not retried. An activation retry includes the exact already-crossed/unexecutable reason and newly collected evidence, because its purpose is to replace a stale reviewed condition. Permanent authentication, credit, or model-availability failures stop immediately instead of consuming the three activation-repair rounds.

## Concurrent versions

- At `:00/:30`, immediately before the authoritative scheduled cycle starts, old monitoring stops accepting new events. Prior rules remain live up to that boundary so the last five minutes are not blind.
- Accepted emergency reviews continue independently.
- At `:30/:00`, scheduled analysis starts on time and does not wait for emergency work.
- The newest scheduled cycle is authoritative for Top2 and monitoring. A late emergency or manual retry may notify its analysis but may not overwrite a newer scheduled version.

## Failure behavior

For any final failure, identify the workflow, exact step, completed prior steps, direct cause and causal chain, actual impact, recommended resolution, safe diagnostics, and precise retry scope. Technical process/schema retries and the bounded per-symbol monitoring repair state machines run silently. Only after their configured budgets are exhausted does the system wait for an explicit user retry.
