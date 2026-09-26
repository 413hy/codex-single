# 数据与模型逐文件审核（2026-09-26）

审核为源码/测试静态检查 + 6 次 Bybit 公共订单簿请求；未调用真实模型、未发布信号、未操作服务/交易。只修改 orderflow.py 和 test_orderflow.py，根审已接手其余修改与总验证。

## 已确认的问题

1. **REST 重复快照被错误当故障（已修复，待根审部署）**。`src/analysis_core/orderflow.py` 原判定 `b.observed_at <= a.observed_at`，但 API 的 `ts` 是交易所生成快照时间而非每次本地请求时间。现场 MSFU 两次间隔约 1 秒返回相同 ts=1790405319751、seq=179869140822、u=381179 和同盘口哈希；旧实现必然拒绝。现在允许同快照重复，披露 `book_sample_count/distinct_book_samples/repeated_book_samples`；严格拒绝时间/seq/u 倒退、同 seq/u 对应不同盘口或不同配对 ID。目标 Ruff 与 tests/test_orderflow.py 全过（23 passed）。TMX 本次三次均递增，历史 TMX 失败原始三快照未留存，不能断言那次必为重复而非倒退。
   官方语义依据：https://bybit-exchange.github.io/docs/v5/market/orderbook （snapshot、ts 为生成时间、seq 越小越早）。
2. **可选聚合币种背景源会因 HTTP 传输异常升级为整个币失败**。`tradingview_sources.py:156` 的 SourceRegistry.enrich 捕获 TradingViewError/OSError/TimeoutError，但 httpx.ConnectError/ReadTimeout 不属于此集合。CoinContextSource 的 web._post 传输错误会直传，造成 TV_EVIDENCE 或额外双仓失败；HTTP 非 200 被包装为 TradingViewError 则正常降级。建议加 `httpx.HTTPError` 并测试取消继续向上抛出。已通知根审。
3. **根目录 `tradingview.py` 是旧流程残留**。BINANCE 名单、browser scanner、common/signals 旧包 import，与当前 analysis_core 无引用关系且不可作为本系统直接运行。应移出活动树保留外部归档；它不证明当前服务仍在使用旧扫描器。已通知根审处理。

## 模型实际收到哪些 TradingView 数据

- 初选：TV 全市场发现过程比较公开 CEX 全量 BYBIT USDT perpetual，再按成交量、涨跌两端、量变化、15m/1h技术活跃度形成最多20币的采集池；模型比较全部成功采集池，输出1—10。
- 每币七周期：5m、15m、30m、1h、2h、4h、1d。compact_tradingview 实际传递 time/OHLCV；Recommend.All/MA/Other；RSI及前值；ADX/+DI/-DI；MACD两线；EMA20/EMA50/SMA50；ATR；布林上下轨；VWMA。保留缺失字段、形成中未确认、更新时间/真实报价新鲜度未知、technical page fallback 标识。
- 额外上下文：本合约24h变动/成交量/量变化，周/月等表现、波动率、相对量、OpenInterest、funding_rate（有值才传）；CEX相对名次和母体数量；Coins 聚合背景；Ideas前2个卡片标题/摘要/时间/多空标签/是否同Bybit合约；final阶段 News前3条标题/时间/来源；配置扩展 extra_fields。
- 所有初选和最终输入写入 TV_SELECTION_EVIDENCE / FINAL_SELECTION_EVIDENCE 以及各 MODEL_INPUT 事件。双仓走 DirectionModel，亦用相同 compact_tradingview。代码能够确认 TV 数据已进模型输入；不能从模型理由推导精确内部权重。
- **采集不等于全部喂模型**：原始抓取还包含多数其他振荡器、完整 MA 队列、各类Pivot，当前 compact 白名单会丢弃；原始证据保存在主机事件。Bybit原始抓8周期，模型只保留1m/5m/15m/30m/1h/2h有限已收盘片段，3m/10m只保留主机证据；模型仍有15m/30m/1h/2h最近指标和全历史结构摘要。不能对用户声称全部采集项已被模型逐一使用。

## 逐文件结论与优化

