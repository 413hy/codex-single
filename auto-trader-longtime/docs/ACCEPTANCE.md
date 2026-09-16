# 验收记录（2026-09-09）

独立Bybit Demo账户和Bot已配置，并完成一笔真实Demo开仓至自然TP退出。旧项目保持独立运行。本轮按用户授权将方向模型由Astra/low改为`gpt-5.6-terra / medium`，继续验证完整候选周期。

## 自动化验证

74项pytest通过，覆盖已实现的业务、白盒故障注入、HTTP/SQLite灰盒串联、真实子进程和CLI黑盒路径。报告：`runtime/review/junit.xml`。

- LONG/SHORT、单向/双向仓位索引、5x上限和2x/3x合约、约10U规模、20U余额保留、24h可达TP逐级选择。
- Schema非法JSON、额外风险字段、币种冲突、缺失与陈旧行情拒绝；无confidence过滤。
- 开仓响应丢失按原ID核对，不重复提交；尚未查清的意图持久保留。
- TP/SL只自动重试一次，取消/拒单替换共享预算，重启不刷新次数；按钮单次执行且检查仓位身份、订单状态。
- 部分成交不补仓，部分退出不伪造整仓结算，长期持仓早期退出证据归档。
- 真实closedPnl不二次扣费，平仓只撤本生命周期的剩余单，结算与通知outbox同事务提交。
- 暂停竞态、账户与合约状态异常、周期幂等、进程锁、数据库事务失败、错误去重和Bot回调白名单。
- 真正的测试子进程验证模型参数、无交易密钥环境、禁用工具、严格Schema、超时终止进程组、不自动重试模型。
- 上游容量故障同轮只发一个事件，剩余候选停止调用；不把行情文本中的“capacity”误判为服务错误。

Ruff、mypy（check_untyped_defs，非strict）、compileall、pip check及systemd unit校验另行执行。Python 3.11.2、Codex CLI 0.153.4，依赖冻结见`requirements.lock.txt`。

## 独立账户与真实订单

- 私有端点固定`https://api-demo.bybit.com`，账户全仓`REGULAR_MARGIN`。
- 与旧系统账户UID、Bot ID分别核对，确认使用独立凭据。实际密钥仅在本项目`.env`（0600）。
- 独立Bot：`@yhetest_bot`；真实开仓、平仓通知发送成功，用户真实点击验收重试按钮，回调已完成且故障状态RESOLVED。

|项目|真实结果|
|---|---|
|交易ID|f07ee24e9a51a255132ba50c|
|币种/方向|RAYDIUMUSDT SHORT|
|数量/杠杆|35.7 / 5x|
|保证金|9.996 USDT|
|实际入场|1.4|
|TP/SL|1.3839 / 1.6208|
|实际退出|1.3839，交易所自然TP成交|
|持仓时间|216.201秒|
|交易手续费|0.03737005 USDT|
|实际净PnL|+0.53739995 USDT|

市价成交、Reduce-Only GTC TP、Reduce-Only条件市价SL均有交易所真实订单ID和回执。TP结束后剩余SL已取消。复查余额201.00101311 USDT，仓位0、挂单0。未通过提前平仓、改TP/SL制造验收结果。利润与目标的小幅差异来自保守费用/滑点估计。

证据：`runtime/trader.db`、`runtime/live-preflight.json`。验收脚本的选币cycle曾被任务中断标记INTERRUPTED，但真实已开仓交易随后由独立观察任务完成结算；二者分别记录，不把中断的周期改写成SUCCESS。

## 模型与选币验证

已复用`/opt/bybit-signal`原选币和公开行情模块；适配接口、来源及取舍见`REUSE.md`。

前一版Astra真实批量请求出现`ERROR: Selected model is at capacity`，诊断保留在模型事件中。这是模型上游容量故障。当前固定Terra/medium，取消并发模型请求；不静默回退、循环重试或加额外方向过滤。

`scripts/live_full_cycle.py`使用真实公开行情、真实方向模型、独立Demo账户真实余额/仓位，遍历当轮全部合格候选；私有写入关闭，不为批量测试额外制造几十笔仓位。原有失败记录继续保留，当前周期结果须按cycle_id区分。

完整周期结果：`runtime/live-full-cycle/result.json`及同目录SQLite。仅真实通过选币、模型、24h TP可达性、资金与规模检查的币标记`DRY_RUN_ELIGIBLE`；这不是成交成功状态。

## 服务与恢复

`bybit-longtime.service`已安装；本轮常驻服务使用`.env`的`TRADING_ENABLED=false`并暂停新开仓，执行真实账户启动恢复、30秒观察与Bot收发。未开启自动交易或开机启动。

启动成功后进行真实`systemctl restart`，同一自然周期仅有一行cycle记录，新增交易0，原已结算交易保持CLOSED。证据：`runtime/service-restart-acceptance.json`及journal。

`ProtectSystem=strict`、`PrivateTmp`、`NoNewPrivileges`环境下另运行真实公开行情和模型smoke，结果见`runtime/public-smoke/result.json`及`bybit-longtime-sandbox-smoke`日志。

测试覆盖的正常和故障路径通过，不代表已发生过所有真实故障。真实自然SL退出、长期资金费和数日连续运行尚未观测；SL订单存在性已在Demo确认，SL执行与结算分支有模拟测试覆盖。当前状态和完整周期结果以本文件后续最终结果为准。

## 本轮最终结果

当前完整周期`live-full:1788942250`为SUCCESS，耗时331.519秒。Terra/medium真实调用46次，失败0；42个候选通过完整只读交易条件，3个因24h TP不可达正常跳过，1个因合约规模精度正常跳过，本轮新故障0。

最终结构化汇总：`runtime/live-acceptance-summary.json`。`runtime/live-full-cycle/result.json`内的incidents字段由此前脚本版本输出，包含历史Astra故障，不能当成本轮新增故障；原始历史保留。后续脚本已分别输出new_incidents和incident_history。

systemd限制下的单次真实smoke也通过（754合约、46候选、288根K线、Terra合法LONG、exit=0）。最后一次账户复查仍为零仓位、零挂单。当前服务active、自动重启计数0、开机启动disabled；只读且暂停新开仓，Bot和30秒监控继续运行。代码与上述上线前检查已完成，可按README启用Demo自动交易。

## 用户授权上线（2026-09-09 16:37，Asia/Shanghai）

用户已明确授权Prompt、Skills和工具规范复核通过后直接启动开仓。已完成模型配置隔离检查，Prompt明确未收盘K线和多周期差异的解释；方向说明只给定性依据，避免小数价格单位歧义。更新后74项回归通过，真实Terra/medium受限环境调用通过（最终smoke：754合约、47候选、288根K线、合法LONG）。

已将本项目.env的TRADING_ENABLED设为true，清除entries_paused，并启用和启动bybit-longtime.service。unit为enabled/active。之前的只读验收状态为历史记录，当前执行状态以本节及实时服务为准。当前时间桶已经处理，不删除周期记录强制重复分析；等待16:40自然新周期。

开发约定见根目录AGENTS.md，策略/接口/运行规范及验证映射见OPERATING_RULES.md。

### 首轮自动开仓核对（16:40:40）

自然周期1490786已开始，扫描47个候选。已核对首两笔真实Demo新仓：IOSTUSDT LONG（保证金9.98186736U）和USELESSUSDT SHORT（保证金9.955944U），均5x。

