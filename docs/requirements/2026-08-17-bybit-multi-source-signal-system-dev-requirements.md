# Bybit 多源合约交易信号系统开发需求文档

- 文档日期：2026-08-17
- 目标读者：本地 Codex、VPS Codex、开发与测试人员
- 对应设计：`docs/superpowers/specs/2026-08-17-bybit-multi-source-signal-system-design.md`
- 当前阶段：V1，仅市场分析和 Telegram 通知

## 1. 一句话需求

构建一个全天候 Python 服务：每 30 分钟扫描 Bybit 全量 USDT 线性永续，使用 CMI 汇总 Bybit/Binance/OKX 等公开市场证据，由 `gpt-5.6-sol` 高推理强度综合判断最多 2 个强交易机会，并通过 Telegram 通知用户手动决策；两轮之间由轻量监测器在关键阈值突破时触发紧急复查。

## 2. 已知事实

- 当前工作目录最初为空，需要独立建设，不以 Binance MCP 项目为代码基座。
- 本地 CMI 项目位于 `E:\quantify\sol_realtime_snapshot`。
- 本地已安装 Crypto Market Intelligence，当前已知安装目录为 `C:\Users\yuuhe\AppData\Local\Programs\Crypto Market Intelligence`。
- CMI 能采集 Bybit、Binance、OKX 的公开永续与可用现货数据，并输出原子 JSON/TXT/健康文件。
- CMI 一次主要处理一个指定币种；同一币种有单实例约束，不同币种可以按资源并发。
- 用户最终在 Bybit 观察并手动交易，因此 Bybit 是合约范围和价格基准。
- 用户要求每 30 分钟都收到通知，而不是只有出现信号时才通知。
- 用户要求上一轮有信号、本轮不再强的币也必须通知分析结果。
- 用户要求 Telegram 主要用于通知，不需要账户/交易控制面板。

## 3. 合理假设

- V1 使用 Bybit 主网公开市场数据；公开数据不需要 Demo API Key。
- 本地 Python 版本和依赖由项目锁定文件确定，优先支持当前 Windows 与 Linux VPS。
- 一轮采用一次批量 Codex 综合分析，而不是对每个币无上限单独调用，紧急事件按合并后的币种集合调用。
- Kronos、公告/新闻和离线研究均为可插拔能力，缺失时系统继续运行并披露状态。
- Telegram 使用私聊和允许列表，Bot Token 在部署时提供，不写入仓库。
- VPS 具体规格尚未给出，因此重计算组件默认采用自动探测和资源上限。

## 4. 明确非目标

- Bybit Demo 或正式盘自动交易；
- API Key 管理、账户余额、仓位、杠杆、订单、盈亏；
- 给出保证金或建议下单数量；
- Telegram 内修改交易策略或进行交易控制；
- 保证每条信号盈利；
- 以其他交易所均价代替 Bybit 实际价格；
- 把参考项目整体复制后改名。

## 5. 用户业务流程

### 5.1 固定周期

1. 系统在上海时间每个整点和半点启动分析。
2. 扫描 Bybit 全量可用 USDT 线性永续。
3. 根据已完成 5m K 线、成交活跃度、流动性、数据连续性和结构机会生成 Top 5。
4. 把上一轮强信号币加入强制复查集合。
5. 调用 CMI 为这些币生成新的多交易所深度快照。
6. 构建带时间、来源、质量、证据 ID 和工具状态的紧凑上下文。
7. Codex 在看不到上一轮方向和目标的情况下独立形成本轮结构化结论，协调器随后再与上一轮结构化结果比较。
8. Telegram 推送本轮强信号或无强信号概况，并附详情按钮。
9. 保存结果和新动态阈值。

### 5.2 紧急复查

1. 轻量监测器持续消费 Bybit 公共行情。
2. 通用异常或 Codex 动态阈值发生真实跨越。
3. 60 秒内同类事件合并，并应用单币冷却和全局软限频。
4. 对触发币重新运行 CMI，禁止直接复用旧快照。
5. Codex 输出紧急复查结论。
6. Telegram 发送紧急变化提醒并保存审计记录。

### 5.3 用户查看

1. 用户收到简洁的“核心结论”。
2. 点击 InlineKeyboard 的“查看分析详情”。
3. Bot 应答回调并编辑原消息或降级发送详情。
4. ReplyKeyboard 仍可通过客户端图标或 `/menu` 重新展开。

