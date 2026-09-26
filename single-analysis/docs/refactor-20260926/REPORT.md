# TradingView 方向信号重构（2026-09-26）

## 运行链路

```text
上海固定周期
  → TradingView CEX Screener 发现 BYBIT USDT 永续合约
  → Bybit 公开合约清单核实仍可交易
  → 逐币抓 TradingView 证据
  → Terra 模型初选 1—10 个币和初步方向
  → 对初选币抓 Bybit 量价、盘口、成交样本
  → Sol 模型最终选 1—3 个 LONG/SHORT
  → 读取 20 秒内 LOCKED 双仓快照
  → 重合币复用最终方向；额外双仓抓 TV+Bybit 后 Sol 判向或 SKIP
  → 按币去重，写入一次 signals.db；两套交易系统独立消费
```

主预测时域为未来 1—2 小时，近期 15m、30m、1h 已收盘量价优先；2h 与可用一周结构辅助。普通最终名单正常有效输入至少一币，最多三币。TradingView 初判与最终方向可以不同。正常首选必须 LONG/SHORT，额外双仓可 SKIP。只用 Bybit 公共行情做交易所数据复核，不用旧 Bybit 全市场评分流程。发布 TTL 为 600 秒；每个消费者继续独立持久认领，旧信号不重放。

为控制网页与模型调用量，TradingView CEX 全页扫描后按成交额、涨跌两侧、活跃度变化、技术评级构成最多 20 币的均衡待审池；这一步只用 TradingView 数据，不输出方向。实际公开页面联调时待审池包含 BTC、ETH、SOL 等流动性较高合约，也包含涨跌两侧的其他合约；模型负责从待审池初选 1—10 个。

## TradingView 来源和边界

| 来源 | 当前采集 | 用途与约束 |
| --- | --- | --- |
| [CEX Screener](https://www.tradingview.com/crypto-screener/) | BYBIT USDT.P 合约成交量、变化、技术评级、同市场排名 | 发现候选和活跃度背景；不是方向结论 |
| [技术分析页](https://www.tradingview.com/symbols/BTCUSDT.P/technicals/?exchange=BYBIT) | 5m、15m、30m、1h、2h、4h、1d 的 OHLCV 快照、技术评级、振荡、均线、ATR、布林带、Pivot 及可配字段 | 同合约方向线索；网页值不承诺已收盘或实时 |
| [Ideas 页](https://www.tradingview.com/symbols/BTCUSDT.P/ideas/?exchange=BYBIT) | 有来源标记、时间和方向标签的可见观点卡片 | 用户观点，弱背景 |
| [Crypto Coins Screener](https://www.tradingview.com/crypto-coins-screener/) | 币种聚合表现、波动率、可用情绪和社交字段 | 跨市场币种背景，不冒充 Bybit 合约行情 |
| [News 页](https://www.tradingview.com/symbols/BTCUSDT.P/news/?exchange=BYBIT) | 公开可见新闻标题、来源、发布时间 | 事件背景；无可核验卡片则显式记录缺失 |

TradingView 官方说明 CEX 与币种聚合筛选器范围不同；技术评级由多个派生指标组成，不应重复计票；公开数据 API 并不向普通用户提供。这些网页内部请求是可替换的数据适配器，不保证接口稳定，也不绕过登录。来源身份和字段形状严格校验，来源故障或缺失分别审计。当前公开页面可读，暂不需要登录；若将来要用仅登录可见的数据，再请用户协助登录。

官方文档： [CEX 与 Crypto Coins 的区别](https://www.tradingview.com/support/solutions/43000746345-what-is-the-difference-between-crypto-%D1%81oins-dex-and-cex-screeners/)、[技术评级](https://www.tradingview.com/support/solutions/43000614331-technical-ratings/)、[Coins Screener](https://www.tradingview.com/support/solutions/43000718742-crypto-coins-screener-discover-hidden-gems/)、[Ideas](https://www.tradingview.com/support/solutions/43000591338-publishing-and-updating-ideas/)、[News Flow](https://www.tradingview.com/support/solutions/43000732560-news-flow-s-filters-overview/)、[公开 API 说明](https://www.tradingview.com/support/solutions/43000474413-i-need-access-to-your-api-in-order-to-get-data-or-indicator-values/)。

## 可扩展接口与审计

`tradingview_sources.py` 的适配器实现 `name/stage/collect`，注册到 `SourceRegistry`。核心同合约快照和合约身份为必需；币种级背景与新闻为可选。新增来源须说明对象范围、时间语义、数据缺失与身份校验，避免把同源派生指标当多份独立确认。模型只接收压缩后的近期关键证据，原始抓取结果另写审计事件。提示词版本为 `tv_selection_v1.md`、`direction_selection_v1.md`、`direction_v15.md`；旧扫描器、筛选契约和提示词在 `docs/legacy-20260926/` 留存以供回溯，生产路径无旧扫描入口。

## 验证和发布状态

隔离测试不向共享信号库写入；真实模型验证记录见 `model_validation.json`。模型讨论轮次见 `review_round1.json`、`review_round2.json`、`review_round3.json`。第三轮代码审阅指出两项确定问题：连续但过期的 Bybit K 线，以及消费端只取前 100 条有效信号造成的遗漏；现已分别增加主周期最新已收盘时间校验和读取全部有效记录，并加回归测试。上线前需让分析生产服务和两个交易消费者加载同一 600 秒信号合同；旧库信号不得修改有效期或重放。分析服务当前保持原有暂停状态，恢复仅由用户既有 Bot 控件触发。