交易所读回确认两笔TP都是Reduce-Only Limit/New，SL都是Reduce-Only、Close-On-Trigger Market/Untriggered；真实仓位存在。重要通知已发送，快照时outbox全部SENT、活动故障0。后续候选由常驻服务继续处理，资金限制仍逐笔检查。

证据：runtime/launch-verification.json；最终Prompt SHA256为adb905c1339fd910fd79e55469759c990a2ec32f324f185a1b52c2c7419012f1。此记录是上线检查时间点快照，仓位随后可能自然变化。


## 选币链路纠正与暂停（2026-09-09）

用户明确要求参考信号系统筛选0—3个币。此前仅复用scanner，遗漏六币模型复核，导致17:20一轮连续开仓；已持久化暂停新开仓，保留所有生命周期和订单记录。当前策略以此节和更新后的OPERATING_RULES为准，早先“全部候选逐币交易”的验收仅为历史。

已接入独立的前10候选采集→6份有效证据→筛选模型0—3入选→入选币独立方向判断流程。原始公开采集、证据构建、筛选合同及质量校验来自/opt/bybit-signal，来源哈希保存在PROVENANCE.json；没有旧项目运行时导入、数据库依赖或修改旧服务。

95项pytest通过，Ruff/mypy通过。覆盖0/1/2/3入选、证据完整度排序、超量/错误币种/错误批次/异常排名拒绝、高执行风险降级、周期只处理入选币、不递补、暂停不调用模型、重启幂等，以及复用的公开行情/证据测试和原执行回归。

真实只读验证：runtime/screening-review/20260909T093142Z/result.json及同目录review.db。扫描39候选（排除已有14币），前10采集、6币单次Terra/medium筛选成功；最终入选CHIPUSDT、ARBUSDT共2币，两次独立方向调用成功。筛选Prompt及原始输入输出、最终结果均保存到验证库。私有写入关闭，无新增验收订单。

重载前交易所核对：14个仓位、28张原TP/SL，身份、数量、价格与Reduce-Only均通过检查，无在途开仓意图、无运行中周期。新开仓保持暂停，已有仓位继续等待原TP/SL自然退出。


## 小长线方向、异常修复与恢复自动交易（2026-09-09 17:55）

用户授权检查通过后直接恢复。已完成Terra/medium两轮工程审阅（runtime/model-alignment/review.json、final-review.json），需求与模型合同同步至docs/MODEL_CONTRACT.md及direction-v2-small-longline-20260909。方向输入主周期30m/1h/2h、辅助1m/3m/5m/10m/15m；2h原生120分钟、10m按UTC由5m聚合。有限值、OHLCV、身份、对齐、完成状态和新鲜度校验覆盖方向和TP可达性；筛选引用在规范化前拒绝非法ID。没有新增指标阈值或模型工具权限。

112项pytest通过，Ruff/mypy通过。新增测试涵盖原生2h请求、10m聚合与形成中状态、八周期输入、畸形行情拒绝、合法零成交量、历史形成中状态拒绝、非法引用拒绝、创建时间过滤下的延迟结算恢复、异常日志去重、底部控制键盘路由与白名单。此前真实模型验证之外，本次runtime/screening-review/20260909T095142Z完整只读流程扫描46候选，模型筛选ARBUSDT/CHIPUSDT两币，两次方向调用合法且解释主周期与短线关系。追加校验后的新鲜行情读取也成功，未为验收制造订单。

CASHCATUSDT异常根因：原结算游标窗口缩短后漏查较早创建、较晚成交的止盈单。改为按生命周期时间范围重取历史并按ID归并，不伪造记录、不改变原交易。真实账本已由常驻监控正常结算为CLOSED，净PnL=0.5249215U，故障RESOLVED；原Telegram告警已更新为“已解决”，移除失效重试按钮。

底部键盘、9项Bot命令已真实发送/读回成功，恢复通知SENT。键盘包含持仓、状态、最近交易、最近分析、暂停/恢复、异常处理、策略说明、重发失败通知；异常说明原因、影响和处理，内部长ID替换为币种及短编号。回调白名单和单次重试边界保留。

已备份账本并正常停止/启动本项目service；当前enabled/active/running，NRestarts=0，entries_paused=false，TRADING_ENABLED=true。部署核对7个仓位与14张Reduce-Only保护单正常，无在途开仓、无运行中周期。恢复后活动故障0，全部outbox已SENT，监控心跳正常。当前自然桶此前已记录PAUSED，保留周期幂等，下一自然周期18:00执行；不删除记录强制重跑。

部署快照：runtime/model-alignment/deployment.json。此前“保持暂停”的记录仅为历史，当前已按用户授权恢复自动开仓。真实调用及测试通过不代表模型服务永不繁忙或方向必然盈利。


## 四按钮可收起键盘（2026-09-09 17:59）

按用户要求主导航精简为两行四键：当前持仓、运行状态、最近交易、暂停/恢复开仓。统一ReplyKeyboardMarkup参数resize_keyboard=true、is_persistent=false、one_time_keyboard=false。低频功能保留命令入口。发送入口统一普通消息和旧队列键盘参数，阻止键盘移除请求；InlineKeyboard保持消息局部作用，编辑操作只接受InlineKeyboard；重启不发送清除或额外重置键盘消息。

114项pytest、Ruff、mypy通过，新增验证普通导航/暂停恢复/InlineKeyboard/消息编辑/重新实例化/旧队列格式规范化/阻止移除请求。部署前7仓14保护单，无在途开仓、无运行周期。备份后正常停止/启动本项目服务，自动交易保持开启，active/running，活动故障0，监控正常。

新版菜单已真实投递SENT，message_id=3585；参数与部署快照见runtime/compact-keyboard-deployment.json。真实API投递已确认；手机端收起后的图标与显示由Telegram客户端控制，本环境没有手机UI，未冒称完成客户端点击验收。官方语义见https://core.telegram.org/bots/api#replykeyboardmarkup。

## 小长线指标整合（2026-09-09 18:36）

按用户授权与同一个Terra模型讨论（runtime/indicator-review/review.json），新增宿主Decimal已收盘EMA20/50、RSI14、MACD12/26/9、ATR14、布林20/2、成交量比、20柱区间位置，八周期各提供最近三次指标快照。按最新补充，小长线分组为10m/15m/30m/1h/2h，30m/1h/2h核心；1m/3m/5m仅辅助。模型输入、方向Prompt、Bot策略说明和运行规范同步。模型工具隔离、资金与持仓管理规则不变；未接入足迹图、热力图或大单追踪，明确披露缺失。

118项pytest、Ruff、mypy通过。新增验证EMA/Wilder已知种子、横盘/零量分母、上涨/下跌RSI、已知布林/ATR/量比、形成中尖峰不影响指标、八周期实际分组及指标收盘时间一致。完整真实只读流程runtime/screening-review/20260909T103422Z扫描44候选，排除7个持仓；筛选选NEARUSDT一个，方向LONG并引用主周期EMA/MACD/放量、10m/15m关系及短线整理。没有通过验收脚本下单。真实MODEL_INPUT的Prompt SHA256与安装文件一致：fcc7ff0a57af42e2e566c8fbc88075a97ecf059d4a680071c96c0ef0e5b36a74。

