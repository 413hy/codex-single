# 开发需求与实现说明

## 1. 最终需求镜像

这是一个 24/7 公开行情分析与 Telegram 通知系统，服务人工判断，不自动交易。

1. 服务启动立即运行一轮，以后每个自然整点和半点运行。
2. 从 Bybit USDT 线性永续全市场中过滤 5 个“5m 波动机会大且流动性可用”的币。示例币不是白名单，新鲜度不是硬偏好。
3. 深采 Top5 与市场背景，交给默认模型逐币独立分析；成功定时轮固定选择并通知 2 个相对最值得参考的方向。
4. 每条主信号包含参考价、方向、可信度、市场状态、大致止盈位、形成中 15m/30m/1h、下一根完整 15m、相比上一轮、方向失效结构和摘要。非 Top2 不发送 WATCH 通知。
5. 上轮主信号本轮退出时单独提示并停止监测。
6. 模型为主信号产生反向结构威胁规则。模型基于独立新采证据再审核一次，宿主只做机械可执行性检查，然后交给实时程序观察。
7. 首个完整命中立即使整组旧规则失效，先通知，再重采触发币和市场背景，唤醒模型重新分析、通知，并产生全新规则。crossing 本身不是方向失效。
8. 旧规则持续到 `:00/:30` 定时任务真正开始并在边界原子停止；已接受的紧急流程继续，定时任务各跑各的、不等待。
9. 所有失败通知必须指出工作流、具体步骤、直接原因、因果链、影响和解决方式，并提供“精准重试”和“完整诊断”按钮。

系统不读取或讨论账户、余额、仓位、保证金、杠杆、下单量、订单、盈亏或收益承诺；没有私有交易所接口。

## 2. 数据与过滤

### 2.1 前置 Top5

动态币池来自 Bybit 可交易 USDT 线性永续。默认硬门槛：

- 24h 成交额不少于 1,500,000 USDT；
- 最近 6 根完成 5m（30 分钟）成交额不少于 50,000 USDT；
- ticker spread 不高于 25 bps；
- 至少 48 根有效完成 5m，缺口不超过 2；
- median 5m range 至少 0.15%，最大绝对 5m return 至少 0.35%。

排序综合完成 5m 波动、收益离散、成交额、点差、深度、K 线重叠和冲击延伸。排名只表示“值得让模型分析”，不能投票决定多空。2026-08-19 真实扫描覆盖 717 个合约、193 个 ticker 合格币与 39 个深筛币；Top5 的 24h 成交额约 666 万至 1.23 亿 USDT、近 30m 约 9 万至 770 万 USDT，当前门槛没有继续抬高。

### 2.2 五币深采

每币采集并冻结到统一截止时间：

- last、mark、index、bid/ask、24h 成交、funding；
- 完成 1m/5m/15m/30m/1h/4h K 线，通常各保留 239 根；
- 50 档盘口 REST 快照、最多 1000 条近期成交；
- OI 5m 历史 48 点、多空账户比、公开强平；
- BTC/ETH、市场宽度与全市场背景；
- Binance USD-M、OKX Swap 可用时的来源独立旁证。

缺失、PARTIAL、STALE、WARMING_UP、UNAVAILABLE 和 ERROR 都不能填成零。Bybit 始终是主口径；参考所不支持某币只写入可选来源失败。

证据构建包含多周期 OHLCV、ATR%、EMA9/20/50、RSI14、MACD histogram、rolling high/low、方向效率、bull ratio、turnover ratio、已确认 pivot、spread、深度、recent-trade delta、OI 变化和覆盖质量。所有结论只能引用当前证据包真实存在的 evidence ID。

## 3. 模型中心合同

正式默认配置：

```yaml
analysis:
  model: gpt-5.6-terra
  reasoning_effort: medium
  timeout_seconds: 300
  max_attempts: 2
  monitoring_review_repair_attempts: 3
```

