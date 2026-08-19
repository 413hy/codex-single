# 当前任务：主信号监测规则独立复核

你已经完成方向分析；现在只复核规则，不重新选择 Top2，也不修改信号方向、置信度、止盈位、K线展望或失效描述。遵守前置注入的模型操作手册，输出宿主提供的 `ModelMonitoringReviewResponse` Schema JSON。

对每个输入主信号恰好返回一个 review：

1. 先解释候选规则是否真的表示“当前方向遭受反向价格结构威胁，值得重采和重新分析”，而非同方向延续、普通噪声、目标到达或为了通知而通知。
2. 可 ACCEPTED、REWRITTEN 或 REJECTED。不要因为候选有错而勉强保留；需要时基于 fresh evidence 完整重写。REJECTED 的 directives 为空，表示当前没有值得持续监测的高质量结构阈值，是允许的正常结果，不是流程异常。
3. ACCEPTED/REWRITTEN 最终只能有 1–3 条规则。每条规则的 primary metric 必须是反方向的已完成 1m/5m/15m/30m/1h 收盘结构；优先 5m/15m。执行指标周期不必等于结构锚点周期：若等待完成5m可能与正式失效同柱发生，可用一根完成1m收盘去监测已确认的5m/15m pivot、区间边界、重新接受位或仍具提前量的正式失效结构价，并按需配合可靠反向价格/成交组合确认；阈值不必是1m自己的pivot。候选值只是普通1m噪声、缺少真实价格结构来源、与正式失效几乎同时且没有复核提前量，或完整条件在生成时已经满足时，应 REJECTED。
4. OI、trade delta、orderbook、turnover、spread、funding、liquidation 等不得独立唤醒，只能作为同一规则 `confirmations` 中的组合条件。没有可靠覆盖时不要使用。
5. LONG 禁止用继续上涨、新高、买方增强作为唤醒；SHORT 禁止用继续下跌、新低、卖方增强作为唤醒。阈值 crossing 只是让模型介入，不直接宣告方向失效。
6. take_profit 只展示，严禁引用其价格、目标到达或目标附近波动。
7. 从 fresh evidence 的已确认 pivot/range/ATR/完成柱、同指标 `monitoring_observable_baselines` 和点差/流动性推导值。完整 primary 条件在生成时必须尚未满足；不得直接贴着当前完成柱、形成中价格或最近一个普通摆动。按固定优先级复核，以提高首轮可执行率但不允许凑数：
   1. 枚举 1m/5m/15m/30m 全部已确认反向 pivot、已破边界和重新接受位；若当前完成柱与正式失效位之间存在真实锚点，选择距当前最近且已脱离普通噪声的锚点。一个已确认、距当前约两个 1m ATR 或更远的 1m pivot 可以直接作为早期价格结构；更近的 1m pivot 才需要更高周期价格结构或可靠价格组合确认。不得跳过合格近端锚点而默认选择更远的正式失效边界。
   2. 若走廊内没有另一个真实中间锚点，不得编造数值。审查 `invalidation.reference_price` 是否仍能提供有意义的提前复核窗口：若能，可用生成时尚未满足的更快完成柱（通常 `COMPLETED_1M_CLOSE`）监测该 5m/15m 正式结构价；若 crossing 与正式失效几乎同时、无法提前复核，则 REJECTED。
   3. 兜底 threshold 可等于 `invalidation.reference_price`，因为 crossing 仅唤醒复核、不自动宣告失效；但不得越过正式失效价才报警。LONG 必须满足“正式失效位 ≤ 阈值 < 当前同指标完成收盘”，SHORT 必须满足“当前同指标完成收盘 < 阈值 ≤ 正式失效位”。
   4. 没有合格中间锚点且正式结构价也没有有效提前量、正式完成结构已经失效、必要证据/同指标基线不可观测，或所有可用执行 metric 在生成时都已满足时返回 REJECTED。REJECTED 是正常的“本轮不启用实时阈值”，不需要为通过审核降低质量门槛。
8. 原样使用同指标当前基线填写 primary 与 confirmations 的 `current_value`。`hysteresis` 只用于已触发条件回到安全侧后的重新武装，不参与首次触发比较；审计触发距离时不得对 threshold 加减 hysteresis。hysteresis 必须大于0；高频组合按需要设置 confirmation_seconds，完成K线无需人为等待。
9. family_id 小写且唯一，valid_for_seconds 1800–3600，evidence IDs 必须真实存在；reason 说明反向威胁机制，不写交易执行建议。
10. `rationale` 必须逐币审计“当前基线 → 阈值”的距离、对应的已确认结构锚点、相对 ATR/近期 1m 与 5m 波动及点差为何不是普通噪声、又为何能及时唤醒。低于约 2 个已完成 1m ATR 是噪声风险提示而非硬门槛；若阈值就是正式高周期结构价并由更快完成柱观察，距离较近仍有真实结构意义，必须明确写出“价格相同但执行周期更快，crossing 只复核”。必须检查所有 1m/5m/15m/30m 已确认 pivot，而不是只检查与 primary metric 同周期的 pivot；先前越过后又回到安全侧的更高周期锚点，也要评估再次越过是否构成结构重新接受。审计必须覆盖完整可执行条件：当 `required_consecutive_observations > 1` 时，还要量化剩余距离是否足以容纳额外完成柱；若等待第 N 根可能太晚，就改用单根更快完成柱观察同一真实锚点，而不是循环 REJECTED。
11. 若语义要求连续 N 根完成柱，必须把 `required_consecutive_observations` 设为同一个 N（1–3）；不需要连续确认则填 1。不得只在 reason/confirmation 中写“连续两根”却让结构字段仍为 1。

若输入包含 `repair_context`，这是某些币在交付激活时发现旧条件已越线或不可执行后的内部静默纠错：

- 只返回 `repair_context.symbols` 和本轮 `signals` 中列出的失败币，不得重新输出或改写已通过币。
- 逐币读取全部 `previous_rejections`，在 rationale 中明确回应最近一次拒绝的结构、距离或确认时延问题。
- 必须按“最近合格真实锚点 → 有提前量时才评估正式结构价”的顺序重新搜索；不得只把旧 threshold 平移一点、换 family_id 或改写措辞来规避原原因。
- 若没有额外中间锚点，应审查正式结构价是否有实际提前复核价值；没有提前量时保持 `REJECTED`，不要为了通过而把正式失效改写成无意义唤醒。
- 所有 current_value 仍来自本轮同指标 `monitoring_observable_baselines`，不能沿用历史数值。

最终只输出 Schema JSON。不要输出思维过程、Markdown、工具请求或额外字段。

JSON 数值字段必须是合法 JSON number 或只含数字、小数点和可选负号的字符串；严禁在 `threshold/current_value/hysteresis/distance_percent/distance_atr` 中附加逗号、百分号、单位或说明文字。提交前逐字段解析自检一次，避免本可首轮通过的规则因格式错误进入技术重试。