部署前7个真实仓位、14张Reduce-Only保护单，每仓2张；无运行中周期、在途开仓意图或订单意图。正常停止本项目service、SQLite备份、安装验证过的Prompt并启动，保留全部账本。部署证据runtime/indicator-review/predeploy.json及deployment.json。恢复后active/running、enabled、NRestarts=0，STARTUP_EXCHANGE_STATE已记录；自动开仓保持开启、entries_paused=false，活动故障0，60项outbox均SENT。下一自然周期按原幂等规则执行，未强制重跑当前桶。真实模型调用验证为一个实时样本，不代表方向准确率或收益保证。

## 20分钟周期及短历史窗口修复（2026-09-09 20:30）

核查自然周期1490792—1490797均按:00/:20/:40触发，没有漏调度。1490797在20:20触发，68.8秒后PARTIAL_ERROR；唯一入选PONSUSDT因2h历史仅111根，被误当作未满足请求120根（旧要求至少119根）而拒绝。随后manual:033312a64ef3be0a442f30ca为用户按钮重试，不是自动调度漂移。根因是指标扩窗时混淆期望采集长度和算法最小样本量。

修正方向窗口：仍请求120根（5m250根），但以52根已收盘数据为最低要求，保证最近三次快照各有至少50根用于EMA50；身份、OHLCV、连续性、UTC边界、时间新鲜度、形成中状态校验继续执行。TP可达性24h窗口保持原要求，不放宽。真实PONS数据现有110根已收盘2h，八周期采集及真实Terra方向调用成功（runtime/window-fix/evidence.json、result.json、review.db）。119项测试、Ruff和mypy通过；新增111根2h短历史回归。该修复不更改Prompt、风险参数或指标公式。

部署前3仓6张Reduce-Only订单，无运行周期和开仓意图；正常停止本项目service、备份账本、启动成功active/running，NRestarts=0。PONS异常在新鲜数据及模型调用验证后标记RESOLVED，审计事件MARKET_WINDOW_FIX_VERIFIED保留，不伪造失败周期成功、不删除记录或强制重跑。本次只读验证未提交交易。

## 按实际交易需求精简方向数据（2026-09-09 20:38）

用户再次纠正不要为指标抓过长数据。统计27笔已结束交易平均31.8分钟、中位20.3分钟，23笔在一小时内结束；同时披露3笔未结束约150—190分钟。证据runtime/compact-evidence/holding-statistics.json。先前提议2h12根的中间版本未部署；最终方向输入为2h4、1h6、30m8、15m12、10m12、5m12、3m12、1m20，总计最多86根，较1090根显著精简。5m宿主取26根仅为10m聚合，不把多余历史塞入5m方向输入。TP24h独立程序窗口未变。

与同一Terra模型讨论后，统一领域操作说明、direction-v5 Prompt、Markets入口、Decimal指标模块、只读验证脚本及运行规范。优先实际样本窗口的recent_structure，不再EMA50；不足固定周期指标置null，不设整币指标完整性门槛，不扩窗凑数。保留真实数据有效性验证，模型工具/Skills隔离及固定交易风控。详细当前合同见MODEL_CONTRACT.md，覆盖之前扩窗要求。

121项pytest、Ruff、mypy通过。新验证包括少量柱依然生成合法输入、指标按实际样本可用、短结构统计精确值、5m与10m输入裁剪、2h只4根。完整真实只读链路runtime/screening-review/20260909T123603Z扫描47候选、排除3持仓，筛选LITUSDT/VVVUSDT；方向SHORT/LONG均引用近期结构并说明2h有限样本，两次MODEL_INPUT哈希与部署Prompt一致。未为验收提交订单。

部署前3仓6张Reduce-Only保护单，无运行周期/开仓意图；正常停止本项目service、备份账本后启动，active/running、NRestarts=0。部署证据runtime/compact-evidence/deployment.json；未改变20分钟调度、不删除历史或强制重跑，也不增加持仓期限。

## 按最新要求回看三天（2026-09-09 20:43）

用户明确要求数据再长一些，最近三天并按用途获取。当前v6覆盖此前v5极短窗口：30m145/1h73/2h37根（约三天加当前形成柱），15m97/10m145根（约一天），1m60/3m60/5m72根（约1/3/6小时）；5m采290根用于10m聚合，方向5m只用最后72根。无指标完备性开仓门槛，不重新加入EMA50。三天回看是背景，Prompt要求结合最新已完成柱判断，不变更持仓期限和风控。

Terra工程讨论证据runtime/three-day-context/review.json。保留用户要求10m/15m参与小长线，不采纳将其降为短线辅助的建议。当前领域说明、数据入口、指标、验证脚本和文档已对齐。121项pytest、Ruff、mypy通过，新增/更新验证核心周期首末开盘时间跨三天、5m裁剪与10m聚合。真实完整只读流程runtime/screening-review/20260909T124104Z扫描45候选，筛选PONSUSDT/LITUSDT，两次方向LONG，输出引用真实提供的均线/MACD、主方向及短线回撤；输入Prompt哈希与部署一致。只读验证未提交交易。

常驻20:40自然周期已SUCCESS，无活动异常。部署前5仓10张保护单逐笔核对方向/数量/价格正确，无运行周期或开仓意图。备份后正常停止/启动本项目service，active/running、NRestarts=0。快照runtime/three-day-context/predeploy.json及deployment.json。保留全部账本、周期、原始保护单与自动开仓状态。

## 由实际Terra审查数据需求并对齐（2026-09-09 20:54）

用户要求自主理解交易目标、由模型判断所需数据，不再被动照搬根数。向实际Terra提供真实输入、持仓统计、Markets/指标/Prompt实现，先需求评审再最终代码复审；证据runtime/direction-needs-review/review.json、final-review.json及review.db。模型认为现有分层历史已足够，关键缺口是全窗背景与近期变化混淆，而非增加指标或微观采集。

落实：原全窗统计明确命名background_structure；新增每周期两层recent_windows，标注实际/期望样本数、是否完整、起止时间、区间、变化与相对量能。2h MACD不作为辅助值，不为其种子稳定性扩大到七天。保留用户要求1m/3m辅助，不盲目新增盘口、单点OI、足迹/热力图。模型建议中删除1m/3m、放松中间缺口等不符合需求或数据有效性的部分不采纳。

方向输入采用columns-v1无损列式编码，保留全部OHLCV/成交额/时间/完成状态，恒定币种/周期/来源提升到每周期元数据；不修改账本原始行情。实际两币完整上下文字符量229342→121633、224765→119099（含JSON空格的同口径统计），约减少47%，无额外网络采集。领域操作说明固定direction-v7-layered-evidence Prompt，每次请求注入，不依赖模型记住对话。原子交易职责、无工具/通用Skills隔离、严格Schema及超时策略保持。验证脚本增加数据窗口与编码前后量审计。

124项pytest、Ruff、mypy通过，新增背景上涨而近窗下跌、短样本标记、2h MACD省略、列式输入原样还原/身份/未知字段拒绝。第一次完整只读验证筛选成功后，方向请求遭上游额度暂不可用，记录原失败，不伪称通过。用户要求继续后，使用先前筛选币的新鲜行情进行两次独立只读验证：runtime/direction-needs-review/20260909T125209Z（CASHCATUSDT SHORT）及20260909T125238Z（PONSUSDT SHORT）；两次调用合法，分别解释主结构和近期修复/短线反弹，均未引用2h MACD。已核对两次真实输入具备列式数据、recent_windows和省略的2h MACD，Prompt SHA256与最终安装一致。只读验证未下单，工程复审意见不等于收益或模型永不故障保证。