该组合来自 `sol/terra × medium/high` 各 3 轮隔离全流程测试的最佳样本对比，token 不参与选择。模型在临时空工作目录运行，禁用用户规则、原生 Skill、shell、插件、Apps 与任意网页访问；项目内 Skill/操作合同作为版本化提示文本显式注入。需要补数时只能请求宿主白名单只读工具。

模型职责：

- 独立分析全部五币，比较趋势延续与冲击衰竭/反转；1h/4h 只作背景，30m/15m 定义近端结构，5m 确认时机，1m 细化超短节奏。
- RSI、OI、资金费率、成交差、盘口、成交额、强平或跨所数据不能单独定向。
- 定时模式 assessments 恰好覆盖五币，rank 1/2 恰好各一次；即使只有 LOW/MEDIUM，也必须给出相对最优两条，不拿 WATCH 凑通知。
- 四项 K 线展望使用宿主给出的自然窗口，可彼此不同向，不能机械复制综合方向。
- 大致止盈位只展示；不得用于排序、freshness、监测、生命周期或实际失效。
- 正式方向失效必须是可验证的已完成 K 线结构条件，不是用户止损。

宿主不再维护隐藏的方向一致性、ATR 距离上下限、微观噪声阈值或“必须复制正式失效位”等第二套策略。宿主只检查：JSON Schema、币种/窗口/evidence ID、同指标基线、完成柱 primary、规则生成时未满足、目标禁用、组合表达式、连续观察数字一致、有效期和版本权威性。

## 4. 交付新鲜度

方向分析通过后，freshness 只复查模型明确写出的“完成 K 线正式失效条件”在模型运行期间是否已经成立。last/mark 瞬时刺穿、大致止盈到达、价格漂移、spread 或深度变化不能让合格方向自动失败。

正式失效已成立时，重采完整 Top5 并允许模型修复一次；仍不合格才关闭本轮。监测配置完全是后置独立阶段，失败不能清空已经成功的两个主信号。

## 5. 实时阈值合同

每个主信号最终按需保留 0–3 条规则；没有合格规则时该币正常不启用实时阈值，不重试、不通知失败，也不凑近阈值。两个主方向信号始终正常保留。

- primary 必须是反方向的完成 1m/5m/15m/30m/1h 价格结构，优先 5m/15m。结构锚点周期可以与执行周期不同；例如完成 1m 可以监测重新越过已确认 5m/15m pivot 或接受边界，避免等待完成 5m 才与正式失效同柱。
- 先选择距当前最近、已确认且脱离普通噪声的真实反向锚点；已确认且约两个 1m ATR 之外的 1m pivot 可以独立作为早期结构。若没有额外中间锚点，只在正式失效结构价仍能由更快完成 1m 提供实际提前复核窗口时使用；否则不生成阈值。阈值可等于正式失效参考价但不得越过，因为 crossing 只请求复核。
- 完成 1m 必须锚定真实的 1m/5m/15m 结构、明显超过普通 1m 噪声，并在需要时带反向组合确认；不能把任意中间收盘当结构。
- OI、trade delta、orderbook、turnover、spread、funding、liquidation 只能作同一规则 confirmation，不能独立唤醒。
- LONG 的继续上涨/新高/买方增强和 SHORT 的继续下跌/新低/卖方增强禁止唤醒。
- 规则不能引用 take-profit 或目标到达。
- 独立模型复核必须审计当前完成柱、结构锚点、ATR、近期 1m/5m 波动、点差和流动性，说明为何普通下一两根 1m 不会触发、又为何早于完整失效。
- `required_consecutive_observations` 与“连续 N 根完成柱”自然语言严格一致；重复 WebSocket 完成柱不会累计次数。
- 同一 `(analysis_id, symbol)` 只持久化首个完整事件并暂停全部 sibling；服务重启也不会复活。
- 每小时约 10 次仅为软异常告警，不能丢弃已确认 crossing。控制频率依靠阈值语义、完成柱、组合确认与一次性消费。
- `hysteresis` 仅是触发后的安全侧 re-arm buffer，首次命中始终直接比较 observation 与 threshold；不得把迟滞加减到首次触发位。
- 独立模型复核写入的完成柱 `current_value` 由宿主按同批证据机械校正，并在激活时复查。只要固定结构 threshold 仍未满足，新完成柱向任一方向移动都只更新基线；若交付前已经穿越或规则不可执行，才只重采/重审该币最多 3 次。模型明确拒绝代表正常无阈值，立即结束且不进入失败链。主方向信号始终保留。
- 阈值事件必须持久化并展示 primary、连续观察和所有 confirmation 的实际命中值，便于从通知还原执行条件。

