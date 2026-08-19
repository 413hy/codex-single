# Bybit 超短线信号系统架构重构实施计划

> 历史实施账本：本文件记录 2026-08-18 的任务拆解。当前实施依据是 [`2026-08-19-model-centered-signal-system-reliability-implementation.md`](../plans/2026-08-19-model-centered-signal-system-reliability-implementation.md) 及其对应设计；与新文件冲突的旧任务不再验收。

设计依据：`docs/superpowers/specs/2026-08-18-signal-system-architecture-rebuild-design.md`  
状态：已实施并完成全量、灰盒与生产闭环复核
原则：先数据与 shadow，后程序 Codex；先修流程正确性，后凭多样本证据调整 Prompt；正式服务在切换前保持现状。

## 1. 实施边界

- 保持公开市场、人工判断、无账户、无仓位、无订单、无自动下单边界。
- 成功定时周期仍必须从当前 Top 5 中输出恰好两条主信号。
- 上一轮掉出 Top 5 的主信号额外采集和复核，不占 Top 5 或新信号名额，退出当前主信号集合后停止实时监测。
- 阈值数量按用途决定，通常每币 3–6 条、硬上限 8 条；任一事件确认后立即接受首发并暂停整套版本，不再等待合并窗口。
- 生产 Prompt、策略和阈值规则不根据 ACU/FHE 单样本直接修改。
- 新实现使用独立 shadow 状态、日志和通知开关，完成验收前不覆盖当前正式状态。

## 2. 阶段 0：基线冻结与可重放证据

### 任务 0.1：冻结当前行为基线

目标文件：

- `docs/TEST_REPORT.md`
- `runtime/state/signal.db`（只读提取，不提交）
- 新增 `tests/fixtures/replays/` 下脱敏、最小化的固定证据夹具

工作：

1. 导出 ACU 12:30、FHE 错向、FHE 修复、正确 TUT/PORTAL 和阈值未唤醒周期的最小证据与模型结论。
2. 以证据截止时间截断夹具，未来 K 线仅放在独立 outcome 文件，避免回放输入泄漏。
3. 保存同批 Top 5 的全部方向和事后 15m/30m 结果，用于验证“相对择优”而不只验证单币。
4. 记录当前全量测试、配置、服务和数据库 Schema 基线。

验收：夹具不含 Telegram Token、绝对用户目录或私有信息；输入哈希稳定；结果结算文件不能被分析器输入读取。

### 任务 0.2：建立 shadow 隔离配置

目标文件：

- `src/bybit_signal/config.py`
- `config/system.example.yaml`
- `config/system.local.yaml`（本地、按 Git 状态处理）
- `tests/unit/test_config.py`

工作：

1. 增加 `runtime_mode=production|shadow|no_notify`、独立 shadow database/data/log 路径和 Telegram 投递开关。
2. 增加新架构功能开关，允许组件逐阶段接入而不影响旧正式进程。
3. 配置当前安全底线、阈值硬上限、工具轮数/调用数和新鲜度策略版本；阈值确认后使用首发事件原子化，不设置等待合并窗口。

验收：shadow 绝不调用正式 Telegram 发送；生产与 shadow 锁、数据库和日志互不覆盖。

## 3. 阶段 1：领域合同与存储迁移

### 任务 1.1：扩展领域模型

目标文件：

- `src/bybit_signal/domain/enums.py`
- `src/bybit_signal/domain/models.py`
- `tests/unit/test_contracts.py`

新增或扩展合同：

- 1m K 线和紧凑 OHLCV 序列；
- `MarketContext`、`BreadthSnapshot`、`LiquidityAssessment`、`LiquidationWindow`；
- `OpportunityScore`、`TradabilityScore`、候选风险标签和过滤阶段；
- 工具请求、工具结果、工具审计和分析轮次；
- 阈值提案、宿主审核结果、严重等级、基线状态和事件通知状态；
- 新鲜度判定、修复分析原因和投递价格；
- 预测/目标/失效 outcome 与错误分类。

要求：时间必须带时区；价格和阈值使用 Decimal；缺失与零严格区分；旧 v1–v3 JSON 周期保持可读，新周期升级合同版本。

### 任务 1.2：SQLite 迁移与查询

目标文件：

- `src/bybit_signal/storage/sqlite.py`
- `tests/unit/test_storage_and_cycle.py`

工作：