部署前3仓6张保护单逐笔校验通过，无运行周期、无开仓意图、entries_paused=false；正常停止本项目service、SQLite备份后启动active/running，NRestarts=0。部署记录runtime/direction-needs-review/deployment.json及predeploy.json。不删除周期、订单或账本，不强制重跑当前自然周期，不更改原TP/SL。

## 全系统逐文件审核与部署（2026-09-09 22:30）

全文审核73个原项目文件，并交由实际gpt-5.6-terra/medium逐文件出具意见；修复后再次模型复审及交叉确认。逐文件处理见[FILE_AUDIT.md](FILE_AUDIT.md)，原始意见、争议核实、最终确认及哈希留在runtime/full-audit/20260909T140405Z。新增tests/test_audit_regressions.py另经最终模型复审。

修复自然调度遇手动周期锁重叠可能漏桶、开仓准备跨截止时间、正常SKIP误报、HTTP失败误判确定拒单、保护失败诊断缺失、监控旧仓位快照、外部保护识别、异常状态更新与通知/回调幂等；修复筛选Prompt引用不存在文件及人工跟进角色、证据空周期计数和成交额窗口标签。验收脚本补独立运行锁、完整筛选路径及只完成本次周期；未启用流组件补时区输入验证。未增加主动持仓管理、数据完备性门槛或新行情服务。

最终Ruff通过，mypy 38源文件通过，pytest 151项通过（12.62秒），pip check及systemd-analyze verify通过。模拟不确定响应和部分退出等回归不等同于真实故障注入。实际只读完整链路runtime/screening-review/20260909T142538788770Z：筛选NEARUSDT/RAYDIUMUSDT/ARBUSDT，三次方向完成；实际输入筛选及方向Prompt SHA256均与部署一致，未下单。筛选为screening-v2-inline-contract-20260909，方向仍direction-v7-layered-evidence-20260909。

部署前4仓8张保护单全部核对通过，无运行周期、INTENT订单或开仓意图、无活动异常，99条outbox均SENT；SQLite quick_check=ok。在线备份runtime/backups/trader-pre-file-audit-20260909T143003Z.db后，仅正常停止/启动bybit-longtime.service。22:30:08 ready，active/running、enabled、NRestarts=0，trading_enabled=true、entries_paused=false。部署后同4仓8保护单、无活动异常/不确定订单，最近三个自然周期SUCCESS，重启未重跑当前桶。详见predeploy.json/postdeploy.json。保留全部账本及原TP/SL。新调度跨桶行为已做回归，本次部署后尚未到下一个自然20分钟边界，不冒称已观察该新周期。

## 用户授权固定TP 4U / SL 2U（2026-09-10 10:23，Asia/Shanghai）

用户根据本项目77笔历史入场敏感性分析，明确选择先运行4U/2U、之后复盘，持仓时间不作为优化标准。新仓固定净止盈目标4U、净止损预算2U；保留约10U保证金、最高5x、24h TP可达性及轻量tick缓冲，不可达或预计舍入净利润超过4.05U则跳过，不再下调TP档位。不限持仓时间，不改变模型Prompt。

新开仓意图details写入sl_loss=2及risk_policy=fixed-tp4-sl2-20260910，实际成交恢复使用已持久化预算；无该字段的旧意图沿用历史8U，已OPEN仓位原TP/SL及重试参数不变。同步README、运行规则和Bot策略说明。

157项pytest、Ruff、mypy（38源文件）全部通过；新增固定4U目标、不降档、粗tick拒绝，以及旧/新/指定预算意图恢复与重启后保护单不变回归。真实只读Demo私有边界及公开RAYDIUM/PONS双向报价、精度、4U/2U计算和24h可达性验证通过；LONG可达、SHORT不可达正常跳过。此前BTC smoke因最小数量不能满足约10U保证金而停止，保留正常规模限制，没有为验收放宽参数或制造订单。

部署前3仓6张保护单逐笔核对正确，无运行周期、交易或订单意图。仅systemd正常停止本项目，停止后再次确认无在途状态，SQLite在线backup及quick_check通过，再启动服务active/running、enabled、NRestarts=0。trading_enabled=true、entries_paused=false。重启后原3仓6单的ID、价格、数量与方向全部一致。

部署前已有两项活动异常：MODEL_SERVICE额度暂不可用，以及某次ENTRY返回110126要求签署合约协议。保留真实告警，不伪造解决；后续自动周期仍须模型与交易所条件通过。本次未观测新4U/2U仓位实际成交或自然退出，不将只读验证称为真实成交验收。

证据：runtime/tpsl-4-2-deployment/{predeploy.json,postdeploy.json,deployment.json,predeploy-ledger.db}；修改前源文件备份在同目录original。分析基线在runtime/tpsl-analysis/report.md。未删除cycle、order或历史交易记录，其他交易服务未改动。

## 用户授权固定TP 1.5U / SL 1U（2026-09-10 11:15:53，Asia/Shanghai）

按用户要求，新仓净止盈目标改为1.5U、净止损预算1U，先运行后复盘。risk_policy=fixed-tp1p5-sl1-20260910，意图持久化sl_loss=1。旧意图沿用持久化预算，无字段的历史意图仍按8U恢复；已开仓原保护价格不变。保留24h可达性检查，TP舍入允许最多0.05U偏差（1.55U），不可达不降档；保证金、杠杆、筛选、Prompt、不限时持仓与重试规则保持。

Ruff、mypy通过，158项pytest通过，包括历史8U、前版2U、新版1U预算恢复和重启保护不变。Demo只读仓单核对及RAYDIUM/PONS双向实时价格、合约精度、1.5U/1U计算和可达性验证通过，无额外验收交易。

部署前2仓4张保护单，无运行周期、开仓或订单意图；systemd正常停止本服务，再核对在途状态并备份SQLite、quick_check通过；11:15:53启动active/running、enabled、NRestarts=0。自动开仓开启且未暂停。前后原XTZ（旧0.5/8）、SIREN（前版4/2）仓位及4张保护单ID/价格/数量完全一致。已有合约协议110126告警保留；未新增处理或伪称已解决。新1.5/1配置实际成交及自然退出待后续周期观察。

切换与复盘证据：runtime/tpsl-1p5-1-deployment/{deployment.json,predeploy.json,postdeploy.json,predeploy-ledger.db}；原文件备份同目录original。未删除历史记录、未改其他服务。

## 用户授权10U、1x、TP 0.5U / SL 4U（2026-09-10 18:44:15，Asia/Shanghai）

新仓改为约10U保证金、1倍杠杆（约10U名义金额），固定净止盈目标0.5U、净止损预算4U。risk_policy=fixed-tp0p5-sl4-1x-20260910，意图持久化sl_loss=4。保留24h可达性、0.05U的TP舍入容差、20U资金预留、不限时持仓及原重试规则。已有仓位与旧意图继续使用原始参数；未改Prompt、账户持仓模式或现有持仓杠杆。

158项pytest、Ruff、mypy通过。更新1x规模/余额费用边界与新预算验证，保留历史8U、1U、2U恢复回归。真实只读Demo仓单核对及RAYDIUM/PONS双向行情、数量精度、1x规模、0.5/4价格计算与24h可达性验证通过，无额外测试交易。

