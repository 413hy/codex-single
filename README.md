# Bybit 多源交易信号服务

这是一个面向 Bybit USDT 线性永续的 24/7 公开市场分析与 Telegram 通知服务。系统每 30 分钟扫描全量合约，选出 5 个高波动且具备基本流动性的候选，由 `gpt-5.6-sol`（`high`）一次性综合分析，最多发送 2 个强信号；两轮之间由轻量阈值监测器在结构变化时触发紧急复核。

当前版本只提供人工判断用信号：没有 Bybit API Key、账户、余额、仓位、保证金、杠杆、盈亏、下单或撤单能力。

## 已实现

- Bybit 全量 USDT 线性永续动态币池与 5m 波动/流动性筛选；
- 自建 Bybit 公共数据深采：完成 5m/15m/1h/4h K 线、last/mark、盘口快照、近期成交、OI 和资金费率；
- Binance USD-M 与 OKX Swap 可选 ticker 一致性旁证，不混合价格；
- 多周期价格行为、结构、波动、成交和数据质量证据包；
- 严格 JSON Schema、证据 ID、时间截止线、最多 2 个强信号和模型输出重试；
- 上一轮强信号自动复核，转弱或失效仍发送完整结论；
- Telegram ReplyKeyboard 主导航、InlineKeyboard 分析详情、白名单和幂等投递；
- Bybit 公共 WebSocket 动态阈值、迟滞、60 秒合并、10 分钟单币冷却和每小时软上限；
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

- [开发需求与实现说明](docs/DEVELOPMENT.md)
- [简洁架构图](docs/ARCHITECTURE.md)
- [参考项目审查与取舍](docs/REFERENCE_REVIEW.md)
- [VPS 部署与交接](docs/DEPLOYMENT_VPS.md)
- [测试报告](docs/TEST_REPORT.md)

分析 Prompt 位于 [prompts/signal_analysis_zh.md](prompts/signal_analysis_zh.md)。它是结合当前数据合同重写的系统 Prompt，不会运行或复制参考项目。