1. 增加候选过滤、工具调用、阈值事件、立即提醒、正式复核、outcome 和策略版本记录。
2. 增加 schema migration/version 表，迁移必须幂等且保留旧周期。
3. 定义当前有效主信号、追踪币、有效阈值和最新成功定时周期的明确查询。
4. 立即提醒与正式复核使用不同幂等键；阈值替换和主信号切换使用事务。

验收：复制的旧数据库能原地只增量迁移；失败可在 shadow 副本重试；旧正式数据库不在开发阶段写入。

## 4. 阶段 2：公开市场数据平面

### 任务 2.1：扩展 Bybit 公共客户端

目标文件：

- `src/bybit_signal/providers/bybit.py`
- `tests/unit/test_bybit_provider.py`（若现有文件名不同则在对应 provider 测试中扩展）

工作：

1. 支持完成 1m K 线、index/mark/premium 需要的公开字段和多空账户比例。
2. 统一 REST 时间、反向排序、完成柱裁剪、分页、限流、超时和字段质量。
3. 明确 ticker、盘口、成交、OI、K 线和强平的不同时间语义。
4. 不添加私有 API Key 或账户客户端。

### 任务 2.2：市场背景与实时流

建议新增：

- `src/bybit_signal/providers/market_context.py`
- `src/bybit_signal/providers/public_streams.py`
- 对应 `tests/unit/` 测试

工作：

1. 构建 BTC/ETH 1m/5m/15m 背景和 Bybit USDT 永续上涨/下跌宽度。
2. 计算收益分布、成交额宽度、Top 候选同步性和山寨币风险切换。
3. 为 Top 5/当前主信号订阅 ticker、1m/5m K 线、public trades 和 `allLiquidation`；必要时维护有限盘口缓存。
4. 实时缓存按 symbol、metric 和 observed_at 存储，带断线重建和过期状态。
5. 公开强平流没有 REST 历史回填；新进入订阅集合的币在覆盖窗口满足前标记 `WARMING_UP`，未收到事件不得解释为零强平。

### 任务 2.3：重构深度采集器

目标文件：

- `src/bybit_signal/providers/deep_market.py`
- `src/bybit_signal/providers/cross_exchange.py`
- `tests/unit/test_deep_market.py`

工作：

1. 每币采集约 240 根 1m 和约 239 根 5m/15m/30m/1h/4h 完成柱。
2. 增加多空比例、公开强平窗口、有效盘口深度和市场背景引用。
3. 所有数据绑定统一 cutoff；并行请求记录开始、结束、skew 和字段失败。
4. 跨所数据只做同币种一致性旁证，不平均 Bybit 价格。

验收：真实 ACU/CYS/TUT 快照包含 1m 和市场背景；所有完成柱无未来数据；可选来源失败只降级对应字段。

## 5. 阶段 3：Top 5 过滤器

### 任务 3.1：两维评分与风险标签

目标文件：

- `src/bybit_signal/selection/scanner.py`
- `src/bybit_signal/config.py`
- `tests/unit/test_scanner.py`

工作：

1. 保持动态全币池与已确认的初始安全底线量级。
2. 分离 `OpportunityScore` 与 `TradabilityScore`，保存全部原始特征，避免单一归一化总分掩盖问题。
3. 机会维度加入 5m 振幅、实现波动、绝对变化、成交放大、ATR 活跃度、方向效率、重叠和近端结构空间。
4. 可交易性维度加入绝对成交额、spread、有效深度、连续性、异常成交集中度和数据质量。
5. 输出 `THIN_LIQUIDITY`、`CHOPPY`、`IMPACT_EXTENDED`、`DATA_DEGRADED` 等风险标签；标签不机械生成方向。
6. Top 5 只来自当前全市场排名；上一轮主信号在编排层额外追加，不污染排名。

### 任务 3.2：过滤器真实审查

1. 使用多个自然半小时的真实市场快照运行 scan-only。
2. 对比旧/新 Top 5 的成交额、spread、深度、波动和风险标签。
3. 检查候选不会全部变成无波动大币，也不会全部变成盘口空洞的暴涨暴跌币。
4. 当前 Codex 人工审查每轮入选/落选原因；这一阶段不调用程序 Codex、不发 Telegram。

验收：Top 5 可解释、每项特征可重算；门槛未整体抬高一倍；ACU 类型币可以入选但薄流动性与低效率不能被隐藏。

## 6. 阶段 4：证据包与当前 Codex 参考分析

### 任务 4.1：重构证据构建器