## 6. 功能需求

### FR-01 全币池发现

- 自动发现 Bybit 当前可交易的 USDT 线性永续。
- 新增和下线币种无需改代码。
- 过滤不可交易、数据不完整或流动性明显不足的合约。

### FR-02 候选筛选

- 使用已完成 5m K 线，形成中 K 线不得参与完成 K 线统计。
- 评估波动、成交活跃度、价差、连续性和结构空间。
- 输出候选排名和可解释原因。
- 不包含仓位、保证金、杠杆或利润金额公式。

### FR-03 多源深度采集

- CMI 输入为指定币种，原始数据来自 Bybit、Binance、OKX。
- 采集永续、可用现货、K 线、订单流、盘口、OI、资金费率、基差、多空比、爆仓、相对强弱和健康状态。
- 每轮校验快照时间、币种、schema 和哈希。
- 单所失败时保留明确数据限制，不制造数据。

### FR-04 市场背景

- 至少包含 BTC/ETH 走势和候选币相对强弱。
- 重大公告/新闻采用低频缓存和来源核对。
- 可选背景不可用时不阻断核心分析。

### FR-05 证据构建

- 所有关键数值带来源、市场类型、时间和证据 ID。
- Bybit `last` 与 `mark` 不混淆。
- 多交易所价格不平均。
- `null`、零、缺失和 `WARMING_UP` 具有不同语义。
- 上下文不包含密钥或账户信息。

### FR-06 Codex 分析

- 默认模型 `gpt-5.6-sol`、`high` 推理强度。
- 使用严格 JSON Schema。
- 校验 `analysis_id`、工具状态、证据引用和输出完整性。
- 筛选器和确定性工具只提供证据，Codex 负责综合方向结论。
- 缺少可选工具时继续分析，禁止固定票权和机械扣分。

### FR-07 信号选择

- 深度分析 Top 5，最多发布 2 个强信号。
- 强信号必须有唯一方向、单一大致止盈、当前市场状态、形成中 1h 展望、上一轮比较和失效条件。
- 没有强信号时仍输出候选概况。
- 所有输出明确为分析信息，由用户自行判断。

### FR-08 上轮信号追踪

- 上一轮强信号币不得因本轮未进入 Top 5 而消失。
- 本轮模型证据包不包含上一轮方向和目标；当前结构化判断通过校验后，协调器再比较上一轮。
- 输出 `MAINTAINED`、`WEAKENED`、`INVALIDATED` 或 `INDETERMINATE`。
- `WEAKENED`、`INVALIDATED` 和 `INDETERMINATE` 都必须通知。

### FR-09 动态监测

- Codex 可以输出可实时监控的动态阈值。
- 阈值包含比较符、迟滞、有效期、原因、证据 ID 和阈值族 ID。
- 只在真实跨越时触发，避免边界抖动。
- 默认 60 秒合并、单币 10 分钟冷却、每小时 10 次紧急模型调用软上限。
- 方向失效和极端波动允许越过软上限，但必须审计。

### FR-10 Telegram

- 每 30 分钟至少发送一次周期通知。
- 紧急事件可额外发送通知。
- 主导航使用 `ReplyKeyboardMarkup`：`resize_keyboard=true`、`is_persistent=false`、`one_time_keyboard=false`。
- 生产运行代码禁止构造 `ReplyKeyboardRemove` 或包含 `remove_keyboard` 的 payload。
- InlineKeyboard 提供“查看分析详情”。
- 只保留最新信号、系统状态、帮助等最小入口。
- 使用 Chat ID/User ID 允许列表。

### FR-11 历史与审计

- SQLite 保存分析运行、候选、信号、监控状态、快照引用、哈希、工具状态、Token 用量和通知状态。
- 原始大快照使用文件保存并轮转。
- 支持查询最新信号、详情、历史和系统健康。

### FR-12 只读 MCP/CLI

- 向 Codex 暴露只读健康、候选、快照、信号和历史工具。
- 可提供“立即分析”运维动作，但不得存在交易工具。
- 默认 stdio 或 localhost，不允许裸露无认证公网 HTTP。

### FR-13 24 小时运行

- 服务异常退出后可由系统服务或容器自动重启。
- 重启恢复上轮信号、有效监控策略、冷却状态和 Telegram offset。
- 提供健康检查、结构化日志和日志轮转。