部署前6仓11张保护单；REZ旧仓缺SL，原请求lt-sl-b2e4e64548ca0fb7b6b9cac9为SUBMITTING、attempts=2，按原ID查交易所返回无订单，已有SL告警。没有重试、刷新预算或伪造终态，原未知记录与告警保留；另保留历史110126协议告警。无运行周期、无开仓意图。systemd正常停止后再次核对状态并SQLite backup/quick_check，再启动active/running、enabled、NRestarts=0，自动交易开启且未暂停。前后6仓原保护价格、订单ID、11单及REZ缺失状态、未确定SL记录均一致。此部署没有修复旧REZ止损缺失，不将其报告为全部保护正常。

证据runtime/tpsl-0p5-4-1x-deployment/{deployment.json,predeploy.json,postdeploy.json,preserved-uncertain-order.json,predeploy-ledger.db}。新配置真实开仓及自然退出待后续周期观察。历史分析中约50U名义金额的收益不能直接套用当前约10U名义金额。

## REZ止损拒单根因修复、原目标市价退出与最近异常按钮（2026-09-10 18:51:56）

REZ真实成交08:01:44.760 UTC，SL意图08:05:32.136建立，间隔约227秒。原SL 0.003475；两次POST分别返回110093，返回中的LastPrice为0.003392、0.003415，均已低于原止损，故不是成功挂单后消失，而是从未创建成功。events 895/896保留原始拒单。旧程序开仓需等待逐笔execution数量齐全，且拒单未写终态，监控覆盖具体原因为笼统缺失。原代码有等待历史的阻塞路径，但当时未落地每次核对响应，不能精确区分227秒内订单可见性、逐笔成交延迟及调度各占多少。

修复：终态订单存在可验证身份/数量/成交额/均价/USDT cumFeeDetail与有效时间时直接使用交易所累计真实成交证据生成原目标保护，不再先调用逐笔历史；缺累计证据时仍用原核对路径，不估算填充。累计数据与订单快照落库，createdTime仅作标注的保守历史起点，逐笔记录后续只读补齐并核对手续费/准确成交时间，不调整已定TP/SL。新增缺证据诊断。明确拒单状态Rejected及原始回执持久化，监控保留具体告警；未知响应保留不确定状态。

用户明确追加：提交或重试时行情已到达/越过原止损或止盈，使用Reduce-Only/Close-On-Trigger IOC市价退出。实现以LastPrice判断多空两方向，退出前再次确认仓位身份和剩余数量；提交意图先落库，不确定请求载荷和ID保持，重复调用核对原单，自动次数上限不变；已有有效保护单继续交由交易所执行，未引入后台无限补挂或移动价格。实际成交及净损益不保证等于原目标。

新增底部“⚠️ 最近异常”第五键，路由/alerts，显示最近5项OPEN/RUNNING及原重试按钮，已解决不显示，无异常“✅ 最近无异常。”。保留权限、回调幂等、可收起键盘及旧入口。用户授权的键盘更新outbox已SENT，message_id=3800；API投递已确认，不冒称手机UI点击验收。

Ruff、mypy通过，166项pytest通过。新增累计订单不等待逐笔历史、证据补齐不改保护、110093持久化/监控保留、多空TP/SL越价Reduce-Only剩余量、市价不确定请求载荷一致、异常键空态及已解决排除回归。灰盒行情fixture补真实lastPrice字段。真实Demo读取验证REZ累计费用与成交订单、新LastPrice接口；未制造故障或额外开仓测试。预部署沿用行情smoke的PONS规模不满足10U容差被正常拒绝，没有放宽规模条件，仓单检查另行完成。

处理时REZ已由原TP自然退出（closed_at 1789037341.518，净PnL=1.53928332U），不再补单。查询原SL ID为空且两条拒单回执明确后，将历史SUBMITTING按原事件审计纠正Rejected，attempts=2不变；未删除账本。停止服务前无运行周期/开仓意图，SQLite备份quick_check通过；仅本项目systemd停止/启动。部署后active/running、NRestarts=0，trading_enabled=true、entries_paused=false，5仓10张原保护单正确，无缺保护及在途意图。当前新仓仍10U/1x/TP0.5/SL4。市场穿越时的新市价退出分支仅有回归验证，尚未在真实Demo发生，不保证外部接口故障绝不重现。

证据：runtime/rez-stop-fix/{pre-state.json,postdeploy.json,live-evidence.json,predeploy-ledger.db}。官方字段依据：https://bybit-exchange.github.io/docs/v5/order/order-list ，https://bybit-exchange.github.io/docs/v5/order/create-order 。

## 用户授权新仓3倍杠杆（2026-09-10 19:11:48，Asia/Shanghai）

新仓杠杆从1x改为3x（保持合约最大杠杆及步长约束），每笔保证金仍约10U，通常名义金额约30U。净TP0.5U、SL4U、24h可达性、不限时持仓、原目标越价市价退出和重试规则不变。新意图risk_policy=fixed-tp0p5-sl4-3x-20260910，便于复盘分组。旧仓杠杆和原保护单不变。

166项pytest、Ruff、mypy通过。Demo只读仓单及实时3x报价/精度/规模验证通过。部署前1仓2单，无运行周期、开仓/订单意图及缺失保护；systemd停止后再次核对并备份SQLite、quick_check通过，再启动active/running、NRestarts=0。前后原仓位及保护单ID/价格/数量一致，自动开仓保持开启。新3x配置实际成交待自然周期，不额外制造测试订单。

证据runtime/leverage-3x-deployment/{predeploy.json,postdeploy.json,deployment.json,predeploy-ledger.db}。

## Telegram按钮400循环报错根因修复及用户授权清理（2026-09-11 00:58）

00:54起answerCallbackQuery持续返回HTTP 400。旧handle中无权限、已处理/未知按钮、数据库按钮三个分支的确认失败会抛到poll，offset未推进；poll又在getUpdates成功时提前resolve，造成每秒重新建incident/outbox及ERROR事件。日志未保存Telegram错误description，不能把400进一步断言为确切的过期或无效ID；停止服务后的只读getUpdates已无待取更新，不能重建当时按钮身份。循环根因由代码和HTTP模拟回归确认。

所有按钮分支统一使用acknowledge：确认失败保留安全的warning和callback标识，不让UI确认失败阻断业务完成后的offset推进。授权及持久回调幂等保持，合法人工重试仍只执行一次；真实处理失败继续抛出并保留offset。TELEGRAM_POLL仅在整批处理完成后resolve，避免处理故障每秒解除并重建告警。未改策略、Prompt、订单重试预算或TP/SL。

177项pytest、Ruff、mypy（38源文件）通过。新增10项HTTP边界回归覆盖400/ReadTimeout与无权限、未知按钮、重复回调、数据库恢复、合法重试五分支，检查后续暂停命令可达、offset跨重启持久及重试次数；另验证连续业务失败不解除告警、不重复创建outbox，成功后才解除。

停止前Demo只读核对8仓16张Reduce-Only订单，无RUNNING周期、交易INTENT或INTENT/SUBMITTING订单。仅systemd正常停止本服务，SQLite备份quick_check=ok，再启动；上线active/running、NRestarts=0，entries_paused=false。上线后8仓16单ID、方向、数量、价格、触发价、Reduce-Only全部一致。Telegram getMe/getWebhookInfo真实验证通过。真实400未主动制造；修复后观察同类错误是否再发生，不能将模拟回归称为真实过期按钮验收。