| 文件 | 责任与结果 | 后续建议/限制 |
|---|---|---|
| src/analysis_core/market.py | 只用Bybit instruments验证交易身份；TV发现；所选币Bybit8周期K线/指标/三次盘口/1000逐笔。核心15m/30m/1h完整性及新鲜度有硬校验。未发现Bybit全市场评分调用。 | reachability 方法无生产调用，可清理；3m/10m采集计算后不进模型，可评估移除耗费或真正用于入场背景；2h/短周期没有与核心相同新鲜度门，但时间明示且非主指标。 |
| src/analysis_core/tradingview.py | exact BYBIT perpetual 网页与scanner身份校验；tech404严格同合约overview兜底；七周期字段检查；Ideas/榜单可选降级；原始字段+hash有审计。 | scanner是网页内部端点，不是官方稳定行情API；页面变动/403会失败。每币7次字段请求且统一1秒间隔，20币延迟明显，可在不改字段语义前提用有界字段批量减请求。Ideas卡片按页面前2条而非按新鲜度/合约一致性筛排序，模型可看时间但仍有陈旧背景噪声。 |
| src/analysis_core/tradingview_discovery.py | TV全量 CEX，严格分页完整/身份；Bybit可交易交集；核心bar过旧预排除；多桶最多20池，无旧Bybit评分。 | pool_limit固定20，不等于模型审所有合约。指标字段 volume|15 采集未用于判断；新字段校验较基础。给用户明确这是成本上限和覆盖取舍，可将桶覆盖率/漏选率做离线评估。 |
| src/analysis_core/tradingview_sources.py | 适配器协议与registry可扩展；Coin聚合与News均标弱背景；News超时/取消杀子进程。 | 确定HTTP异常降级缺口见上。Coin `symbol.removesuffix(USDT)+USD` 映射对于倍数合约或同名资产不充分，缺失会明确unavailable；应引入底层资产映射后再扩展。News通过HTML正则，缺专门fixture回归，DOM变动会转unavailable；可增加脱敏固定HTML样本。 |
| src/analysis_core/selection.py | strict模型schema，候选1—10/最终1—3非空、连续rank、币种唯一、必须在证据池，LONG/SHORT+真实LOW允许；输入输出留证一次调用。 | compact字段白名单需与采集清单明确对应；传入新闻/Ideas没有URL/唯一证据ID，模型理由目前无法结构化回指来源。未来新版schema可加 evidence_refs/反证/有效条件，须新版prompt和真实模型验证。 |
| src/analysis_core/model.py | 独立Codex子进程白名单env、禁工具/网络搜索/插件，严格schema，固定prompt profile，输入hash/输出留证，超时取消杀组，不二次调用；Context防错币/错周期。 | 同用户HOME用于已有模型认证，不能表述成OS级独立UID安全边界；当前需求是隔离模型服务进程，已实现进程分离。日志保留有界诊断，禁止把鉴权内容输出到外部。 |
| src/analysis_core/indicators.py | 已收盘 Decimal EMA/RSI/MACD/ATR/布林/价量结构；形成中排除；分母为零记None；没有生成交易方向阈值。 | 多个相关指标不能当独立票数，prompt已说明。2h MACD故意缺省。当前周背景仍由latest_closed内的 background_structure 提供，daily只传末2日不意味着周结构全丢失。 |
| src/analysis_core/orderflow.py | 有界REST证据、逐笔execId去重冲突校验、陈旧逐笔排除、不冒充连续订单流。重复快照缺陷已修。 | 盘口年龄记录但无硬过期门，模型被告知局限；以后可按明确freshness角色区分参考与入场时效。异常诊断建议保留三个时间/seq/u/hash，避免TMX历史无法分辨重复还是倒退。 |
| src/analysis_core/vendor/public.py | 公共API客户端，无交易API/密钥。Market主路只用instruments/recent_candles/orderbook/recent_trades；分页连续性身份验证。 | tickers/completed_candles/historical_completed_candles/completed_5m/open_interest_history/long_short_ratio当前src/tests均无外部调用，是未使用公共能力，非旧评分scanner，可裁剪或显式标可扩展接口。默认max_attempts3，Markets构造覆盖1。 |
| src/analysis_core/vendor/models.py | frozen Candle、币格式、正值/非负、时区/OHLC检查，无交易状态。 | 通用符号/证据别名部分未用，可低优先整理。 |
| src/analysis_core/vendor/__init__.py | 空包文件。 | 无问题。 |
| src/analysis_core/vendor/PROVENANCE.json | 记录公共客户端上游来源/源hash与改编说明。 | hash是上游来源hash，不能当当前文件hash；后续明确字段语义。 |
| src/analysis_core/prompts/tv_selection_v1.md | 比较所有输入，1—2h，TV快照局限，不伪造，初选1—10。 | 未更改；未来提示修改按要求新版本+真实模型验证。 |
| src/analysis_core/prompts/direction_selection_v1.md | TV初判+Bybit精细化1—3、非空、LOW如实、3—5句理由，无订单参数。 | 未更改；建议以后加结构化证据引用以验证TV利用程度，不宜仅要求口头保证。 |
| src/analysis_core/prompts/direction_v15.md | 额外双仓可SKIP，1—2h、弱背景区分、一次调用。 | 未更改；模型等待期间市场证据新鲜度需宿主发布前再验（由根审app部分检查）。 |
| 根 tradingview.py | 非活动旧BINANCE浏览器扫描器残留。 | 应外部归档移走，避免误认可运行入口。 |

## 测试逐文件覆盖与缺口

| 文件 | 已读覆盖/结论 |
|---|---|
| tests/test_orderflow.py | 完整读取并新增重复快照、同ID内容冲突、ts/seq/u倒退、同毫秒不同更新测试。23通过。 |
| tests/test_selection.py | 完整读取；双阶段schema、有效池、重复/rank/空集、压缩白名单。尚缺一条完整七周期+扩展源输入快照回归。 |
| tests/test_indicators.py | 完整读取；已知数值seed、平市、升降、形成中排除、短历史、短窗反转与量价语义。 |
| tests/test_market_direction.py | 完整读取；8周期、10m聚合、原生2h、身份/OHLC、时间、最少已收盘、分页、形成中排除。 |
| tests/test_tradingview_discovery.py | 完整读取；交集、stale预排除、错合约、多桶、Coin聚合、可选失败。缺HTTP transport错误降级（根审处理中）。 |
| tests/test_tradingview.py | 已审全部测试名称和针对真实调用/404/字段扩展/身份/形成中/模型版本关键段；其余测试函数逐行阅读未全部完成。 |
| tests/test_model_process.py | 已审前170行+所有测试名称，核心源码已完整读取；后半取消竞态/分类测试逐行阅读未全部完成。 |

## 建议优先次序

P1：部署重复盘口修复、可选源传输错误降级、发布前最后一次新鲜度校验、清理根旧扫描器。P2：增加源成功率/时延/缺失字段指标；在Bot详情明确TV初判和Bybit最终改判；为模型理由增加证据引用（新版本真实验证）。P3：采集字段/模型使用矩阵、按信息收益裁剪3m/10m及冗余字段；公共适配器与未使用能力整理；资产映射及Ideas新闻新鲜度排序。

没有证据支持“采集越多必定交易更准”。优化应按未来1—2h目标做历史留出验证，比较方向命中、最大不利/有利波动、成本和数据缺失，不改变模型故障失败/不二次调用约束。
