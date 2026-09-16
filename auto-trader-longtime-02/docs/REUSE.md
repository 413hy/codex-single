> 原项目复制资料：以下部署状态、账户验收和旧止损策略不代表新系统。新系统以 README.md 和 HEDGE_STRATEGY.md 为准。

# VPS现有模块审查与复用决策（2026-09-09）

|能力|现有位置|处理|
|---|---|---|
|程序选币|`/opt/bybit-signal/src/bybit_signal/selection/scanner.py`|复制到独立包；保持评分与现有全部阈值；提前排除持仓；底层保留完整排名；筛选层前10→6份证据→模型选0—3|
|公开行情及校验|`/opt/bybit-signal/src/bybit_signal/providers/bybit.py`、domain/models.py|复制行情客户端和最小Candle模型；独立HTTP连接|
|Bybit签名、校时、错误处理|`/opt/bybit-trader/src/bybit_trader/bybit.py`|抽取原签名传输代码；私有请求锁定Demo；重构仓位索引、分页和订单确认|
|原下单/TP/SL|`/opt/bybit-trader/src/bybit_trader/execution.py`、calculations.py|复用Decimal舍入与持久化意图/成交核对方式；新实现GTC reduce-only TP及条件市价SL|
|旧持仓管理|旧execution.py、`/root/ai-trader/bridge/dual_execution.py`、flexible_policy.py|不引入反手、浮亏调TP、角色切换、时间退出、加减仓|
|模型|`/root/ai-trader/bridge/model.py`|重构无工具Codex CLI隔离调用；保留超时进程组清理及严格Schema；按最新授权使用gpt-5.6-terra/medium；去掉模型自动修复及容量重试|
|Telegram|旧telegram.py、`/root/ai-trader/bridge/notifications.py`|复用白名单、持久outbox、故障去重设计；新增持久化内嵌重试按钮、回调幂等|
|数据库|旧store.py、bridge/store.py|独立SQLite WAL；新建cycle、signal、trade、order intent、incident、outbox等表|
|平仓核账|`/root/ai-trader/bridge/settlements.py`|复用订单ID与closed-pnl交叉核对；扩展分页、长持仓时间分段、分笔退出归集|
|调度|`/opt/bybit-signal/src/bybit_signal/scheduling.py`|复用自然时间边界原则；20分钟周期+独立30秒观察；SQLite周期唯一键及进程锁|
|服务部署|原项目deploy/systemd、service.py|独立.env、venv、日志、数据库、systemd服务|

源码基线：bybit-signal `1c7fc901f9cf9325a255fee270e3a0b54779f17b`，bybit-trader `81c0a2cd250fb41e796aae93da7f7497792b7bbd`。
/root/trading-cheap、/root/bybit-trader为开发副本；/opt为部署副本。旧备份只作历史参考。
/root/ai-trader正在运行主动管理策略；binance-trader为正式盘Binance项目，与本项目无运行依赖。

新项目不导入旧工程，不写旧数据库或配置。vendor/PROVENANCE.json记录复制文件的原始SHA256。
原选币的60币预采池和合理过滤保持。按用户更正，恢复信号系统的前10候选采集→优先6份完整证据→模型筛选0—3个流程；只有入选币进入独立方向模型。不直接截取扫描排名前3名，不向后递补开仓。

## 最新确认：故障处理

TP/SL首次创建失败，仅自动重试一次，预算持久化防止重启刷新次数。仍失败不平仓，不后台循环补挂。所有故障通知附重试按钮。
按钮由绑定用户触发；每次点击执行一次，先重新核对交易所、仓位身份、订单和原始参数。已成功/已结束回调不产生新订单。
行情/模型故障的重试读取新行情重新判断；不复用旧方向。外部已有仓位不接管，相关按钮仅重新核对。

## 官方接口依据

- https://bybit-exchange.github.io/docs/v5/demo
- https://bybit-exchange.github.io/docs/v5/order/create-order
- https://bybit-exchange.github.io/docs/v5/position/close-pnl
- https://developers.openai.com/codex/noninteractive
- https://core.telegram.org/bots/api#answercallbackquery

## 本次模型故障与适配

真实批量验证中，Astra上游返回`Selected model is at capacity`。按用户授权，当前固定改为旧bridge配置采用的`gpt-5.6-terra / medium`；不在交易过程中静默切换模型。

接口边界：`Markets.scan(excluded)`输出原选币器候选，`Markets.evidence(symbol, candidate)`提供新行情，`DirectionModel.decide(signal_id, context)`返回严格`Decision`，`Executor.enter(cycle_id, signal_id, decision)`统一负责资金、真实成交与保护单。旧项目无需启动额外HTTP服务，适配在独立包内直接调用，避免运行依赖。

