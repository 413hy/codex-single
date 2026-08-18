# 全量与灰盒测试报告

测试日期：2026-08-17～2026-08-18（Asia/Shanghai）
环境：Windows 10、Python 3.13、真实公网公开市场、真实 Telegram Bot、真实 Codex CLI

说明：第 2～6 节保留重构前的灰盒基线和问题修复轨迹；第 7～8 节记录当前 `contract_version=3` 的最终行为，最终验收以第 7～9 节为准。

## 1. 自动化与静态检查

| 检查 | 结果 |
|---|---|
| Ruff 全仓 | 通过 |
| mypy strict（31 个源码文件） | 通过，0 issue |
| pytest 全量 | 通过，62 项 |
| pip check | 通过，无破损依赖 |
| MCP stdio initialize/list/call | 通过 |
| PowerShell 三个服务脚本语法解析 | 通过 |
| git diff whitespace | 通过 |
| Bot Token 提交扫描 | 通过，`.env` 之外无真实 Token |
| 私有交易能力扫描 | 通过，生产源码无账户/仓位/订单/杠杆端点 |
| 键盘安全扫描 | 通过，无 ReplyKeyboardRemove 或 `remove_keyboard` |

自动化覆盖包括：配置、领域 Schema、完成柱、Bybit provider、扫描器、深采、证据构建、Codex 重试/价位几何/监测约束、SQLite、上一轮信号比较、Reply/Inline 键盘、Telegram 白名单/幂等/回调、阈值 crossing/hysteresis/过期/限频、只读 MCP、单实例锁和日志脱敏。

## 2. 真实公开数据采集

命令：`market-capture --symbol CYSUSDT`

结果：

- Bybit last `0.4753`、mark `0.4762`，口径分离；
- 完成 5m/15m/1h/4h 各 239 根；
- order book 可用；
- recent trades 1000 条；
- OI history 48 点；
- 快照 SHA-256 生成成功；
- CYS 在 Binance/OKX 无对应合格 ticker，分别写入可选来源 failure；Bybit 主证据不受影响。

这验证了部分跨所缺失按字段降级、不会填零或把其他交易所价格平均为 Bybit 参考价。

## 3. 完整 no-notify 灰盒

分析 ID：`cycle_20260817T160848Z_fe7ffd08`

| 指标 | 结果 |
|---|---|
| Bybit 动态 universe | 713 |
| ticker 初筛合格 | 455 |
| 完成 5m 深筛 | 60 |
| Top 5 | AIOUSDT、FHEUSDT、GUNUSDT、ACEUSDT、HFTUSDT |
| 上轮强信号追踪 | GPSUSDT |
| 模型 | gpt-5.6-sol / high |
| 模型调用 | 单批 1 次通过 |
| 延迟 | 148300 ms |
| 强信号 | HFTUSDT LONG_BIAS |
| 上轮信号变化 | GPSUSDT 降为 WATCH，TrackingStatus=INVALIDATED |

GPS 不在 Top 5 仍被重新深采和完整分析，满足“上轮出现信号、本轮不再强也必须提醒”的核心场景。所有监测指令均为当前实现支持的指标，有效期 1800 秒；forming 1h 精确对应请求小时。

## 4. 正式 24/7 服务与真实通知

启动方式：`scripts/start-service.ps1`，隐藏 Windows 进程，单实例文件锁。

正式分析 ID：`cycle_20260817T162038Z_1a58d9e6`

| 项目 | 结果 |
|---|---|
| 服务启动与立即分析 | 通过 |
| Telegram getMe/preflight | 通过，Bot `@yhetest_bot` |
| ReplyKeyboard 启动消息 | 已真实发送 |
| Top 5 | FHEUSDT、AIOUSDT、TUTUSDT、GUNUSDT、ACEUSDT |
| 追踪币 | HFTUSDT |
| 模型调用 | 单批 1 次通过，167218 ms |
| 强信号 | FHEUSDT LONG_BIAS、TUTUSDT LONG_BIAS |
| 追踪变化 | HFTUSDT 降为 WATCH，TrackingStatus=INVALIDATED |
| Telegram 核心结论 + Inline 详情按钮 | 已真实发送 |
| SQLite delivery 幂等记录 | 1 条，cycle/chat/kind 唯一 |
| 每个币动态阈值 | 2 条，均通过宿主校验 |
| 服务进程 | 启动后仍存活 |
| 应用日志 | 无真实 Bot Token |

Inline callback 的授权、64-byte 边界、详情查询和答复由自动化场景测试覆盖；实际按钮已经随正式消息发送，最终视觉点击由 Telegram 客户端呈现。

### 真实半点续跑

服务随后在上海时间 00:30:00 自动启动下一轮，而不是按首次启动时间简单加 30 分钟。分析 `cycle_20260817T163000Z_f0ab7d6e` 一次通过并再次投递：

- 上轮强信号 FHEUSDT、TUTUSDT 均被强制追踪；
- FHEUSDT 降为 WATCH / WEAKENED，仍出现在通知；
- TUTUSDT 保持 STRONG / MAINTAINED；
- HFTUSDT 成为新的 STRONG；
- SQLite 对该 cycle 仍只有 1 条 Telegram delivery；
- 轮次完成后服务继续存活，日志再次确认无 Token。