触发顺序固定为：`原子落库/整组退役 → 阈值通知 → 单币与市场重采 → 模型紧急复核 → 复核通知 → 独立阈值复核 → 新 analysis_id 覆盖`。任何失败都保持旧规则退役。

并发版本规则：

- `:00/:30` 定时分析开始前原子持久化冻结旧规则；此前规则持续有效，不存在五分钟盲区。
- 紧急与定时模型调用可以并行，定时任务不等待已经接受的紧急复核。
- 最新成功定时周期是 Top2 权威版本。
- 迟到紧急分析可以完成并通知，但 CAS 提交会拒绝它覆盖更新的定时版本。

## 6. Telegram 合同

主导航统一使用：

```json
{"resize_keyboard": true, "is_persistent": false, "one_time_keyboard": false}
```

任何流程都不发送 `ReplyKeyboardRemove` 或 `{"remove_keyboard": true}`。分析详情、返回本轮、异常重试与完整诊断使用 InlineKeyboard；callback data 限制在 64 UTF-8 bytes 内，收到 callback 先立即 ACK，再处理工作。

失败重试任务写入 SQLite，按 failure ID 幂等创建、事务认领、后台执行；重复点击不会重复运行。服务重启把 RUNNING 标为 INTERRUPTED，可再次点击。按钮只重跑失败组件并获取最新数据；更新的权威版本已替代旧任务时返回 SUPERSEDED。

敏感信息只从环境变量读取。异常文本、URL 与安全诊断在落库/通知前脱敏。

## 7. 存储、只读 MCP 与代码导航

SQLite WAL 保存 cycle、五币证据、conclusion、freshness、阈值事件、投递、失败事件、重试任务、Bot offset 和运行状态。`commit_monitoring_version` 使用事务 CAS 防止旧 scheduled/emergency review 覆盖新版本。

stdio MCP 只暴露公开市场、信号、证据、监测、审计与结果读取工具；测试会扫描禁止账户、余额、仓位、杠杆和订单能力。

| 路径 | 职责 |
|---|---|
| `selection/` | 全市场 Top5 过滤 |
| `providers/` | Bybit 主数据、市场背景与参考所旁证 |
| `evidence/` | 冻结证据与指标构建 |
| `analysis/` | Codex 调用、只读工具、freshness、模型阈值复核 |
| `orchestration/cycle.py` | 定时/紧急分析与监测发布编排 |
| `monitoring/` | WebSocket 观察、组合规则、整组一次性消费 |
| `notifications/` | Telegram 格式、键盘、回调与后台重试 |
| `failures.py` | 结构化失败因果与脱敏 |
| `storage/sqlite.py` | 审计、幂等与版本 CAS |
| `.agents/skills/analyze-bybit-ultrashort-signals/` | 项目模型操作合同 |

## 8. 验证命令

```powershell
.\.venv\Scripts\python.exe -m bybit_signal config-check --config config\system.local.yaml
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src tests
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m bybit_signal run-cycle --config config\system.local.yaml --no-notify
```

首次部署或重大重构时先在 shadow 保持 `monitoring.enabled=false`；静态、全量自动化、真实 Top5 深采、定时/紧急模型灰盒和版本替换检查通过后再启用。当前生产已经完成该验收，`monitoring.enabled=true`。
