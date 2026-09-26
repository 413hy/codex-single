# 三系统接口

`single-analysis` 是唯一模型调用和信号发布者。它只读取 TradingView、Bybit 公共行情，以及对冲交易系统的本地只读快照；不持有交易密钥，不下单。

共享 `signals.db` 的 `publications` 按 `signal_id` 唯一。当前 payload `version=2`，包含 `cycle_id`、币种、LONG/SHORT/SKIP、原因、普通候选标记、可选对冲组及 generation、行情时间、发布时间和到期时间。到期时间恒为发布时间后 600 秒。同一发布不因再次写入而延长有效期。

两套交易系统只读查询尚未过期的发布，各自将 `feed:<signal_id>` 原子认领到独立账本。普通方向由各交易系统按自身资金、仓位和风控决定是否执行。非普通对冲方向只由归属匹配且 generation 有效的对冲系统处理；普通交易系统记录后跳过。SKIP 仅记录，不下单。失败或重启后不得重新认领旧 ID 制造成交。

对冲快照取自对冲系统交易所同步的仓位及同事务保存的独立观测时间。分析端核对 20 秒时效、LOCKED 阶段、两侧账本归属、双向槽位、数量和均价；消费端再次核对组状态及 generation。

采集器在 `src/analysis_core/tradingview.py`、`tradingview_discovery.py` 和 `tradingview_sources.py`。新增公开数据源按 `name/stage/collect` 适配，明确身份、范围、时间和缺失语义。TradingView 网页内部接口不是稳定公共 API；不可用时显式失败或记录可选背景缺失。
