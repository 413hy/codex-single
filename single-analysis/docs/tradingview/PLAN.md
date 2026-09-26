# TradingView 方向参考数据接入设计

日期：2026-09-23。用户指定直接网页抓取；已实施方向参考采集与隔离验证入口。后续用户又明确要求：已有参考数据用于分析小长线方向，不设固定的数据年龄上限。完成状态见RESULT.md。

## 回滚基线

节点：`/root/single-analysis-backups/pre-tradingview-20260923-104618`。91 个源码/测试/说明/部署等文件逐项 SHA256 验证，另有依赖环境、受保护 Bot 配置、实际 systemd 单元和两份 SQLite 在线备份。两库 integrity_check 均为 ok；分别一致，不代表跨库同一事务。未复制模型认证文件或交易系统密钥。

基线验证：ruff 通过；mypy 37 个源文件通过；pytest 173 passed。备份不停止在线服务。常规回滚保留当前 runtime 和消费者去重账本，不恢复旧信号，不补跑。详见节点 ROLLBACK.md；尚未执行实际回滚演练。

可直接使用的回滚提示词：

> 请将 /root/single-analysis 回滚到 /root/single-analysis-backups/pre-tradingview-20260923-104618 的 TradingView 接入前版本。先阅读该节点 ROLLBACK.md，验证 SHA256SUMS 和源码清单，再备份当前版本。仅回滚分析系统代码、提示词及必要的依赖/服务配置，移除本次新增的TRADINGVIEW_ENABLED配置项，保留当前凭据、运行账本、调度/暂停状态和两交易系统的消费去重记录；不要覆盖旧数据库、重放旧信号或手工运行 cycle。若数据库结构发生变化，先做保留新记录的兼容处理。运行 AGENTS.md 要求的 ruff、mypy、pytest，恢复原服务并核实上海固定周期调度。不得输出凭据或模型认证文件。报告恢复范围、验证结果和服务状态。

## 已确认的原系统边界

- scanner、Screening.select、screening_v3.md、TrendScreeningResponse 保持不变：十份有效候选证据全比较，选 1—3 个。
- 接入点为 app.py 取得 Markets.evidence 后、DirectionModel.decide 前，新增独立的结构化参考字段；不回流选币、不增加二次判向、不改变原证据。
- 主周期仍为 15m/30m/1h/2h 一周；1m/3m/5m/10m 近12小时仅辅助。额外高周期只能提供背景，不能改变小长线目标。
- 原生 Bybit K 线和订单流已经存在；同一交易所同一根 K 线经 TradingView 再传来不构成独立证据。
- direction_required 首选 LONG/SHORT、非首选可 SKIP、双仓优先和 60 秒有效信号不变。数据故障失败，不伪造，也不借故重新选币。

## 当前方案：直接抓取网页（用户最新指定）

用户已明确选择网页抓取；此前讨论的付费账户/MCP方案不再是实施前提。采集器访问公开的 Bybit 永续技术分析页，解析页面 symbolInfo 核对真实标的，再读取该页面 JavaScript 实际使用的 scanner.tradingview.com/symbol 请求。不登录、不持有 TradingView 凭据、不使用代理轮换、不绕过验证码/访问拦截。该页面内部请求没有稳定 API 承诺；网页或字段变化会明确失败。

纯 HTML 中指标表只有占位符，故不能把下载 HTML 当作拿到技术数值。研究保存于 runtime/tradingview-research；真实 BTC 实测已读到7个周期每周期79个数值字段。初次全量 URL 超长返回414，现按周期分别请求，每次间隔至少1秒，不设总采集时间的固定截止线，单次网页请求仍有网络超时；无自动重试。

### 采集内容及解释

| 类别 | 本次实际范围 |
| --- | --- |
| 周期 | 5m、15m、30m、1h、2h、4h、1d；主周期沿用原规则，4h/日线仅背景 |
| 量价快照 | time、OHLC、volume；time仅是K线开盘时间 |
| 评级 | Recommend.All/MA/Other三组；区间[-1,1]，不是概率 |
| 动量/趋势 | RSI及前值、Stoch K/D及前值、CCI及前值、ADX与DI、AO及前值、Momentum及前值、MACD/Signal、Stoch RSI、Williams %R、Bull Bear Power、UO |
| 均线/参考 | EMA/SMA 10/20/30/50/100/200、Ichimoku基准线、VWMA、HullMA9 |
| 波动 | ATR、布林上下轨 |
| 枢轴 | Classic/Fibonacci/Camarilla/Woodie/Demark对应页面字段；不假设其基准周期，不称成交密集位 |