目标文件：

- `src/bybit_signal/evidence/builder.py`
- `tests/unit/test_evidence_builder.py`

工作：

1. 增加 1m 统计、最近紧凑 1m/5m/15m OHLCV、市场背景、深度、强平和多空比例证据。
2. 继续保留 5m/15m/30m/1h/4h 结构、ATR、pivot、效率、重叠和 turnover。
3. 每项证据包含 observed_at、coverage、quality 和 source；形成中与完成柱明确区分。
4. 控制基础上下文大小，完整历史通过工具按需查询。

### 任务 4.2：当前 Codex 代替程序 Codex

1. 对若干真实 shadow 周期生成可移交证据包和工具输出。
2. 当前 Codex 在证据截止时点完成独立 Top 5 分析和两条参考信号，不读取 outcome。
3. 保存参考方向、目标、失效、置信度、阈值和调用过的工具。
4. 窗口完成后运行 outcome 结算，区分数据、方向、择优、置信度、目标、阈值和时效问题。
5. 数据合同仍有缺口时回到阶段 2/4.1，不通过增加 Prompt 规则掩盖。

验收：至少覆盖正常延续、假突破、衰竭、薄流动性、山寨币普跌和字段降级；参考分析可仅凭保存证据复现。

## 7. 阶段 5：白名单工具与程序 Codex

### 任务 5.1：宿主管理的只读工具注册表

建议新增：

- `src/bybit_signal/analysis/tool_registry.py`
- `src/bybit_signal/analysis/tool_contracts.py`
- `tests/unit/test_analysis_tools.py`

白名单工具：最新行情/1m/5m、市场宽度、盘口与成交、OI/funding/多空比、公开强平、跨所验证、历史信号/outcome 和扩展 K 线窗口。

工作：

1. 每个工具使用严格参数 Schema、symbol 白名单、只读实现、超时和输出大小上限。
2. 工具结果带 data cutoff、status、latency 和 evidence ID。
3. 禁止 shell、任意文件、任意 URL、账户、订单和私有 API 工具。
4. 同一分析内调用去重并缓存，所有调用写入审计。

### 任务 5.2：Codex 多轮工具协议

目标文件：

- `src/bybit_signal/analysis/codex.py`
- `src/bybit_signal/domain/models.py`
- `prompts/signal_analysis_zh.md`
- `tests/unit/test_codex_analyzer.py`

工作：

1. 将单次最终 JSON 扩展为宿主管理的 `TOOL_REQUESTS | FINAL` 严格轮次协议。
2. Codex 可一次请求多个白名单工具；宿主校验后并行执行，再把结构化结果加入下一轮。
3. 设置最大工具轮数和总调用数；基础数据足够时直接 FINAL。
4. 工具失败按字段进入模型上下文，不允许模型假装调用成功。
5. FINAL 继续校验恰好两条主信号、追踪币、四项预测、目标、失效、置信度和阈值。
6. 保持 ephemeral、read-only 和严格输出 Schema；模型矩阵评测完成后正式值确定为 `gpt-5.6-terra / medium`。

### 任务 5.3：策略与宿主校验

1. 将市场环境、单币双假设、Top 5 相对择优、追踪生命周期和阈值生成分成清晰 Prompt 章节/版本。
2. 宿主增加最近有效目标校验、HIGH 置信度冲突校验和候选风险标签约束。
3. 方向冲突校验仍只拒绝已有证据明确冲突的结论，不由程序自动翻转方向。
4. 用 ACU/FHE/TUT/PORTAL 和其他正确样本对照回放；Prompt 改动必须改善多样本而非单样本。

验收：程序 Codex 与阶段 4.2 参考分析的事实和时间边界一致；工具调用可审计；无工具时可降级完成；输出不泄露账户或执行建议。

## 8. 阶段 6：投递前新鲜度与快速修复

建议新增：

- `src/bybit_signal/analysis/freshness.py`
- `tests/unit/test_freshness_gate.py`

工作：

1. 模型完成后刷新两条主信号的 last/mark、完成/形成中 1m/5m、spread、有效深度和市场背景。
2. 根据 snapshot age、相对 5m ATR 的反向漂移、快照后极值回撤、失效位、spread/depth 变化和背景切换输出 `PASS | REPAIR | REJECT`。
3. 数值参数使用 shadow 样本分布校准并记录版本，不用 ACU 单样本拍脑袋定值。
4. PASS 更新投递展示价并重新验证目标/阈值；REPAIR 使用当前 Top 5 的更新证据做小范围程序 Codex 修复；REJECT/修复失败不发旧信号。
5. 保存分析快照价格、投递价格、漂移原因和最终决定。

