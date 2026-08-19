# Bybit 多源交易信号服务

这是一个面向 Bybit USDT 线性永续的 24/7 公开市场分析与 Telegram 通知服务。系统每 30 分钟扫描全量合约，选出 5 个高波动且具备基本流动性的候选，由 `gpt-5.6-terra / medium` 一次性综合分析并固定发送 2 个相对最优主信号；两轮之间由轻量阈值监测器在结构变化时触发紧急复核。该默认组合来自 4 组配置 × 3 轮隔离全流程对比，不是按 token 成本选择。

当前版本只提供人工判断用信号：没有 Bybit API Key、账户、余额、仓位、保证金、杠杆、盈亏、下单或撤单能力。

## 已实现

- Bybit 全量 USDT 线性永续动态币池与 5m 波动/流动性筛选；默认要求 24h 成交额不低于 150 万 USDT、最近完成 30m 成交额不低于 5 万 USDT、spread 不高于 25 bps；
- 自建 Bybit 公共数据深采：完成 1m/5m/15m/30m/1h/4h K 线各 239 根、last/mark、盘口快照、近期成交、OI、资金费率、多空账户比与公开强平流；
- Binance USD-M 与 OKX Swap 可选 ticker 一致性旁证，不混合价格；
- 多周期价格行为、结构、波动、成交和数据质量证据包；
- 严格 JSON Schema、证据 ID、时间截止线、只读宿主管理工具、固定 2 个相对最优主信号和最多 2 次模型输出尝试；单次模型超过 300 秒会终止整个进程树后重试；
- Codex 在系统临时空目录中运行，关闭原生 Skill、shell、插件和浏览器能力；模型只能通过结构化 JSON 请求宿主白名单只读市场工具；
- 模型是方向与阈值语义的唯一判断者；宿主只校验 Schema、证据 ID、反向完成柱、条件尚未满足与版本权威性，不按隐藏 ATR/距离参数替模型改方向或调阈值；主信号先发送，随后同一模型基于新采证据独立复核监测规则；模型可正常返回“无合格阈值”，不重试、不报错、不发失败通知；普通基线变化由宿主机械更新，只有激活前条件已经越线或不可执行才按币静默重采/重审；大致止盈位仅展示，完全不参与择优、freshness、阈值或失效判断；
- 每条主信号输出形成中 15m/30m/1h 与下一根 15m 的方向、强度和窗口；
- 上一轮定时主信号自动复核，紧急轮次不会覆盖追踪基准，退出或失效仍单独提醒；
- Telegram ReplyKeyboard 主导航、InlineKeyboard 分析详情/返回本轮、异常诊断/精准重试、白名单和幂等投递；所有异常通知显示流程、具体步骤、直接原因、影响与解决方式；
- Bybit 公共 WebSocket 持续监测模型指定的完成 K 线、价格、成交差额、turnover、spread、盘口、OI、funding 与强平阈值；阈值突破仅代表需要模型复核，不直接等于方向失效。同一 `analysis_id + symbol` 只接受首个事件，随后暂停整套旧阈值、通知、重采并紧急分析；模型根据新证据输出 `MAINTAINED / REVERSED / INVALIDATED` 和整套新阈值。旧规则持续生效到自然半小时任务真正开始，在 `:00/:30` 边界原子停用，只有成功的新 analysis ID 才恢复监测；每小时约 10 次仅作软预算告警；
- SQLite 审计、只读 stdio MCP、轮转日志和服务单实例锁。

## 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -c requirements-dev.lock.txt -e ".[dev]"
Copy-Item config\system.example.yaml config\system.local.yaml
Copy-Item .env.example .env
```

在 `config/system.local.yaml` 启用 Telegram 并填写 chat/user 白名单，在 `.env` 里只填写 `BYBIT_SIGNAL_TELEGRAM__TOKEN`。随后验证：

```powershell
.\.venv\Scripts\python.exe -m bybit_signal config-check --config config\system.local.yaml
.\.venv\Scripts\python.exe -m bybit_signal telegram-check --config config\system.local.yaml --send-test
.\.venv\Scripts\python.exe -m bybit_signal market-capture --config config\system.local.yaml --symbol CYSUSDT
.\.venv\Scripts\python.exe -m bybit_signal run-cycle --config config\system.local.yaml --no-notify
.\scripts\start-service.ps1
```

## 文档

- [VPS Codex Debian 部署交接单](VPS_CODEX_HANDOFF.md)
- [当前文档导航与权威顺序](docs/CURRENT_DOCUMENTS.md)
- [开发需求与实现说明](docs/DEVELOPMENT.md)
- [简洁架构图](docs/ARCHITECTURE.md)
- [参考项目审查与取舍](docs/REFERENCE_REVIEW.md)
- [VPS 部署与交接](docs/DEPLOYMENT_VPS.md)
- [测试报告](docs/TEST_REPORT.md)
- [模型三轮评测与定制结果](docs/MODEL_EVALUATION_2026-08-18.md)

分析 Prompt 位于 [prompts/signal_analysis_zh.md](prompts/signal_analysis_zh.md)。它是结合当前数据合同重写的系统 Prompt，不会运行或复制参考项目。
