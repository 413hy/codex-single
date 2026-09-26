# 方向模型与 TradingView 参考数据复核

后续用户要求扩大可参考资料；以下记录的是当时的 v12 审核基线。当前 v14 已增加同合约市场指标及带原始标的标签的社区观点，见 PLAN.md 和最新验证记录。原有“不接入Ideas”的结论已被后续用户要求覆盖。

日期：2026-09-23（上海时间）。本文件记录针对用户提供的根目录`tradingview.py`脚本和当前方向模块的复核。隔离审阅与生产轮次均使用`gpt-6-sol / medium`；隔离审阅不发布信号。

## 结论

生产选币仍按十份有效候选证据比较，选1—3；TradingView不进入scanner或Screening.select。方向模型对已选币和待判双仓币逐币单次调用，首选Schema限定LONG/SHORT，其余允许SKIP。15m/30m/1h/2h近一周已收盘Bybit量价为核心，TradingView仅辅助。最近成功生产轮次的MODEL_INPUT均有同币种`BYBIT:<symbol>.P`、七周期网页参考；模型理由披露未确认收盘与报价时间未知。该样本证实通路和合同执行，不证明方向收益。

用户给出的脚本截获TradingView全市场scanner排行榜并解析页面表格、社区Ideas，范围含BINANCE/CRYPTO。它适合发现市场关注对象，但当前系统已固定自己的十币筛选；把排行或他人观点当作已选BYBIT永续币的方向依据会混淆标的及证据性质。现行采集器针对已选中的同一BYBIT永续合约，读取技术分析页及其页面数据请求，采集七周期OHLCV、评级、振荡指标、均线、波动和枢轴字段。TradingView[官方说明](https://www.tradingview.com/support/solutions/43000614331-technical-ratings/)表明技术评级是均线与振荡指标的组合，技术页与筛选器的评级同源，不能重复计票；[社区Ideas说明](https://www.tradingview.com/support/solutions/43000761245-tradingview-social-network/)表明Ideas由用户发布，不是交易所行情事实。因此未照搬脚本的排行或Ideas。

用户最新规则取消方向参考数据的固定年龄上限。复核发现并移除了末尾未完成Bybit K线残留的45秒门槛。仍要求其只出现在序列末尾，并保留币种身份、数值、周期连续性、完整一周已收盘主周期、来源标记与模型Schema。对冲快照20秒与发布信号60秒有效期是其他独立合同。

## 模型对齐证据

隔离模型服务审阅记录依次位于：

- `runtime/tv-alignment-review-20260923T093306Z`：初评指出三个问题，后经代码与现有故障合同核对，均属于误判。
- `runtime/tv-alignment-followup-20260923T093448Z`：确认初评三项不成立，但依据未更新的旧文档要求恢复已被用户取消的时效门槛。
- `runtime/tv-alignment-final-20260923T093628Z`：按最新用户规则指出最后一根未完成Bybit K线残留45秒门槛。
- `runtime/tv-alignment-final-20260923T093757Z`：修复后返回`aligned=true`、`required_fixes=[]`、`prompt_change_needed=false`。MODEL_INPUT/OUTPUT各一次，模型`gpt-6-sol`、推理强度`medium`，审阅提示词SHA256为`e78362d50c351b92bc4bdbb01fa2c2149524577a67959380420528e328257521`，完整输入SHA256为`ae19c5ec0ce6ed08da62e22afab94ff0dfb58c2448a91ae58d503bf96c6e3046`。

生产方向提示词`direction_v12.md`保持原版，SHA256为`85e01c250d1e2d511a741a57719c79c3a8e5681c69fb45ac96fca9b36c56ea04`；未因审阅而改变选币、方向Schema或交易端。`ruff check src tests`、`mypy src`和`pytest -q`通过，215项测试通过。