## 7. 非功能需求

### NFR-01 安全

- 仓库不包含真实 Token 或私钥。
- Codex 子进程只读运行，使用环境变量白名单。
- 外部命令禁止 shell 字符串拼接。
- 输入输出执行 Schema、长度、来源和身份校验。

### NFR-02 正确性

- 所有时间在内部使用 UTC，通知显示 Asia/Shanghai。
- 完成 K 线与形成中 K 线严格分离。
- 原子写入，服务不得发布半成品。
- 构建和测试失败必须返回非零退出码。

### NFR-03 可解释性

- 每个结论可追踪到本轮证据。
- 每个数据源都报告 `AVAILABLE`、`PARTIAL`、`STALE`、`WARMING_UP` 或 `UNAVAILABLE` 等状态。
- 模型输出说明不确定性与缺失数据，但不使用虚构字段。

### NFR-04 资源控制

- 全币池扫描轻量化。
- CMI 仅针对候选和追踪币运行。
- 并发数、超时、快照长度和模型调用数可配置。
- Kronos 在资源不足时自动关闭。

### NFR-05 可移植性

- Windows 本地和 Linux VPS 共用核心 Python 代码。
- 本机绝对路径只出现在示例或部署覆盖中，不进入核心逻辑。
- 配置、数据、日志和运行时目录可重定位。

## 8. 信号输出最小字段

```text
analysis_id
symbol
generated_at
canonical_price.exchange = BYBIT
canonical_price.market = LINEAR_PERPETUAL
canonical_price.type = LAST | MARK
canonical_price.value
canonical_price.timestamp
direction = LONG_BIAS | SHORT_BIAS
market_state
take_profit.value
take_profit.rationale
forming_1h.direction
forming_1h.strength
forming_1h.window_start
forming_1h.window_end
comparison_with_previous
invalidation.condition
invalidation.reference_price
summary
details
uncertainties[]
tool_assessments[]
evidence_ids[]
monitoring_directives[]
```

候选但非强信号允许使用 `NO_STRONG_SIGNAL` 状态；强信号方向不允许中性或同时多空。

## 9. 验收标准

### AC-01 固定通知

给定服务正常运行，当连续跨过两个半小时边界时，Telegram 应收到两次不同 `analysis_id` 的周期通知，即使都没有强信号。

### AC-02 上轮减弱

给定上一轮 CYS 为强偏空，本轮 CYS 未进入 Top 5 且结构变弱，系统仍采集 CYS 并发送 `WEAKENED` 分析，不能静默消失。

### AC-03 多源但 Bybit 定价

给定 Bybit、Binance、OKX 价格存在差异，分析可引用差异作为证据，但通知最近价格和止盈/失效基准必须明确使用 Bybit，不得输出三所平均价。

### AC-04 部分数据失败

给定 OKX 不可用且 Bybit/Binance 正常，系统将 OKX 标记为不可用并继续；不得将 OKX 字段填零。

### AC-05 陈旧快照

给定 CMI 超时且目录中只有上一轮 JSON，系统不得把旧 JSON 当作新快照发布新信号。

### AC-06 紧急触发

给定动态价格失效阈值，价格从阈值下方向上穿越，系统只触发一次；在迟滞区间内抖动不得反复触发。紧急分析必须使用新 CMI 快照。

### AC-07 限频

给定一小时内多个普通异常，合并和冷却后模型紧急调用不超过软上限；方向失效越界时记录原因。

### AC-08 Telegram 键盘

所有主导航 payload 满足三个固定布尔值；覆盖全部生产键盘工厂的测试证明任何输出 payload 都不包含键盘移除字段。发送 InlineKeyboard、编辑详情或重启后，`/menu` 可恢复 ReplyKeyboard。

### AC-09 无交易能力

静态扫描、依赖审计和网络模拟均不得发现账户、仓位、杠杆、余额或订单端点调用。

### AC-10 模型输出校验

无效 JSON、错误 `analysis_id`、缺失必需字段或未知证据 ID 不得进入 Telegram 信号，系统执行有限修复重试并记录失败。

## 10. 代表性验证场景

### 场景：CYS 偏空信号减弱并紧急失效

