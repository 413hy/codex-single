# Bybit 多源信号系统实施计划

- 日期：2026-08-17
- 依据：已确认设计规格与开发需求文档
- 实施原则：先打通可验证的纯数据/纯通知闭环，再逐层加入可选工具；任何阶段都不加入账户或订单能力

## 1. 当前环境事实

- 工作区：`E:\quantify\bybit-mcp`
- Python：3.13.13
- Codex CLI：0.147.0
- `uv`：未安装，因此本地脚本必须支持标准 `venv + pip`
- CMI 源码：`E:\quantify\sol_realtime_snapshot`
- CMI 源码 CLI：`python -m app --profile <SYMBOL> --data-root <PATH> capture-once --live-seconds 30 --timeout-seconds 60 --progress-json`
- 已安装的 `ContractDataCollector.exe --help` 不会作为无头 CLI 及时退出，自动化优先使用 CMI 源码入口；可执行文件适配保留为部署覆盖项
- TradingAgents 审查 commit：`a33fd4c0f134485a43553a2c23a63cb14adbd88f`
- Kronos 固定 commit：`67b630e67f6a18c9e9be918d9b4337c960db1e9a`

## 2. 技术栈

- Python 3.12+；本机以 3.13 验证
- Pydantic v2 / pydantic-settings：契约和配置
- httpx：Bybit REST 与 Telegram HTTPS
- websockets：轻量实时监测
- PyYAML：版本化配置
- aiosqlite：运行状态和历史
- Typer：CLI
- 官方 Python MCP SDK：只读 MCP（在核心闭环稳定后加入）
- pytest / pytest-asyncio / respx：测试
- Ruff / mypy：静态质量

所有依赖锁定精确版本；第三方重型研究工具使用独立环境，不进入核心依赖。

## 3. 项目结构

```text
src/bybit_signal/
  cli.py
  config.py
  domain/
  providers/
  selection/
  cmi/
  evidence/
  research/
  analysis/
  monitoring/
  notifications/
  storage/
  orchestration/
  mcp_server/
config/
contracts/
prompts/
scripts/
tests/
  unit/
  integration/
  scenario/
  fixtures/
```

## 4. 实施阶段

### 阶段 A：基础契约与安全边界

交付：

- `pyproject.toml`、包结构、CLI 和示例配置；
- Pydantic 领域模型：行情、候选、工具状态、证据、信号、追踪、监测指令；
- 配置密钥分离、结构化日志和运行目录；
- 静态安全测试：生产包不包含账户/仓位/订单客户端，Telegram markup 不含移除键盘字段。

验证：

```powershell
python -m pytest tests/unit/test_contracts.py tests/unit/test_safety_boundaries.py
python -m ruff check .
```

### 阶段 B：Bybit 全币池扫描

交付：

- 公共 instruments、tickers、kline 客户端；
- 只使用已完成 5m K 线；
- 波动、成交活跃、价差、连续性和结构距离特征；
- Top 5 排名与原因；
- REST 限流、重试、缓存和录制 fixture。

验证：使用录制的 GPS/CYS/BTC/低流动币数据，证明形成中 K 线被排除、不可交易币被排除、排名稳定且不含利润/杠杆公式。

### 阶段 C：CMI 采集适配

交付：

- 源码 CLI 子进程适配器；
- 同币种锁、不同币种并发上限、超时和进程清理；
- 原子文件发现、schema/币种/时间/哈希校验；
- PARTIAL/STALE/UNAVAILABLE 语义；
- Windows 本地路径与 VPS 可移植配置。

验证：真实运行一个公开币种 capture-once；再用 fixture 覆盖单所失败、超时、旧快照和错误币种。

### 阶段 D：实时证据工具

交付：

- Louie 兼容价格行为：多周期趋势/结构、枢轴、结构目标和失效水平；
- PYTA 兼容订单流：L1/L5/L20、delta/CVD、sweep、absorption、gap fail-close；
- Quantitative Knowledge v2：回归通道、Fib、ATR、Dow、周期、启发式形态和 evidence ID；
- Kronos 独立适配器：固定 commit、已完成5m、批量/单币、超时、概率分位数；
- 工具注册表统一报告状态、版本、耗时和证据。

验证：无未来数据测试、断档测试、Kronos unavailable/timeout/success 三路径。

