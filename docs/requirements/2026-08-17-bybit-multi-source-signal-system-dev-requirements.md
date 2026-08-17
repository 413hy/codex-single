# Bybit 多源信号系统最终需求（已确认）

> 本文件是需求归档。实现、运维和验收细节以 `docs/DEVELOPMENT.md`、`docs/ARCHITECTURE.md`、`docs/DEPLOYMENT_VPS.md` 和 `docs/TEST_REPORT.md` 为准。

## 目标

开发并部署一个 24/7 公开市场信号服务。每 30 分钟从 Bybit USDT 线性永续全量币池中筛选 5m 波动大、具备基本流动性且近端结构有机会的币，交给 `gpt-5.6-sol` 高推理强度综合分析，再通过 Telegram 通知用户人工判断。

## 必须实现

- 动态全币池，不使用示例币硬白名单；
- 每轮 Top 5，最多 2 个强信号，允许无强信号；
- 上一轮强信号下一轮强制复核，转弱/失效仍通知完整当前分析；
- Bybit last 是参考价，mark 单列，Binance/OKX 只作可选公开旁证且不平均；
- 自建公开数据采集和证据构建，不直接运行或复制参考项目；
- 完成 5m/15m/1h/4h K 线、盘口快照、近期成交、OI、funding 和质量状态；
- 唯一方向、唯一近端目标、forming 1h、市场结构失效条件和分析详情；
- 每轮一次批模型调用，严格 JSON Schema 和宿主二次校验；
- 轻量实时阈值、迟滞、合并、冷却和每小时约 10 次普通紧急调用上限；
- Telegram ReplyKeyboard 主菜单与 InlineKeyboard 详情；
- SQLite 审计、只读 MCP、日志、单实例锁、Windows 与 VPS 服务部署；
- 开发文档、简洁架构图、参考取舍、部署文档和全量/灰盒测试报告。

## 明确不做

- 任何交易所私有 API 或 API Key；
- 账户、余额、仓位、保证金、杠杆、盈亏、订单和执行器；
- 下单数量或固定资金/目标利润反推；
- 自动下单、一键下单和 Telegram 交易按钮；
- 直接依赖 CMI、Binance MCP、followers、Kronos、TradingAgents、OpenBB、Freqtrade 或 VectorBT；
- 没有完整数据合同的连续 L2、强平流或新闻结论。

## Telegram 键盘硬约束

主导航统一为 ReplyKeyboardMarkup，精确设置：

```json
{
  "resize_keyboard": true,
  "is_persistent": false,
  "one_time_keyboard": false
}
```

任何流程不得发送 ReplyKeyboardRemove 或 `remove_keyboard=true`。发送 InlineKeyboard、详情、菜单或服务重启不能主动清除 ReplyKeyboard 状态。

## 验收

生产源码安全扫描、Ruff、mypy、pytest、真实 Bybit 深采、完整 Codex no-notify 轮次、正式 Telegram notify 轮次、MCP stdio handshake、服务隐藏启动/存活和日志审查全部通过后才算完成。