1. 14:00 扫描器把 CYS 放入 Top 5。
2. CMI 成功取得三所证据；Bybit last 为通知价格。
3. Codex 输出 CYS `SHORT_BIAS`、一个止盈位和一个上方失效结构。
4. Telegram 收到核心结论和详情按钮。
5. 14:12 价格真实上穿失效阈值，订单流反转；60 秒内重复事件被合并。
6. 系统重新采集 CMI，Codex 判定原方向失效并发送紧急提醒。
7. 14:30 固定周期照常运行；CYS 即使不在 Top 5 也被强制复查并报告 `INVALIDATED`。
8. 整个过程中不存在任何下单、仓位或账户调用。

通过条件：步骤 1–8 都可由日志、SQLite、快照哈希、模型输出和 Telegram 模拟记录证明。

## 11. 风险与处理

| 风险 | 级别 | 处理方式 |
|---|---:|---|
| 小币种流动性和盘口瞬变导致信号快速过期 | 高 | 显式行情时间、失效条件、实时监测和紧急复查 |
| CMI 多币种深采超过半小时窗口 | 高 | Top 5 限制、资源并发上限、超时和部分结果 |
| Codex 输出漂移或 Schema 不合格 | 高 | 严格 Schema、analysis_id、证据校验和有限修复重试 |
| 把旧快照当新数据 | 高 | 运行 ID、生成时间、文件哈希和原子完成标记 |
| 过度唤醒消耗 Token | 中 | 合并、冷却、软限频、紧凑上下文和 Token 审计 |
| Kronos 在 VPS 上过慢 | 中 | auto 模式、只跑最终候选、超时降级 |
| Telegram 客户端隐藏键盘图标 | 中 | 不移除键盘，固定 flags，/menu 和命令菜单恢复 |
| 符号与项目新闻错配 | 中 | symbol/token/project 多别名和官方来源核验 |
| MCP 被公网暴露 | 高 | 默认 stdio/localhost，远程必须认证代理 |

## 12. 开放配置项

不需要改变产品设计，但部署前必须落实：

1. Telegram Bot Token 和允许的 Chat/User ID；
2. VPS 的操作系统、CPU、内存和磁盘；
3. VPS 可用的 CMI 运行方式；
4. Codex CLI 的登录与模型可用性；
5. 是否启用额外公告/新闻数据源。

## 13. 决策日志

| 日期 | 决策 |
|---|---|
| 2026-08-17 | 初始自动下单方案暂缓，V1 改为纯信号通知 |
| 2026-08-17 | 删除所有仓位、保证金、杠杆和盈亏相关需求 |
| 2026-08-17 | 固定每30分钟通知，未出现强信号也要通知 |
| 2026-08-17 | 上轮信号币本轮不强时必须继续分析并提醒 |
| 2026-08-17 | 恢复轻量实时监测与紧急 Codex 唤醒 |
| 2026-08-17 | 默认使用 gpt-5.6-sol/high |
| 2026-08-17 | Bybit 为目标市场，多交易所数据作为深度证据 |
| 2026-08-17 | CMI 方向定义为交易所源数据进入 CMI，再输出标准化快照 |
| 2026-08-17 | Telegram 以通知为主，不建设交易或复杂配置界面 |

## 14. 交给 VPS `session_0` 的启动提示

```text
你是本项目 VPS 部署与实现控制会话 session_0。

先完整阅读：
1. docs/requirements/2026-08-17-bybit-multi-source-signal-system-dev-requirements.md
2. docs/superpowers/specs/2026-08-17-bybit-multi-source-signal-system-design.md
3. 仓库根目录 AGENTS.md（若存在）

你的职责：
- 先核对 VPS 操作系统、Python、Codex CLI、CMI 入口、Telegram 密钥注入方式；
- 检查当前实现与两份文档是否一致，发现差异先报告；
- 保持 V1 纯市场分析和 Telegram 通知，禁止加入账户、仓位、杠杆、盈亏或订单功能；
- Bybit 只作为目标市场和权威参考价，深度证据必须支持 CMI 的多交易所输出；
- 严格运行单元、集成、端到端与灰盒场景测试；
- 不要吞掉构建或测试失败；
- 完成部署后输出服务状态、健康检查、日志位置、配置缺口和回滚方法。

任何真实 Token 只通过安全环境变量或受限密钥文件注入，不得写入仓库、Prompt、日志或测试夹具。
```