模型调用串行处理；识别上游容量/额度故障后，同轮剩余币记录SKIP，只保留一个模型服务告警及手动重试按钮。下一轮读取新数据重新调用，不缓存失败方向，也不额外增加选币指标。

## Terra方向模块上线前复核

按照OpenAI Docs的GPT-5.6家族Prompt指导，保留简洁的任务、业务边界和输出约定，不加入通用开发代理流程。方向Prompt明确未收盘K线的含义、多周期暂时反向不构成数据冲突、不能臆造指标或消息；不新增指标门槛。

调用固定Terra/medium、独立临时目录、忽略用户配置、AGENTS内容预算为0、跳过宿主Skills发现；禁用Skills搜索/依赖安装、记忆、插件、Apps、浏览、Shell及多代理工具。无需给方向模型配置交易Skill；所有脚本由宿主程序执行。MODEL_INPUT记录Prompt SHA256，便于人工比较结果，程序不会自行改Prompt。

参考：https://developers.openai.com/api/docs/guides/prompt-guidance-gpt-5p6


## 选币链路纠正（2026-09-09）

此前只复用scanner、对所有合格候选逐币开仓，遗漏了信号系统的六币横向复核。当前补入`screening.py`适配层和`vendor/signal/`公开采集、证据、筛选Schema/校验代码；原系统文件和数据库保持只读且无运行时依赖。

- 复用原3m/5m/15m/30m/1h/4h行情、盘口、成交、OI、市场背景及可选跨交易所参考；缺失维度按源逻辑披露，不补造数据。未接入实时强平流，相关证据按不可用处理。
- 原筛选Prompt、模型操作手册和合同作为固定prompt资产保存到`prompts/screening.md`，不让模型发现宿主Skills或调用工具。原始来源SHA256见PROVENANCE.json。
- 从domain/models仅提取公开证据需要的类型；不引入旧持仓管理、回测、自学习或策略调整代码。
- 复用原引用规范化及绝对质量门；引用规范化前先拒绝不存在/跨币引用，额外先严格核对六币覆盖、最多3个和连续唯一排名，拒绝超量输出。保存原始模型输出和最终筛选结果。
- 筛选和方向共用本项目隔离的单次Terra/medium调用器。筛选失败不开仓；手动候选重试也重新完整筛选，不能绕过筛选层。
- 市场背景证据直接内嵌，避免引用未提供的批次背景对象。未改变筛选的市场质量标准。


方向与通知更新：详见MODEL_CONTRACT.md。Telegram底部控制键盘参考/opt/bybit-trader及/root/ai-trader的只读布局，保留本系统权限与持久outbox；不引入原系统调参、主动平仓操作。结算查询从生命周期开仓前1秒至当前，按ID归并历史；不再按滑动游标排除较早创建的TP/SL，修复CASHCATUSDT真实平仓核账遗漏。

已收盘方向指标更新：本项目indicators.py独立以Decimal实现互补指标，避免将筛选层浮点快照或不存在的足迹/热力图冒充方向数据。公式及最新周期分组见MODEL_CONTRACT.md，真实Terra讨论与验证见ACCEPTANCE.md。

## 一周量价更新（2026-09-11）

vendor/public.py的recent_candles增加有界分页，服务24h的1441根1m TP证据；保留原始来源哈希作为基线，不冒充未修改vendor。未修改原信号系统或其他交易项目。主趋势和12h辅助窗、量价证据、正常历史不足SKIP在本项目适配层实现，详见MODEL_CONTRACT.md。

## 2026-09-11逐文件审核适配补充

本地vendor扫描器拒绝重复/逆序/非整5m间隔；筛选证据的20根成交额基准纠偏，OI变化校验实际5m连续性和末点时效，REST逐笔仅表述接收样本跨度，不声称连续订单流。BTC/ETH旁证按采集截止过滤并校验新鲜度。未启用流缓存修正事件时间不能代替连接心跳、乱序过期裁剪，未启用结果类补哈希格式；没有启用新服务或引入其他项目运行依赖。原PROVENANCE继续记录来源基线，当前全文哈希另保留在runtime/system-audit-20260911/final-manifest.json。

2026-09-15：本项目新增orderflow.py复用vendor/public.py公开盘口和逐笔接口；盘口observed_at改为返回快照ts，逐笔新增返回symbol核对。vendor原来源哈希仍是基线。本次新增文件与修改证据见runtime/orderflow-review，不改旧项目。