### 阶段 E：低频研究平面

交付：

- 统一 `ExternalResearchSnapshot` Schema；
- Freqtrade/VectorBT 规范化报告读取与一致性聚合器；
- OpenBB/TradingAgents 低频研究快照适配接口；
- 工具版本、许可状态、数据哈希、截止时间和有效期验证；
- 独立进程权限边界。

V1 不自动安装或运行所有第三方框架。先交付可测试的契约、摄取器和调度边界；仅在许可、资源和数据覆盖预检通过后启用相应 runner。

验证：CYS 无 TradingAgents 覆盖但 BTC 背景可用的代表场景；过期或资产身份不匹配的研究快照必须拒绝。

### 阶段 F：Codex 分析

交付：

- 紧凑、无密钥、带哈希和 evidence ID 的分析上下文；
- 系统 Prompt 和严格输出 JSON Schema；
- `codex exec --json --ephemeral --sandbox read-only --model gpt-5.6-sol` runner；
- `analysis_id`、工具状态、证据引用、Token 用量和错误分类；
- 每轮当前判断不包含上一轮方向/目标；协调器后置比较。

验证：正常、超时、无效 JSON、错误 analysis_id、未知证据、工具缺失和无强信号路径。

### 阶段 G：存储、周期编排与实时监测

交付：

- SQLite schema、迁移、原子 cycle 状态和幂等键；
- `:00/:30` 调度、Top 5 + 上轮信号复查、最多2个强信号；
- 动态指令、迟滞、锁存、family ID、60秒合并、10分钟冷却和每小时10次软上限；
- 重启恢复和过期策略清理。

验证：CYS 14:00 信号、14:12 失效突破、14:30 强制复查完整场景。

### 阶段 H：Telegram 通知

交付：

- 核心结论格式、无强信号摘要、减弱/失效和紧急提醒；
- “查看分析详情” InlineKeyboard；
- 最小 ReplyKeyboard：最新信号、系统状态、帮助；
- `/start`、`/menu`、`/latest`、`/status`、`/help`；
- Chat/User 允许列表、callback 64字节、先 answer 再 edit、编辑失败降级；
- 幂等投递和重启 offset 恢复。

验证：使用 Telegram 模拟服务跑发送、详情、菜单恢复、未授权和失败重试；获得真实 Bot Token 后再做真实私聊验收。

### 阶段 I：只读 MCP/CLI 与运维

交付：

- health、scan、latest signal、details、snapshot、monitoring、history 工具；
- 默认 stdio/localhost；
- Windows PowerShell 和 Linux VPS/systemd 或容器部署；
- 日志轮转、健康检查、备份、升级和回滚说明。

验证：由独立 Codex 调用只读工具完成“查看最新信号及证据”任务；网络扫描证明没有裸露公网写接口。

### 阶段 J：全量与灰盒验收

交付：

- 全测试集、覆盖率、静态检查、依赖审计；
- 录制数据端到端；
- 至少一次真实公开 Bybit + CMI 数据运行；
- Telegram 沙箱/真实私聊（凭据可用时）；
- 24小时稳定性短时加速模拟和故障注入；
- 最终架构图、开发文档、VPS部署交接文档。

## 5. 实施顺序与提交策略

每个阶段独立提交，提交前必须通过该阶段测试。禁止用 `|| exit 0`、忽略测试或手工修改输出绕过失败。

优先顺序：A → B → C → D → F → G → H → E → I → J。

研究平面 E 在核心实时闭环之后实现，因为它只提供软证据；其契约会在 A/D 中先定义，避免后续破坏上下文格式。

## 6. 代表场景作为持续验收门

每完成一个阶段都使用同一个 CYS 场景扩展验证：

- 正常：多所数据完整、Kronos 可用、生成偏空示例；
- 边界：TradingAgents 不支持 CYS，只提供 BTC 背景；
- 失败：OKX 缺失、Kronos 超时、Codex 首次输出无效；
- 状态变化：上轮信号本轮减弱/失效；
- 紧急：价格跨越动态失效阈值；
- 通知：固定周期和紧急提醒均可查看详情；
- 安全：无任何私有交易 API 或下单动作。

只有真实用户路径通过，阶段才视为完成；单纯 import、build 或 API 200 不算完整验收。