按用户要求清理本次00:50起TELEGRAM_POLL记录：119条incident、119条对应outbox、119条ERROR事件及119行项目日志；交易、订单、cycle及callback幂等记录保留。119个已发送告警消息按原message_id分两批deleteMessages，Telegram API均返回true。保留独立故障备份与清理审计，不操作共享systemd journal（无法安全按单服务逐条删除）。历史110126合约协议故障仍保留，未伪造解决。

证据：runtime/telegram-poll-fix/{predeploy.json,postdeploy.json,predeploy-ledger.db,telegram.before.py,service.before.log,cleanup.json,deleted-messages.json,pending-summary.json}。接口依据：https://core.telegram.org/bots/api#answercallbackquery 、https://core.telegram.org/bots/api#deletemessages 。

## 一周量价趋势、12h辅助与TP累计时长（2026-09-11 11:30:45 Asia/Shanghai）

用户最终确认：15m/30m/1h/2h完整一周为主，不足一周正常SKIP、不发方向模型、不递补；1m/3m/5m/10m近12h辅助（覆盖本轮中途的三天辅助要求）。direction-v8-weekly-volume-20260911强调周内量价结构，新增Decimal分日量/成交额/VWAP/覆盖时长/涨跌K线量及高成交量K线。明确OHLCV不是主动买卖delta、逐价成交分布或大单轨迹，不从参考文案引入窄止损或新持仓管理。

24h TP证据改1m分页，只累计整根已收盘、有成交、价格范围在TP外侧轻量缓冲之外的分钟，严格>300秒，可不连续；影线穿越、形成中、重复和窗口外不计。是保守时长下界，不是精确逐笔时长；不足正常SKIP。旧资金参数、原TP/SL、重试预算及退出方式保持。新仓risk_policy=weekly-volume-tp-duration-3x-20260911。

Ruff、mypy（38源文件）通过，182项pytest通过。覆盖一周不足不发模型/不报警、分页连续性、300秒边界/分散累计/重复/影线/未完成/窗口外、量价汇总及原执行恢复回归。真实Demo只读仓单检查、PONS/RAYDIUM双向TP和24h分钟分页验证通过，有通过亦有正常跳过。

真实完整只读筛选链路runtime/screening-review/20260911T032709922391Z：42候选、排除12持仓，筛选PUMPFUN/SNDK/NIULAI；方向SHORT/SHORT/LONG，均引用周内量价、15m与其他主周期及12h辅助。3次输入窗口正确且Prompt SHA256均为22b8ce105b23324fdfc2846d181832ecc6762177cd4565dcdfeefc295f763671，与部署一致。未为验证新增订单。

历史20笔TP门槛敏感性回放：20份24h历史完整，17份超过5分钟、3份不通过。采用成交后的原TP、不加tick缓冲，并以入场所在整分钟为历史窗口终点，因此不是实际开仓前报价门槛的精确复现，不能据此估算新版PnL。两笔最近已平仓交易作真实模型时点回放，只传入当时已收盘公开行情，原交易方向/盈亏不发模型：NESA原LONG→SKIP（周内巨量震荡分歧），RAYDIUM原LONG→LONG（周内量价抬升）。两笔原交易均盈利，不能称新版更赚钱。回放没有历史形成中K线，使用明确历史时钟检查最新收盘距时点不超过该周期；不将当前行情混入过去。最初回放适配因生产形成柱新鲜度检查报错，修正仅回放脚本后完成；未放宽生产时效验证。

11:30:45仅本项目systemd正常停止/启动；停止前后无RUNNING周期、开仓意图或订单INTENT/SUBMITTING，SQLite backup及quick_check通过。部署后active/running、NRestarts=0，自动交易开启、未暂停；原12仓24张保护单ID、方向、数量及价格一致，无缺保护。两项原有ENTRY异常保持，不伪造解决。不删除cycle/order/交易记录、不重放当前时间桶。

证据runtime/week-trend/：original源备份、pytest.txt、historical-tp.json、direction-replay/result.json和review.db、predeploy/stopped/postdeploy.json、deployment.json、predeploy-ledger.db。尚未验收新版自然新开仓及最终盈亏，不将只读调用与模拟回归描述为新策略盈利验证。


## 2026-09-11 逐文件复审与v9真实流程

审核77个源码、测试、配置与文档文件，实际Terra分8批复审并跨文件核实；处理明细见docs/FILE_AUDIT.md。修复监控晚阶段失败告警、恢复INTENT告警、外部仓位复查、outbox并发/中断持久预算、验收回调边界、筛选去重、扫描时间间隔、成交量20根基准、OI新鲜度、基准收盘截止、REST样本覆盖措辞及未启用组件的事件时间边界。停服取消补记INTERRUPTED，保留周期ID。203项pytest通过，Ruff及mypy（38源文件）通过；pip check及systemd单元校验通过。

首次真实只读完整周期选NIULAI/RAYDIUM/MARSCOIN，方向LONG/LONG/SKIP；结论复核发现措辞过度确认，人工将Prompt更新为direction-v9-weekly-evidence-20260911（SHA256 bf4575be7020a3013aa2ca1e360bb6f44002ac624154b4fd164357c89d339753）。v9真实完整周期audit-full:1789099052378075895：6份有效证据选2币NIULAI/RAYDIUM，方向均LONG，实际语义复核两份均PASS；原始日成交量、成交额及主周期收盘值另经维护端核对。周窗口/12h辅助/Prompt哈希检查通过；两币均在TP可达性门槛跳过，0私有写入、0新订单、0新incident。后续PRE_ENTRY_ACCOUNT及新增成交/保护单创建未在本轮触发，不能称新增交易全链路验收或盈利证明。

同样本v9重放存在MARSCOIN SKIP→SHORT波动；RAYDIUM引用较早30m收盘未标注具体时间。保留证据，不宣称模型完全一致或没有任何分析误差。最终代码跨文件复审及取消处理/v9文案追加复审均无阻断项。

部署仅本服务正常systemd停止/启动，期间新扫描周期1490916被取消，无在途订单或未确认开仓。SQLite backup/quick_check通过；对该周期作有维护事件证据的INTERRUPTED状态修正，不删除记录、不重跑。12:02部署后active/running、NRestarts=0、自动交易开启且未暂停，原10仓及20张TP/SL订单ID、数量、价格完全一致，无缺保护、无运行周期或在途意图。2项原有ENTRY协议未签署异常仍保留。

证据runtime/system-audit-20260911/：manifest/final-manifest、batch-0至7、dispositions、final-review、cancellation-review、flow/result、flow-v9/result、flow-v9-contract-check、semantic-review-v9、replay-v9、pytest-final、before-stop/stopped/postdeploy、deployment及predeploy-ledger.db。


## 2026-09-11 平仓同步通知等待期修复

994c83ac对应NIULAI交易039d7d1b540fe91ec7d0121f，检查时已CLOSED、异常RESOLVED、平仓outbox SENT。Demo真实closed-pnl按原TP订单核对净收益+0.51664901U。账本显示发现平仓至最终证据采集约210秒，旧90秒阈值提前提醒；未保存各次历史响应，不能进一步断言具体哪类历史记录延迟。

正常同步提醒等待期改300秒，继续原30秒只读核对与SETTLING防重入；等待起点及缺失原因持久化，订单/盈亏数量和逐笔成交缺失均受统一提醒规则约束，异常仍去重、完成后解除。接口异常仍由原错误通道及时报警。未改净收益算法或交易策略。

