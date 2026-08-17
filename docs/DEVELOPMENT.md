# 开发需求与实现说明

## 1. 最终需求镜像

系统面向人工手动交易判断，全天候运行：

1. 每个整点和半点扫描 Bybit 全量 USDT 线性永续；服务启动后先立即运行一轮。
2. 从新鲜且可交易的动态币池中，按完成 5m K 线波动、成交额、spread 和数据完整度选 Top 5；GPS、TUT、ALICE、AIO、PORTAL、CYS、BTW、HEMI、SNXX、H 等只是偏好示例，不是白名单。
3. 对 Top 5 和上一轮强信号币重新采集公开市场数据。上一轮强信号本轮即使转弱或不再进入 Top 5，也必须复核并发送结果。
4. `gpt-5.6-sol`、`high` 在一次批调用中独立分析全部币种，最多输出 2 个强机会，允许零个。
5. Telegram 每 30 分钟都发送一条本轮结论；强信号和上一轮转弱/失效币优先展示，按钮可查看完整详情。
6. 两轮之间使用公开 WebSocket 观察模型指定的少量动态阈值。穿越后只触发重新采集和紧急 Codex 复核，不直接形成信号或执行交易。
7. 主导航只使用 ReplyKeyboardMarkup：`resize_keyboard=true`、`is_persistent=false`、`one_time_keyboard=false`；任何流程都不移除键盘。详情使用 InlineKeyboardMarkup，不改变已有 ReplyKeyboard 状态。
8. 服务可在 Windows 本机隐藏运行，也能把项目和本文档交给 VPS Codex，按 systemd 部署。

## 2. 强制边界

生产系统只使用公开市场数据，不需要 Bybit/Binance/OKX API Key。代码和 Prompt 均不得出现：

- 账户、余额、仓位、保证金、杠杆、盈亏、费率账户等级；
- 下单数量、入场价建议、开仓/加仓/减仓/平仓/撤单；
- 收益保证、基于固定资金或目标利润反推价格；
- 私有 REST/WS、签名、API Secret 或交易权限；
- Telegram 一键下单按钮。

系统给出的“止盈位”是方向前方唯一近端结构目标；“失效条件”是市场结构条件，都由用户自行判断，不能解释成受托执行。

## 3. 数据合同

### 3.1 动态候选

- 从 Bybit instruments/tickers 获取当前可交易 USDT 线性永续。
- 初筛排除成交额不足、spread 过大、完成 5m K 线不足、缺口过多或波动不达阈值的币。
- 排名使用完成 5m K 线的 median range、最大绝对收益、近期收益离散度和 log turnover；分数只表示值得分析，不代表多空。

### 3.2 深采

每个待分析币种采集：

- Bybit last、mark、index、bid/ask、24h turnover、funding、OI；
- 完成 5m/15m/1h/4h K 线，各周期验证身份、间隔、缺口、陈旧和形成中柱排除；
- order book 50 档 REST 快照；
- recent trades 最多 1000 条，仅在覆盖窗口完整时计算 delta；
- OI 5m 历史 48 点；
- 可选 Binance USD-M 与 OKX Swap ticker，分别保留来源和时间。

同批 ticker 只请求一次。深采并发由 `runtime.market_request_concurrency` 限制；可选来源失败写入 `collection_failures`，不能填零或覆盖 Bybit。

### 3.3 证据

证据包包含冻结截止时间、源快照哈希、canonical last/mark、证据 ID 和工具质量状态。主要字段：

- 5m/15m/1h/4h OHLCV、returns、ATR%、EMA9/20/50、RSI14、MACD histogram；
- rolling20 高低/中位、方向效率、K 线重叠、bull ratio、turnover ratio；
- 已确认 pivot 的发生时间和确认时间，不能使用未来柱；
- spread、top5/top20 深度与 imbalance（只声明 REST 快照）；
- 有覆盖门槛的 1m/5m recent-trade delta；
- funding、OI 与 OI 变化；
- 可选跨所 price divergence，Bybit 始终是基准。

## 4. Codex 分析合同

- CLI 调用为 `codex exec --ephemeral --ignore-user-config --sandbox read-only`。
- 模型和推理强度固定来自配置，默认 `gpt-5.6-sol` / `high`。
- 输入是排序、压缩、哈希后的单批 JSON；市场文本是数据，不具备指令权限。
- 输出必须通过 Pydantic 生成的严格 JSON Schema；最多重试 2 次。
- 宿主继续校验币种集合、evidence ID、强信号目标/失效几何、目标最大距离、forming 1h 窗口、监测 metric、family ID 和 1800–3600 秒有效期。
- STRONG 需要 5m 时机、15m 结构和 1h/4h 背景没有未解释强冲突；逆趋势不能只凭超买超卖。
- WATCH/NO_STRONG 仍需解释缺少的确认。追踪币只要不是 INDETERMINATE，应尽量给完整当前方向、目标、forming 1h 和失效条件。
- 上一轮方向不放入模型上下文，避免锚定；模型本轮结论完成后，宿主才比较方向、目标和失效状态。