验收：ACU 过期结论回放被拦截；稳定样本不因正常噪声大量重跑；成功周期最终仍为两条主信号。

## 9. 阶段 7：实时阈值和独立紧急通道

### 任务 7.1：阈值宿主审核

目标文件：

- `src/bybit_signal/monitoring/engine.py`
- 新增或扩展 `src/bybit_signal/monitoring/validation.py`
- `tests/unit/test_monitoring_engine.py`

工作：

1. 校验当前观测、阈值、距离百分比/ATR、迟滞、确认周期、严重等级、有效期、实现支持和证据来源；完成 K 线、last、mark 和非价格窗口各自使用所属指标基线。
2. 拒绝已穿越、过远、过近、重复和无法追溯的阈值；成交 delta、成交额、OI、spread、盘口和强平还要通过按本轮规模计算的动态噪声底线。
3. 每币允许多种语义阈值，去重后硬上限 8；不要求凑数量。
4. 新指令用最新实时值初始化基线；形成中 K 线只基线，完成柱才确认完成柱规则。
5. 新订阅后的第一根完成柱真实穿越必须触发；分析完成后须再次深采两个主信号并执行阈值交付激活，增加“完成柱距自身基线过近”和 ACU 端到端回归测试。

### 任务 7.2：实时订阅与事件优先级

目标文件：

- `src/bybit_signal/monitoring/realtime.py`
- `src/bybit_signal/providers/public_streams.py`
- `tests/unit/test_realtime_monitor.py`

工作：

1. 支持 ticker/mark、1m/5m/15m K 线、public trades、spread/depth、OI 和 allLiquidation 的受控订阅。
2. 指令切换、断线和重启后重新基线，取消旧币 pending 事件；同一 `(analysis_id, symbol)` 任一 family 穿越后原子记录首个事件并暂停整套版本，SQLite 恢复后不得重新武装任何 sibling。
3. 任一确认 crossing 都立即进入事件队列；同一分析/币种版本只原子接受首个事件，不再等待普通事件合并窗口。
4. 自然半小时任务真正开始时持久化冻结全部旧阈值；边界前已开始的紧急复核独立收尾，定时任务按时执行。失败保持冻结，成功新周期才恢复。
5. 普通唤醒保持约 10 次/小时软预算，超出时记录异常但不拒绝预冻结前已经确认的真实突破。盘口、成交 delta、spread 需持续确认，30 秒成交和 1 分钟强平窗口完整预热后才可观测；所有原因入审计。

### 任务 7.3：两阶段通知和紧急编排

建议新增：

- `src/bybit_signal/orchestration/emergency.py`
- 调整 `src/bybit_signal/service.py`
- 调整 `src/bybit_signal/notifications/telegram.py`

工作：

1. 阈值事件持久化后立即发送简短 Telegram 提醒，不等待采集或模型。
2. 定时边界前已接受的紧急事件继续完成通知和复核；自然半小时定时任务不等待。旧集合到边界才停止接受新事件；同一旧版本整套只允许首个事件。
3. 紧急采集重新获取该币和最新市场背景，调用程序 Codex 完整复核。
4. 正式结果补发维持、转弱、反向、失效或退出，并以新 analysis ID 原子替换阈值后恢复监测。
5. 模型失败时发送复核失败状态，保留原始事件提醒并继续暂停整套旧阈值，等待后续成功分析覆盖。

验收：预冻结前的关键事件立即提醒并完成复核；定时模型等待其收尾，冻结期间不再产生旧阈值事件；重复事件和重启不会重复投递。

## 10. 阶段 8：Telegram、MCP 与结果结算

### 任务 8.1：Telegram

目标文件：

- `src/bybit_signal/notifications/formatter.py`
- `src/bybit_signal/notifications/telegram.py`
- `src/bybit_signal/notifications/keyboards.py`
- `tests/unit/test_telegram.py`
- `tests/scenario/test_contract_and_keyboard_flow.py`

工作：

1. 保持每个成功定时周期两张主卡、追踪状态更新、四项预测、详情与返回。
2. 增加立即阈值提醒、正式紧急复核、新鲜度修复和复核失败格式。
3. 所有通知保持 HTML 长度安全和幂等。
4. ReplyKeyboard 精确保持 `resize=true`、`is_persistent=false`、`one_time=false`，禁止 remove。