该续跑同时验证了墙钟整点/半点调度、跨轮追踪、转弱提醒、信号更新和长期服务不中断。

## 5. 灰盒故障与修复

首次正式启动时发现根日志 INFO 会让 `httpx` 把 Telegram 请求 URL 写入本地 `runtime/logs/service.log`，URL 含 Bot Token。该日志受 `.gitignore` 保护且未进入 Git/SQLite/通知，但仍属于本地敏感信息风险。

处理：

1. 受控停止唯一服务实例；
2. 为文件日志加入 Telegram Token 正则脱敏 filter；
3. 把 `httpx`/`httpcore` 日志降为 WARNING；
4. 添加日志脱敏回归测试；
5. 精确删除唯一含 Token 的旧 `service.log`，不删除数据库；
6. 重启服务并再次扫描 runtime logs，确认无 Token 或 `/bot<id>:...`。

修复后专项 Ruff、mypy 和 16 项日志/Telegram/键盘/安全测试通过，正式轮次和投递再次成功。

## 6. 2026-08-18 信号质量与流动性优化复测

针对低成交额候选偏多和 FHEUSDT 方向误判，完成以下复测：

| 检查 | 结果 |
|---|---|
| 新准入门槛 | 150 万 USDT/24h、5 万 USDT/最近完成 30m、25 bps spread |
| 实时 universe | 713 |
| ticker 初筛合格 | 196，门槛未导致候选枯竭 |
| 完成 5m 深筛 | 60 |
| 新 Top 10 | AIO、FHE、TUT、HFT、ACE、VELVET、GPS、CYS、CBR、HEMI |
| 低成交额样本 | GUN 被排除；FHE 档位仍保留 |
| pytest | 49 项通过 |
| Ruff / mypy / config-check | 全部通过；mypy 覆盖 31 个源文件，0 issue |

使用数据库保存的 FHEUSDT 真实证据进行两次 no-notify 模型回放，未写库、未发送 Telegram：

- `cycle_20260817T162038Z_1a58d9e6`：旧结果为错误的强多；新结果降为 `INDETERMINATE`，没有继续给多。回放发生在原证据窗口之后，模型明确以时点错位为理由拒绝强行判断。
- `cycle_20260817T170000Z_a072946e`：旧结果仍偏多；新结果为 `WATCH / SHORT_BIAS`，市场状态识别为“急拉后的冲击衰竭与近端回落确认”。
- 两次均在模型第一次输出通过，耗时分别为 24.406 秒和 60.519 秒；没有依赖程序硬编码生成空头方向。

自动化回归另行覆盖：FHE 式衰竭追多会被宿主拒绝并重试、仍保持正向动量的 TUT 样本不会被误伤、定时强信号后插入紧急 WATCH 轮次仍能在下一定时轮被正确追踪。

部署新版本后，正式周期 `cycle_20260817T174042Z_9705c3e0` 完成并真实投递：

- 实时 universe 713、ticker 初筛合格 197、完成 5m 深筛 60；Top 5 为 AIO、FHE、TUT、HFT、VELVET，全部满足三项流动性准入条件。
- FHE 24h 成交额约 244.7 万 USDT、最近完成 30m 成交额约 46.9 万 USDT，仍被保留；GUN 没有进入 Top 5，只因旧定时周期曾是强信号而额外复核一次，随后降为 `WATCH / WEAKENED`。
- FHE 新鲜行情结论为 `WATCH / SHORT_BIAS`，市场状态为“高周期急拉后的短周期反转下挫”；本轮没有为满足配额而强行生成 STRONG。
- SQLite 对该周期只有 1 条 `cycle` 投递记录，Telegram preflight 成功且 no-send 检查没有额外发送测试消息。
- MCP stdio 实机列出并成功调用全部 6 个只读工具；health、最新信号、FHE 详情、FHE 市场快照、动态阈值和 FHE 历史均返回有效结果。
- 服务重启后保持单实例存活，部署后日志未发现 Bot Token 或私有交易能力。

## 7. 固定双主信号与 FHE 方向修复

本轮不是通过抬高门槛把结果压成全 `WATCH`，而是修复决策链：旧版模型过度优先 1h/4h 方向，短周期冲击衰竭只作为次要风险；Prompt 又允许用 `WATCH` 回避相对择优，宿主仅校验格式和价位几何，未校验“追涨/抄底”与已完成 5m/15m 证据的冲突。当前版本要求模型同时检验延续与反转假设，用已完成 5m/15m 决定超短线近端方向，30m/1h/4h 只作结构背景；宿主对两条可见结论统一执行方向冲突与价格几何校验。

当前规则及验证结果：