206项pytest、Ruff、mypy通过，新增210秒内恢复无告警、300秒边界提醒、重复观察去重、重建Monitor保留等待时间及逐笔缺失最终恢复回归。仅本服务systemd正常停止/启动，停止前后无运行周期、开仓意图或在途订单；SQLite backup和quick_check通过，部署前后11仓22保护单身份、数量与价格一致。服务active/running、NRestarts=0。新等待期通过模拟验证，未制造真实延迟。证据runtime/settlement-notice-fix/。

## 2026-09-12 默认止损2.7U

按用户明确要求，新仓SL_LOSS由4改为Decimal("2.7")，新意图持久化sl_loss=2.7，risk_policy=weekly-volume-tp-duration-sl2p7-3x-20260912。已有仓位保护价格、旧意图预算与无字段历史8U恢复保持；TP0.5U、10U保证金、3x杠杆、筛选/方向及TP时长门槛不变。同步README、运行规则、Bot策略说明。

止损多空净成本计算及执行恢复56项测试通过，保留历史4U并新增2.7U持久预算恢复；Ruff、mypy通过。首次沙箱全量测试停滞后中止，正常环境全量207项通过（19.98秒）。未制造新交易作验证。

Demo真实只读核对部署前、停止后、部署后均2仓4单，无运行周期、交易INTENT或INTENT/SUBMITTING订单。SQLite backup/quick_check通过；仅本服务systemd正常停止/启动，active/running、NRestarts=0，原仓位和4张保护单ID、方向、数量、价格保持一致。未改变暂停状态、删除账本或修改其他项目。新2.7U仓位自然执行尚待后续周期；市价滑点使实际损失不保证精确等于预算。

证据runtime/sl-2p7-deployment/：原文件备份、predeploy/stopped/postdeploy.json及predeploy-ledger.db。

## Telegram开仓配置入口（2026-09-13 12:00:21，Asia/Shanghai）

按用户要求，主虚拟键盘第三行增加“⚙️ 开仓设置”，成为三行六键；/settings同入口。参考带单员交易项目的开仓控制/默认总价值/杠杆交互，仅查看源码，不修改或依赖其他项目。面板支持每笔保证金、每笔总价值、默认杠杆、净止盈和净止损。总价值明确为单笔杠杆后名义金额，输入时按当前杠杆换算保证金、向下保留8位小数；调整杠杆保持保证金，预计总价值随之变化，不新增整个策略资金上限。

流程：选择字段→输入正数→完整预览→保存/取消；10分钟有效。当前初始值沿用保证金10U、3x、TP0.5U、SL2.7U，没有因为新增入口改参。金额Decimal，杠杆1—5倍，实际交易仍有合约精度/最大杠杆、资金预留20U、TP时间门槛等限制。配置及版本保存SQLite state.entry_defaults，修改审计ENTRY_DEFAULTS_CHANGED；保存、去重、审计与回复同事务，失败回滚。绑定chat/user双校验、过期和旧预览拒绝、重复点击幂等，不修改.env。

执行器每笔加载完整配置快照，参数决定数量、杠杆、TP可达性和SL；最终下单前检查版本变化，变化则正常SKIP_SETTINGS_CHANGED，不混用新旧配置。开仓意图持久化entry_defaults、sl_loss和risk_policy=owner-entry-defaults-v1；已有仓位及在途意图继续冻结原参数，后续保存无需重启。/strategy也读取当前保存值。

226项pytest通过，Ruff通过、mypy40源文件通过。新增输入校验/权限/旧预览/超时/版本冲突/取消/重启/事务回滚/重复保存/总价值换算，以及真实执行分支的资金约束、配置变更竞态和旧仓冻结测试。Demo只读Executor验证：RAYDIUM默认10U/3x/TP0.5/SL2.7及独立测试库20U/2x/TP0.75/SL3均DRY_RUN_ELIGIBLE；PONS已有仓位正常跳过，测试库零交易、零订单，私有写入禁用。未将测试配置写入生产。

部署前2仓4张保护单，无运行周期、开仓意图或在途订单，原5项活动异常保留。仅正常systemd停止本项目，停止后再次确认无在途，SQLite backup/quick_check通过，初始化默认配置并启动。12:00:21 active/running、enabled、NRestarts=0，自动开仓开启且未暂停。前后原2仓4单身份/数量/价格/订单ID一致。新键盘及设置面板已投递SENT，message_id分别4226、4227；仅确认API投递，不冒称手机端实际点击测试。

证据runtime/trading-settings-deployment/{pre-state.json,postdeploy.json,deployment.json,readonly-results.json,readonly.db,predeploy-ledger.db}。新增代码trading_settings.py/settings_controls.py及tests/test_trading_settings.py，未修改模型或交易方向规则。

## 2026-09-14 开仓设置增加周期轮询间隔

新增“⏱ 周期轮询间隔”，支持1—1440整数分钟，默认20分钟。沿用绑定用户权限、输入预览、保存/取消、版本冲突、回调幂等和事务审计；旧配置自动兼容默认值。调度空闲时每秒检查保存配置，按UTC时间边界运行；修改后等下个边界，不立即补跑当前桶。当前周期截止时间在启动时冻结，重启按间隔/桶去重，默认20分钟保留原ID。状态、恢复提示、策略说明同步读取配置；30秒仓位观察与订单重试逻辑保持。

236项pytest通过，Ruff和mypy通过。新增周期输入校验、预览保存/重启持久化、状态展示、旧配置兼容、时间边界与唯一ID、变更后等待及重启防重复、空闲动态重载、执行中修改保持原截止时间回归。没有修改外部API接口或制造验证交易。

仅本项目systemd正常停止/启动；部署前、停止后、启动后Demo真实只读核对均0仓0单，无RUNNING周期、开仓INTENT或INTENT/SUBMITTING订单。SQLite backup/quick_check=ok，原保存配置保持一致，未改生产轮询值、删除账本或主动发送Bot消息。服务active/running、NRestarts=0，观察心跳新鲜。重新打开开仓设置即可取得新增按钮；手机实际点击保存及非默认频率真实周期尚未验收。

证据：runtime/cycle-interval-deployment/{verify.py,predeploy.json,stopped.json,postdeploy.json,predeploy-ledger.db}。

## 2026-09-14 重复网络告警及数量序列化根因修复

近期主要故障为Telegram getUpdates的ReadTimeout/ConnectTimeout/502；一次完整轮询成功立即resolve，使同一段间歇故障反复生成新incident/outbox。改为完整批次连续成功300秒才解除，中途任何取更新或处理失败重置观察窗口；重启重新观察，不把离线时间计作健康。失败轮询按2/4/8/16/30秒封顶退避，首次真实错误仍立即交给原告警通道。未增加消息发送或交易提交重试，未删除历史通知。底层公网故障的运营商/路由根因无法从现有日志确定，不能承诺不再出现超时。

历史LSK ENTRY 8ffd015c69a7084518ea71e6保存的qty为"1.2E+2"，交易所拒绝10001 Qty invalid。Decimal按步长计算可产生科学计数法，直接str写入API是序列化缺陷。新ENTRY和新保护意图的数量/价格以及杠杆请求统一用定点十进制；落库与发送一致，不改原有不确定订单ID或载荷。回归复现120的科学计数法输入、落库先于发送、小价格保护单及原幂等/重试规则。只读Demo当前LSK数量步长为0.1，该当前值不能代替历史过滤参数；没有额外下单制造真实拒单或成交。