不编造3m/10m、不采集私有Pine、不将网页观点当客观事实；目前网页未提供可核实的完整历史指标序列、Footprint或清算热图。原Bybit周内K线与订单流继续完整保留。新增同合约网页字段包括24小时变化、成交活跃度、较长期间表现、波动率和相对成交量；数值保持供应方原字段名与原值，null保留为缺失。`TRADINGVIEW_EXTRA_FIELDS`可追加其他经核实的字段名（数值、文本或结构化值），采集器按网址长度分批请求、逐批核对合约身份后传给模型，没有按字段类别设置封闭名单。 同市场排名来自TradingView网页筛选器，对全部返回的Bybit USDT永续行逐项核对交易所、合约类型和名称，再计算当前币的名次、有效分母与原始指标值；排名仅为方向弱背景，不改变十币选币。

### 时间、数值和故障合同

- 双重核对网页与数据响应的 BYBIT、币名、swap、perpetual身份。跨周期身份变化失败。
- 不以 HTTP Date、Age、周期K线距当前时长或采集后经过的秒数设置参考数据年龄上限。保留周期time对齐与未来时间校验；HTTP时间和fetched_at均不等于最新成交时间。原始响应的Date/Age可作为审计元数据保存。
- 数据源未提供可验证的quote timestamp，因此始终标记quote_freshness_verified=false。指标全部closed_bar_confirmed=false，不能用于收盘确认；即使字段带[1]也不自行认定时间身份。
- 网页OHLCV、评级与其他指标逐字段标记可用/缺失；部分字段null不清空其他参考。身份不符、异常数值、核心网页HTTP失败或整份参考无可用数据时中止该币方向分析。供应方update_mode只作为参考标签传给模型，不因延迟标签直接丢弃数据。
- 每次原始字段、请求URL、HTTP时间、响应哈希和页面哈希落入原方向证据。模型接收精简来源证明、已核实合约身份、结构化值和局限；原始大响应只保留审计，不重复传给模型。
- 同源价格和相关指标不多次计票；网页Buy/Sell不直接决定LONG/SHORT。既有首选必判、Schema、双仓优先和60秒发布规则不变。
- 原Bybit方向证据不再因采集后的90秒上限、最近K线或末尾未完成柱的固定45秒新鲜度门槛而被拒绝；未完成柱仍只能在序列末尾，连续性、完整一周已收盘历史、身份、数值和完成状态仍须有效。对冲交易所快照20秒与已发布信号60秒有效期是独立合同。

### 用户提供脚本的定位

根目录`tradingview.py`以浏览器截获全市场scanner排行榜，并读取社区Ideas。它面向币种发现，含BINANCE/CRYPTO范围及用户观点；当前系统的选币条件不变，因此排行榜不进入筛选。现行采集器从已选中的BYBIT永续币技术分析页及页面数据请求获取七周期数值，还读取同标的Ideas页面。页面会混排其他交易所和现货观点，程序逐条保留原始交易所/合约标签、作者、页面时间与链接；只作弱背景，不当作当前Bybit合约行情事实。脚本只供采集思路参考，没有照搬其币种映射。

## 代码与启用

采集器为src/analysis_core/tradingview.py；Markets仅在TRADINGVIEW_ENABLED=true时采集。AnalysisApp把开关和可选的TRADINGVIEW_EXTRA_FIELDS传入方向行情层；筛选层未引用TradingView。DirectionModel在含该参考时使用direction_v14.md，否则沿用v11。默认关闭便于明确回滚，不静默降级；启用后核心技术快照抓取失败会走既有失败记录路径，可选Ideas页面失败则明示不可用。

tradingview_validation.py是隔离的模型验证服务入口，使用原DirectionModel服务代码调用模型；只写独立validation.db/context/result，不打开生产账本、不创建SignalBus、不推送Bot、不发布信号。必须新建目录，避免重跑已有模型验证。生产不得以cycle命令验证本接入。

## 真实验证与版本证据

direction_v11完整保留；v12增加网页参考合同，版本/哈希/源码变动范围见manifest.json。真实模型验证、生产开关和测试最终状态见RESULT.md。一次真实样本只能证明通路和合同初步成立，不证明方向收益或所有币种覆盖。

初次隔离验证在写JSON时遇到原有Decimal数值序列化错误，未产生MODEL_INPUT、未调用模型、未发布信号；已改用项目encode统一序列化并新增回归测试。修复后新建隔离目录采集新的行情验证，保留失败目录作证据。

## 官方参考与网页来源

- 实际技术分析网页：https://www.tradingview.com/symbols/BTCUSDT.P/technicals/?exchange=BYBIT
- 技术评级定义：https://www.tradingview.com/support/solutions/43000614331-technical-ratings/
- 盘中及高周期重绘：https://www.tradingview.com/pine-script-docs/concepts/repainting/
- 网站自动采集/非展示用途限制：https://www.tradingview.com/policies/
- 研究中发现的官方MCP（未采用）：https://www.tradingview.com/mcp/docs

网页可访问不代表官方支持自动采集。本方案按用户指定技术路线实现；401/403/429/验证码不绕过，不能承诺网页源长期稳定。