- 每个成功的定时周期必须恰好产出排名 1、2 的两条相对最佳主信号，不要求伪装成 `STRONG`；
- 非入选候选的 `WATCH/NO_STRONG` 只保留在 SQLite 供诊断，不通知、不订阅实时阈值；
- 上一轮主信号退出时发送独立 `EXITED/INVALIDATED/REVERSED` 状态更新，随后停止监测；
- 紧急复核不会污染“上一轮定时双信号”，可见紧急结论执行同一方向校验；
- FHE 历史证据按 v3 Prompt 重放得到 `SHORT_BIAS`，没有再次出现原来的错误追多；旧证据缺少 30m 时明确降为低置信度，不伪造数据；
- Telegram 主通知固定两张卡；详情消息与“返回本轮”Inline 按钮、ReplyKeyboard 生命周期、失败去重与恢复均有场景测试。

## 8. 多周期预测与正式环境灰盒

当前证据包采集已完成的 5m、15m、30m、1h、4h K 线。每条主信号固定输出形成中 15m、形成中 30m、形成中 1h、下一根完整 15m 四项预测，每项包含方向、强度和宿主计算的自然时间窗口；程序不要求模型猜精确 OHLC。非入选候选四项预测必须全部为空。

### 真实 no-notify 周期

分析 `cycle_20260818T032506Z_b4c6559b` 一次模型调用成功，耗时 132704 ms：

- `PORTALUSDT SHORT_BIAS` 排名 1、中等置信度；
- `TUTUSDT LONG_BIAS` 排名 2、中等置信度；
- 两条主信号均有四项预测和两条受支持的监测指令；
- BMT、ACU 作为退出状态更新；其他非入选候选无预测、无监测；
- 消息 HTML 和长度约束通过，重复投递调用仍只有一条 delivery 记录。

已收盘样本的方向核验：PORTAL 与 TUT 的形成中 15m 分别实际为 `-0.294%`、`+3.813%`，下一根 15m 分别为 `-0.370%`、`+2.160%`，均与当时预测方向一致。该样本仅用于验证时间窗口、数据无未来泄漏和方向结算流程，数量不足以声称策略胜率。

### 正式 24/7 部署

正式周期 `cycle_20260818T033816Z_8257f94a` 成功：

- `PORTALUSDT SHORT_BIAS` 排名 1、中等置信度；
- `ACUUSDT LONG_BIAS` 排名 2、中等置信度；
- TUT 生成独立退出更新，BMT/GPS 保持内部状态；
- Telegram 真实投递一次，当前活跃实时监测集合仅含 PORTAL、ACU；
- 该周期形成中 15m 收盘后，PORTAL 实际 `-0.370%`、ACU 实际 `+1.008%`，均与预测方向一致；
- MCP stdio 实机列出并成功调用全部 6 个只读工具，最新信号为 PORTAL/ACU，详情含四项预测，市场快照含 `PA.30M`；
- 服务以 Windows 隐藏单实例持续运行，Telegram 启动 ReplyKeyboard 和正式信号均已真实发送；runtime 日志无 Token，stderr 无异常。

正式续跑时灰盒发现一次已退出 TUT 的旧阈值唤醒。根因是旧 WebSocket 循环仅在连续 30 秒没有任何行情消息时刷新监测指令；活跃行情持续到达时不会触发超时，内存订阅因而滞后。修复后指令按独立墙钟刷新，不再依赖行情静默；事件在合并后、调用模型前还会再次核对 `analysis_id + symbol + family_id`，已退出或已换代事件直接丢弃。新增“持续繁忙 WebSocket 仍刷新”和“退出后待处理事件不唤醒”两项回归测试。

修复版于上海时间 11:54 重新部署，启动周期 `cycle_20260818T035425Z_bf870ce2` 成功产生 GPS 多、PORTAL 空两条主信号并真实投递。随后服务没有按重启时刻顺延，而是在 `12:00:00.003` 准时启动自然墙钟周期 `cycle_20260818T040000Z_c008f355`，于 `12:02:03.642` 成功完成：

- `ACUUSDT LONG_BIAS` 排名 1、`PORTALUSDT SHORT_BIAS` 排名 2，均为中等置信度；
- 两条主信号均有四项预测、两条监测指令；
- GPS 仅生成 `EXITED` 更新且监测为 0，BMT/TUT 内部候选的预测和监测均为 0；
- SQLite 对本周期仅有一条 Telegram `cycle` delivery；
- 修复版启动后未再生成任何旧币紧急周期或无意义通知，服务进程继续存活。

最终自动化结果为 pytest 62 项、Ruff 全仓、mypy strict（31 个源文件）、pip check、配置校验、PowerShell 脚本语法、`git diff --check` 全部通过。生产源码没有账户、仓位、杠杆、下单或撤单能力；真实凭据只存在于 Git 忽略的 `.env`。

## 9. 结论

当前版本符合确认后的信号系统：公开市场多源辅助、Bybit USDT 永续主口径、每 30 分钟恰好两条相对最佳主信号、四项超短周期预测、上一轮退出更新、Telegram 通知、只服务当前主信号的轻量阈值紧急复核、只读 MCP 和 24/7 服务均已实现。系统不接触账户、仓位或自动交易。

实时阈值是否发生市场穿越取决于后续行情；穿越、迟滞、冷却、小时上限、紧急旁路和退出后取消监测已通过确定性测试。短期实盘样本用于核对系统行为，不构成收益率或胜率保证。