完整 Prompt 见 `prompts/signal_analysis_zh.md`。

## 5. Telegram 合同

- Token 只从 `.env` 的 `BYBIT_SIGNAL_TELEGRAM__TOKEN` 读取；chat/user 均需白名单。
- 每轮只投递一次，SQLite `deliveries` 负责幂等。
- 核心通知包括：时间、候选、强信号数、参考价、综合方向、市场状态、唯一目标、forming 1h、相比上一轮和方向失效。
- InlineKeyboard callback 只包含 `analysis_id + symbol`，最长 64 UTF-8 bytes；点击后从 SQLite 获取已落库详情。
- `/start`、`/menu`、`/latest`、`/status` 与虚拟键盘均返回白名单用户；未知和未授权消息不响应。
- 不发送 ReplyKeyboardRemove，不设置 `remove_keyboard`，不使用 one-time 或 persistent 常驻模式。

## 6. 实时监测合同

支持的指标只有 last、mark、spread、OI、funding 和完成 5m/15m/1h close。模型通常为强信号、观察信号和追踪币生成 1–2 条动态阈值：

- 首次观测只武装，不把已经满足的条件当作新穿越；
- crossing 后必须离开 hysteresis 区域才重新武装；
- 同币 60 秒事件合并，10 分钟冷却；
- 普通唤醒滚动 1 小时最多 10 次；family ID 含 invalidation 的紧急事件可绕过小时软上限，但不绕过单币冷却；
- 指令在下一次 30 分钟轮次附近过期，定时轮次会替换为新指令；
- 紧急轮次与定时轮次共用异步锁，禁止并行调用模型。

## 7. 存储、MCP 与运维

- SQLite WAL 保存 cycle、conclusion、evidence bundle、delivery 和 bot offset。
- MCP 只通过 stdio 暴露六个只读工具：健康、最新信号、详情、市场证据、监测阈值和历史。
- 服务日志 UTC 写入 `runtime/logs/service.log`，5 MiB 轮转 5 份。
- `runtime/state/service.lock` 防止重复调度器；Windows PID 文件只辅助脚本显示和停止，不代替文件锁。
- 正式服务遇到单轮失败会记录异常并尝试发错误通知，下一整点/半点继续；Telegram 错误通知本身失败不能终止调度器。

## 8. 代码导航

| 目录/文件 | 职责 |
|---|---|
| `providers/bybit.py` | Bybit 公共 REST 与类型模型 |
| `providers/cross_exchange.py` | Binance/OKX 公开 ticker 旁证 |
| `providers/deep_market.py` | 多周期深采、冻结截止和质量验证 |
| `selection/scanner.py` | 动态币池与 Top 5 初筛 |
| `evidence/builder.py` | 自建指标、结构和 evidence bundle |
| `analysis/codex.py` | ephemeral Codex、Schema 与宿主校验 |
| `orchestration/cycle.py` | 定时/紧急分析、追踪比较、落库 |
| `monitoring/` | 阈值状态机、WS、合并与限频 |
| `notifications/` | Telegram 格式、键盘、轮询与详情 |
| `mcp_server.py` | 本地只读 MCP |
| `service.py` / `operations.py` | 24/7 调度、生命周期、日志和锁 |

## 9. 验收标准

- 静态源码没有私有交易端点、签名或键盘移除字段；
- 扫描与深采使用真实公开网络，完成柱和时间截止检查通过；
- 每轮只调用一次模型，输出覆盖全部候选和追踪币，强信号不超过 2；
- 上轮强信号转弱或失效仍出现在本轮通知；
- 模型格式、未知 evidence、错误目标方向、错误 forming 1h 和不支持监测指标均被拒绝或重试；
- Telegram 主 ReplyKeyboard 参数逐项精确匹配需求，InlineKeyboard 不移除它；
- 阈值 crossing、hysteresis、过期、冷却、合并和小时软上限有自动化测试；
- Ruff、mypy、pytest、真实 market-capture、完整 no-notify、正式 notify、MCP handshake 和服务存活检查全部通过。

## 10. 官方接口依据

- Telegram Bot API（ReplyKeyboard、InlineKeyboard、getUpdates）：https://core.telegram.org/bots/api
- Bybit V5 Kline：https://bybit-exchange.github.io/docs/v5/market/kline
- Bybit public WebSocket Kline：https://bybit-exchange.github.io/docs/v5/websocket/public/kline
- Bybit WebSocket endpoints：https://bybit-exchange.github.io/docs/v5/ws/connect
- MCP Python SDK：https://github.com/modelcontextprotocol/python-sdk
- Binance USD-M Futures market data：https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- OKX V5 API：https://www.okx.com/docs-v5/en/