### 任务 8.2：MCP

目标文件：

- `src/bybit_signal/mcp_server.py`
- `tests/scenario/test_mcp_stdio.py`

增加当前 Top 5、市场背景、工具审计、阈值事件、投递新鲜度和 outcome 只读查询；旧工具保持兼容或给出明确版本迁移。

### 任务 8.3：自动结果结算

建议新增：

- `src/bybit_signal/evaluation/outcomes.py`
- `tests/unit/test_outcome_evaluation.py`

工作：

1. 结算形成中 15m/30m/1h、下一根 15m、目标触达和失效。
2. 保存方向准确性、最大有利/不利变化、延迟、错向类别和数据质量。
3. 生成只读复盘报告；禁止自动写 Prompt、配置或策略。

## 11. 阶段 9：编排重构、文档与完整验证

### 任务 9.1：定时编排

目标文件：

- `src/bybit_signal/orchestration/cycle.py`
- `src/bybit_signal/service.py`
- `tests/unit/test_storage_and_cycle.py`

工作：

1. 将过滤、目标集合、采集、参考/程序分析、新鲜度、持久化、通知和阈值切换拆成有明确输入/输出的节点。
2. 定时与紧急使用不同协调器，不共享单一周期锁。
3. 保持自然整点/半点、单实例、错误通知、恢复和资源关闭。

### 任务 9.2：文档对齐

更新：

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/DEVELOPMENT.md`
- `docs/DEPLOYMENT_VPS.md`
- `docs/REFERENCE_REVIEW.md`
- `docs/TEST_REPORT.md`
- 配置示例和运维脚本说明

文档中的流程图、数据来源、工具权限、shadow/正式切换和故障行为必须与代码一致。

### 任务 9.3：测试与灰盒矩阵

按顺序执行：

1. 全量 pytest、Ruff、mypy strict、pip check、配置检查和 PowerShell 语法。
2. 私有交易能力、真实 Token、Telegram 键盘和日志脱敏扫描。
3. 全部历史回放与旧版对照，重点 ACU、FHE、正确 TUT/PORTAL 和阈值首穿。
4. 多轮真实 scan-only，审核 Top 5 流动性与机会质量。
5. 当前 Codex 参考 no-notify 分析。
6. 程序 Codex no-notify 与工具调用审计。
7. 连续多个自然半小时 shadow 周期及 outcome 结算。
8. Telegram 测试 chat 的立即提醒、正式复核、详情、返回和幂等。
9. MCP 全工具实机调用。
10. 正式切换前的 Windows/VPS 24/7、断线、重启、并发和墙钟周期。

测试发现不合理时回到对应阶段修复并重跑受影响回放；策略/Prompt 改动额外要求全套方向样本对照。

## 12. 正式切换与回滚

1. 保留旧提交、旧配置和旧数据库备份位置，但不在 Git 中保存凭据或 runtime 数据。
2. shadow 全部通过并由用户确认后，受控停止旧单实例服务。
3. 在复制数据库上先执行迁移并验证，再对正式数据库执行同一幂等迁移。
4. 启动新服务，验证 Telegram preflight、ReplyKeyboard、实时订阅基线和一次 no-notify 预检。
5. 完成一个正式启动周期和下一自然墙钟周期；每轮恰好两条、delivery 一次、当前阈值只属于当前主信号。
6. 出现迁移、模型、通知、监测或安全失败时停止新服务，使用旧提交和旧配置恢复；不使用破坏性 Git 或数据库命令。

## 13. 完成定义

- 设计文档中的所有数据流和边界均已实现；
- 当前 Codex 与程序 Codex 两阶段验证完成；
- Top 5 过滤符合高 5m 波动、可用成交额和不过度抬高门槛的需求；
- 1m、多周期、市场宽度、流动性、强平和工具调用可审计；
- ACU 过期信号被新鲜度层拦截，FHE 已有反转证据不再被高周期覆盖；
- 成功周期固定两条主信号，追踪币额外复核且退出后停止监测；
- 阈值首个真实穿越不丢失、数值合理、关键事件立即通知、紧急复核不排队；
- outcome 自动结算但不自动修改策略；
- 全量自动化、历史回放、真实 shadow、Telegram、MCP、24/7 和安全验证通过；
- 正式服务部署成功、工作区提交清晰、文档与代码一致。