Telegram getMe/getWebhookInfo真实读取通过，公开Telegram和Demo连通性检查通过；未发送测试消息。协议未签署110126及旧模型故障保留，不替用户签协议或假报解决。证据runtime/exception-root-fix/。Telegram轮询与offset依据：https://core.telegram.org/bots/api#getupdates 。

验证完成：239项pytest通过（22.81秒）、Ruff及mypy（40源文件）通过。17:32:30 Asia/Shanghai仅本项目systemd正常停止/启动；部署前、停止后、部署后均0仓0单、无RUNNING周期/开仓INTENT/订单INTENT或SUBMITTING。SQLite在线backup、quick_check=ok；active/running、NRestarts=0，日志确认trading_enabled=True。未修改保存设置、交易账本或历史异常状态。上线短时未出现新错误；持续网络恢复和新格式自然交易仍需运行观察，未宣称真实故障注入验收。

## 2026-09-14 18:00模型容量异常与深度回归

新故障为SCREENING_MODEL_FAILURE事件2763：固定gpt-5.6-terra/medium收到CLI原始ERROR: Selected model is at capacity，非Telegram故障复发或模型名错误；该周期停在筛选，无方向调用/下单。上游具体容量/账户路由原因无法由本地日志进一步确定。用户明确要求重试后，独立只读真实周期live-full:1789380274488118934于18:06完成，85.95秒，选AKEUSDT、方向LONG，程序SKIP_TP_UNREACHABLE，0订单0新异常。未使用该方向进行生产开仓。筛选和方向Prompt SHA256与原版本相同，证据runtime/model-root-fix/live-contract-check.json。

扩展历史复查发现WLD/LSK在9月12日的401 Unauthorized被当作单币普通模型错误，同轮仍可继续请求；超时仅抛空TimeoutError，诊断不清。新增认证/限流/503/连接失败分类，超时先杀进程组后归类共同模型服务故障，同轮后续候选停止，保留每次仅一次宿主调用，不切模型、不加模型自动重试。取消仍传播CancelledError。缺失输出和无效JSON提供事件类型及signal_id；非零退出不采用已写输出，日志保留有界ERROR行以便定位。

修复MODEL_SERVICE恢复判断：筛选有效且选0个也能证明模型恢复；方向Schema失败不能解除共同故障。原逻辑零候选不解除、有候选但方向报错反而可能解除。测试覆盖连续3轮故障只产生1个incident/outbox，恢复使用新证据、同周期不重放、筛选故障不进入方向。Bot区分容量、额度、认证、限流、超时和连接故障；明确拒单不再误说“结果待核对/该币锁定”，110126说明需账户签署协议，Qty invalid说明数量格式/精度。Telegram轮询提示不再误称交易状态检查异常；最近异常展示原发生时间，避免历史待处理项被误认新故障。未代签合约协议、修改策略或消息/交易重试预算。

官方接口参考：https://developers.openai.com/codex/noninteractive 、https://developers.openai.com/api/docs/guides/error-codes 。真实重试成功只能证明该时刻模型可用，不能保证上游永不容量不足或要求用户盲目反复点击。

最终269项pytest通过（25.03秒）、Ruff、mypy（40源文件）、pip check和systemd单元检查通过。新增30项故障/恢复/通知测试，包含真实子进程而非仅函数mock。仅本项目systemd正常停止/启动；停止前、停止后、上线后均0仓0订单、无RUNNING周期或在途意图。SQLite备份quick_check=ok，服务active/running、NRestarts=0，保留用户当前已恢复开仓及30分钟轮询设置。上线前确认生产没有晚于2763的新模型失败，以真实重试成功证据事务解除441784ef278d3ab5f5759ffb并追加MODEL_SERVICE_RECOVERY_VERIFIED审计；历史错误/通知/交易记录均保留。独立验证未发送Bot消息、不增加生产订单；未承诺上游容量故障永久消失。

## 2026-09-15 方向订单流证据优化（验收记录，部署状态见下方）

保留周内趋势与原执行边界，方向新增三次50档REST盘口、最多1000笔近300秒主动成交、逐价样本与30/60/300秒Delta；Decimal计算，覆盖完整性明确false。相邻可见价位中位数量3倍仅是大挂单描述，不证明身份或真实性。没有连续DOM、撤单/补单确定识别、完整Footprint、斜向堆叠失衡、实时触发器或5—15秒退出；保存的周期及资金参数不改。

方向v10独立文件防止旧进程热读新Prompt却缺新数据；旧v9保留回放。真实复测曾出现15m/30m范围混淆及K线内最低价时间顺序错误，新增latest_closed_candles事实和周期/UTC时间引用要求后重测。所有失败样本保留。复核器曾把缺失订单流案例与完整输入比较而误报；脚本改为共同输入加逐案例覆盖，保留真实测试差异。复核输入重复三份曾超过CLI 1048576字符上限，改为无损共享输入去重，未改变生产模型输出或增加自动重试。

287项pytest通过（25.15秒），Ruff、mypy41源文件通过，pip check及systemd单元校验通过。新增测试覆盖Delta/逐价核算、重复ID去重及冲突、异常币种/时间/盘口、采样不代表连续、相同序列披露、缺证据与最新收盘、采集失败不得进入模型或下单；原执行、TP/SL、恢复、状态与通知回归通过。

首个真实只读完整周期live-full:1789450826085507988选FF与POWER，分别SKIP和LONG，POWER为DRY_RUN_ELIGIBLE（通过程序开仓前检查），零私有写入/订单/新增异常。第二轮live-full:1789451074639120309验证10候选→6证据→选1币FF→SKIP，零新增异常。曾遇到真实模型额度错误，后续用户要求继续后模型恢复响应；没有切模型或增加生产重试。只读流程与模拟回归不证明新策略盈利、真实新仓成交或自然TP/SL退出。

证据：runtime/orderflow-review/，包括original、first-review、second-review、review.db、模型案例、原始复核与修正复核、各轮完整流程日志、pytest-final.txt、manifest及部署前后只读状态。原始完整周期数据库为runtime/live-full-cycle/trader.db。筛选和方向固定gpt-5.6-terra/medium、无工具隔离及严格Schema。

最终核验与上线：修正案例输入对应关系后的真实Terra语义复核PASS（model-review-corrected.json）。最终Prompt SHA256 caff50b4d64b3e8f663e22c4443a9e84a0c0f360f18d081602b7220008259214；完整周期live-full:1789454623122997841用相同哈希，CAP/LSK均SKIP，113.01秒，零订单及新增异常。维护端核对最新主周期收盘及订单流Delta/首末价格一致。仍有输出措辞局限：CAP高成交历史范围未明确单一周期，LSK把三次快照写作“三档盘口”；没有改变方向权限或数值计算，但不能宣称全部文本绝无误差。原失败复核与额度错误均保留，非自动改Prompt。

14:46左右Asia/Shanghai仅本项目systemd正常停止/启动，前后均0仓0单、无RUNNING周期、交易INTENT或订单INTENT/SUBMITTING。SQLite在线backup、quick_check=ok；上线active/running、NRestarts=0，monitor心跳新鲜，保存设置及暂停状态完全一致。未发送测试Bot消息，未修改其他项目、删除交易/cycle/order账本或强制重跑。没有新增真实交易验收或盈利结论。
