# 全量与灰盒测试报告

测试日期：2026-08-17～2026-08-18（Asia/Shanghai）
环境：Windows 10、Python 3.13、真实公网公开市场、真实 Telegram Bot、真实 Codex CLI

## 1. 自动化与静态检查

| 检查 | 结果 |
|---|---|
| Ruff 全仓 | 通过 |
| mypy strict（31 个源码文件） | 通过，0 issue |
| pytest 全量 | 通过，45 项 |
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

## 7. 结论

当前版本符合确认后的 V1：多币种公开市场数据、30 分钟 Codex 信号、上轮转弱提醒、Telegram 通知、轻量阈值紧急复核、只读 MCP 和 24/7 服务均已实现。未发现账户、仓位或自动交易能力。

实时阈值是否发生市场穿越取决于后续行情；状态机的穿越、迟滞、冷却、小时上限与紧急旁路已通过确定性测试，正式服务会持续订阅模型输出的可支持指标。
