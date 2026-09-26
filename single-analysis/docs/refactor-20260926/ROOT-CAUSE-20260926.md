# 14:02 TradingView 异常根因与信号来源

## 现场证据

2026-09-26 14:00 上海时间启动的轮次为 `scheduled:1e2011aab2069d183b2c66fb`，结束状态为 `PARTIAL_ERROR`。候选池 20 币，两项 TradingView 采集失败；剩余 18 币进入初选模型。本轮仍有有效方向发布，受影响的两个币没有发布信号。

| 币 | 核实结果 | 根因与处理 |
| --- | --- | --- |
| MSFUUSDT | 14:04 的 TradingView `time|15` 为 13:15 上海时间，15m 栏开盘已约 49 分钟；同一时刻 Bybit 最近两根 15m K 线成交量均为 0。Bybit 合约状态仍为 `Trading`。 | 合约可交易但近期无成交，TradingView 技术栏停在最后活跃的 K 线。原候选发现只看综合技术评级，MSFU 的日级 `Recommend.All=1` 且活跃度变化很大，即使 24h 成交额只有约 3.45 万 USDT，也被选入候选池；到逐币校验时才被拦截。现已在 CEX 发现阶段取得 15m/30m/1h 栏时间并先排除缺失或过期的币，审计记录排除原因，同时以 15m 与 1h 评级及相对流动性排列技术候选。 |
| STONKUSDT | TradingView 的 `/symbols/STONKUSDT.P/technicals/?exchange=BYBIT` 返回 404；同币的合约主页返回 200 且标识精确为 `BYBIT:STONKUSDT.P`，`scanner.tradingview.com/symbol` 返回 200，15m OHLCV 和技术指标有值；Bybit 合约状态 `Trading`，最近 15m K 线有成交。 | TradingView 某个网页路由不可用，不等于合约或全部技术数据不存在。原采集器强制要求 technicals 网页 200，造成误排除。现仅对该路由的 404 使用同币合约主页验证身份，再照常读取同合约字段；若主页、字段、短周期时效或身份校验失败，继续拒绝。TradingView 未公开这个路由返回 404 的内部原因，不能断言是下架或需要登录。 |

现场另一次只读复核曾遇到技术页 HTTP 403；403 仍按访问受限失败，不以其他页面绕开，也不重试。TradingView 网页接口及其可用性由第三方控制。官方说明 CEX 筛选器可筛选交易对并提供成交、技术指标；技术评级是均线和振荡指标的组合，可能随未收盘 K 线变化；最近成交时间可用于区分活跃与不活跃币。[CEX 筛选器](https://www.tradingview.com/support/solutions/43000746345-what-is-the-difference-between-crypto-%D1%81oins-dex-and-cex-screeners/)、[技术评级](https://www.tradingview.com/support/solutions/43000614331-technical-ratings/)、[最近成交时间](https://www.tradingview.com/support/solutions/43000753164-last-trade-time/)。

14:14 的独立只读复核使用上线后的采集器成功采得 STONK 的同合约七周期数据，证实 technicals 404 → 同币主页身份核验 → 字段采集这条路径可运行，且没有发布交易信号。14:09 的 CEX 复核中 MSFU 已不在本轮待审池。两条原异常在核实后标记为已解决，诊断事件保留在分析库；外部页面以后仍可能再次失败。

## 交易信号怎样产生

1. TradingView CEX 筛选器发现 Bybit USDT 永续合约。Bybit 公共合约清单只核实仍可交易，不参与全市场评分。发现候选时使用 24h 成交额、成交额变化、价格变化、15m/1h 技术评级和短周期栏时间构造最多 20 币的均衡池。
2. 对候选逐币读取 TradingView 同合约 5m、15m、30m、1h、2h、4h、1d OHLCV 与技术评级、RSI、ADX、MACD、EMA/SMA、ATR、布林带、Pivot 等；Ideas 和币种聚合指标是弱背景。Terra 模型比较所有有效候选，初选 1—10 币及初步 LONG/SHORT。原始响应留在 `TV_SYMBOL_EVIDENCE`，压缩后的实际模型输入留在 `TV_SELECTION_EVIDENCE`。
3. 对初选币采 Bybit 已收盘 K 线、量价指标、盘口和主动成交样本，附上 TradingView 新闻背景。Sol 模型收到初选方向、TradingView 压缩证据及 Bybit 证据，横向选 1—3 个最终 LONG/SHORT。真实输入和输出分别留在 `FINAL_SELECTION_EVIDENCE` 与 `FINAL_SELECTION_RESULT`。最终方向可以改变初判。
4. 读取新鲜 LOCKED 双仓；与普通最终名单重合的直接复用，其他双仓币另取 TradingView 与 Bybit 证据判向或 SKIP。每轮每币只写一次共享 `signals.db`，发布后 600 秒过期；两个交易端独立认领并独立执行风控。

14:00 这轮的实际轨迹：TradingView 初选 8 币，其中 RARE 为第 1 名 LONG/LOW、ARK 为第 3 名 LONG/LOW、ETH 为第 5 名 SHORT/LOW。Bybit 二次复核后，最终普通方向为 ARK LONG/MEDIUM、RARE LONG/LOW、ETH SHORT/LOW；额外双仓 MUBARAK 为 SHORT。最终模型输入确实包含每币的 TradingView 七个周期快照和初判，也包含 Bybit 已收盘量价与订单流。模型无法给出可核验的内部权重，现有最终理由主要引用 Bybit，不能声称 TradingView 对每个最终方向有可量化贡献。分析 Bot 的币种详情现分别展示 TradingView 初判依据与最终复核理由，便于核对两阶段是否一致。

本次没有改动模型提示词或方向 Schema。数据选择、页面处理与展示的回归测试覆盖上述边界；真实模型提示词验证仍沿用 `model_validation.json` 的已有版本证据。
