# 三系统逐文件审核（2026-09-26）

审核口径：检查活动源码、调用链、配置与库结构；对更改运行测试、静态检查和本次真实轮次。下面每项列出相应活动文件；同名交易文件逐一比对两套项目，差异在对冲专属行注明。所有运行凭据、交易库和模型认证文件均未纳入仓库。

## 统一分析系统

| 文件 | 审核结论 |
| --- | --- |
| `__init__.py` | 包初始化，无副作用。 |
| `app.py` | 固定轮次串联发现、两阶段模型、双仓去重、信号发布与通知；单币采集失败隔离并持久告警；发布时复核 Bybit 证据年龄；取消轮次不重放。 |
| `bot.py` | 只管理分析频率、暂停、历史、异常及轮内详情；不管理交易设置。 |
| `config.py` | 模型配置分为 TradingView 初选与方向精筛；TradingView 不可关闭；不加载交易 API 凭据。 |
| `indicators.py` | 只计算已收盘量价技术背景；未把派生指标冒充独立来源。 |
| `market.py` | Bybit 清单只核实可交易身份；方向复核按已收盘 K 线、近期时间与订单流证据验证；TradingView 是唯一全市场发现入口。 |
| `model.py` | 模型请求在隔离子进程，严格 Schema、一次请求、提示词哈希和原始响应审计；分析进程本身不持交易密钥。 |
| `notices.py` | 分析异常及通知文本；无交易执行入口。 |
| `notifications.py` | 每轮汇总与固定轮次的币种详情按钮；600 秒信号时效用语已同步。 |
| `orderflow.py` | Bybit 盘口及主动成交样本作为局部复核；明确采样范围。 |
| `polling.py` | 分析 Bot 的轮询与失联恢复；持久 offset。 |
| `scheduling.py` | 1970-01-01 上海 00:00 锚点、10—1440 分钟整十、错过周期跳过。 |
| `selection.py` | TV 初选严格 1—10；Bybit 最终严格 1—3 且首选方向明确；输入币必须来自有效证据。 |
| `signals.py` | 每轮每币唯一 ID、600 秒 TTL；LOCKED 双仓按 20 秒新鲜快照、唯一组、双向槽位和账本归属核实。 |
| `store.py` | 仅分析事件、轮次、信号、异常与通知表；空的旧交易表已移除。 |
| `tradingview.py` | 同合约技术页、Ideas 和榜单身份/字段校验；15m/30m/1h 核心技术快照必须完整且近期。技术页单独 404 时用身份一致的合约主页核实，403 等访问失败仍拒绝。 |
| `tradingview_discovery.py` | CEX 全页验证和限额均衡待审池；先排除短周期栏过期币，按 15m/1h 技术评级和相对流动性排列技术候选；没有旧 Bybit 评分、绝对阈值或二次递补。 |
| `tradingview_sources.py` | 可扩展的币种背景、新闻适配器；可选来源显式记缺失，取消渲染时终止子进程并传递取消信号。 |
| `vendor/__init__.py`、`vendor/models.py`、`vendor/public.py`、`vendor/PROVENANCE.json` | Bybit 公共行情合同、读取器及来源记录；无私有交易请求。 |
| `prompts/tv_selection_v1.md`、`direction_selection_v1.md`、`direction_v15.md` | 当前三阶段提示词与 1—2 小时主时域一致；真实模型验证见 `model_validation.json`。 |

## 两套交易系统共同活动文件

| 文件（各项目 `src/longtime/` 下） | 审核结论 |
| --- | --- |
| `__init__.py`、`__main__.py`、`cli.py` | 入口只提供消费、状态与交易端管理；旧 `scan` 命令已删除。 |
| `config.py` | 独立 Demo 凭据与共享信号路径；没有模型/选币配置。 |
| `emergency.py` | SQLite 故障时的本地告警兜底，属于交易端运行保障。 |
| `exchange.py`、`transport.py` | 私有调用限定 Demo 账户；订单和仓位核对保持原 ID 与账本。 |
| `execution.py` | 信号通过本端仓位、资金、数量和退出保护约束；无模型调用。 |
| `market.py` | 只保留纯 K 线验证/兼容计算函数；生产服务不再构造公共行情客户端或自行扫描。 |
| `model.py` | 只含 LONG/SHORT/SKIP 信号 Schema，无模型服务。 |
| `monitor.py` | 交易所仓位/订单心跳与恢复；不驱动选币周期。 |
| `notices.py`、`polling.py`、`telegram.py` | 交易通知与 Bot；分析通知由独立分析 Bot 负责，交易 Bot 不修改分析频率。 |
| `risk.py` | 金额、杠杆、精度和 TP/SL 数学；普通开仓不调用旧 TP 可达性过滤。 |
| `service.py` | 按 0.5 秒消费共享发布、独立监控与通知；不启动市场扫描、模型或分析调度。 |
| `settings_controls.py`、`trading_settings.py` | 仅交易金额、杠杆和退出参数；没有分析频率控制。 |
| `signal_consumer.py` | 只读 600 秒有效发布、独立持久认领、到期与本端风控检查；重启不重放。 |
| `store.py` | 保留交易账本和历史周期数据；新库不再创建旧分析周期表。 |
| `vendor/__init__.py`、`vendor/models.py`、`vendor/PROVENANCE.json` | 兼容 K 线合同及来源记录；旧公共市场扫描读取器已移出活动源码。 |

## 对冲端专属文件

| 文件 | 审核结论 |
| --- | --- |
| `auto-trader-longtime-02/src/longtime/hedge.py` | 只对同组、同 generation、LOCKED 双仓应用有效方向；逆向仓处理、顺向保护、条件限价对冲与重启恢复保留原策略。 |

验证：分析端 Ruff、mypy、pytest；两交易端 Ruff、mypy、pytest 均通过。真实行为、降级原因及消费结果见 `LIVE-AUDIT.md`。交易端仍保留历史账本与为历史合同设计的纯函数，这些文件不在选币/模型/全市场采集调用链中。
