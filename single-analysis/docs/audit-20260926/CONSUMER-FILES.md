# 两套交易消费端逐文件审计（源码阅读完成）

审核日期：2026-09-26。范围：`/root/auto-trader-longtime/src/longtime` 与 `/root/auto-trader-longtime-02/src/longtime`。已读取两端AGENTS、OPERATING_RULES及对冲HEDGE_STRATEGY。未操作生产数据库、未重启服务、未触发交易或模型。

## 已确认及修改

1. 两套 `signal_consumer.py` 原有解析/Schema/时间校验处于执行异常捕获之外，`tick()` 又无逐条隔离。有效期内任一坏发布会挡住后续所有发布。经主代理授权，已将纯校验与执行隔开；坏发布使用本地state和`SIGNAL_REJECTED`事件同事务持久拒绝，后续信号继续，不把原payload或校验异常文本写入告警，重启不重复处理。仍只读共享库；交易提交失败继续走原持久认领路径，不重放。
2. 两端分别新增6个消费者测试，覆盖坏JSON、非对象、非法方向、TTL不一致、非法对冲归属、执行ValueError不得误分类。目标Ruff通过、消费者测试各21项通过。
3. 主代理已要求停止继续修改源码；后续发现仅报告。

## 具体未修复发现

- **双仓快照年龄计量不准确（已通知主代理）**：对冲 `monitor.py:145–146` 获取并保存 `exchange_positions`，之后 `check_external()` 可能耗时多个API调用，最后`:172`才保存 `monitor_heartbeat`。分析 `signals.py:83–89` 以此heartbeat计算快照年龄。旧快照可能被较晚heartbeat错误认定20秒内；两个键又分事务保存。建议positions及独立observed_at同事务提交，分析读取实际快照时间；heartbeat只表示监控进度。
- **旧扫描通知话术遗留**：普通端 `notices.py` 仍含 `MODEL_SERVICE`、`SCAN_DATA/CANDIDATE/CYCLE/MODEL` 老分析分支，ENTRY明确拒绝动作仍称“按钮会重新取行情并筛选”。实际 `service.retry` 对旧candidate/cycle只核对INTENT或resolve，不扫描。属于误导性话术，不代表扫描逻辑仍在运行。
- **死兼容代码待清理**：两端 `market.py` 仅K线校验/10m聚合，`vendor/models.py`只有Candle，`risk.reachable_tp`为已停用旧TP可达性函数；目前生产App没有market实例。这些不是活动扫描器，但与“不要遗留”目标不完全一致；删除前需把保留测试归档/迁移并确认无动态导入。

## 已完整阅读或同源逐差异核对的文件

| 文件（两端，除说明外） | 角色/结论 |
|---|---|
| `__init__.py`, `__main__.py` | 包描述和CLI入口，无模型/扫描入口。 |
| `cli.py` | 仅config-check、preflight、cycle(消费)、serve、status；ProcessLock独占runtime。 |
| `config.py` | 凭据仅执行端SecretStr，Demo固定地址；默认共享signals.db。 |
| `service.py` | 0.5秒共享信号轮询，普通30秒/对冲5秒监控；无分析调度/模型对象。 |
| `signal_consumer.py` | 只读feed、600秒TTL、各端独立信号主键与cycle/symbol唯一认领；已修坏发布阻断。 |
| `model.py` | 纯Decision Pydantic schema，无模型调用。 |
| `market.py` | 纯兼容函数，无网络/发现入口；建议清理死兼容代码。 |
| `risk.py` | Decimal定量、最多5x、合约精度/数量边界；reachable_tp未接生产流程。 |
| `store.py` | SQLite FULL/WAL、订单/信号/outbox持久化、唯一约束独立去重。 |
| `exchange.py` | 私有Demo与公共单币接口；无全市场选币。对冲只额外强制双向模式。 |
| `transport.py` | Demo地址校验、HMAC签名、关闭redirect。对冲只对GET暂时传输失败重试一次，不重发交易突变。 |
| `execution.py` | 持久化意图先于下单，响应未知按原ID核对；入场前再次检查仓位、暂停、TTL/余额/设置。对冲子仓只核账不挂SL。 |
| `monitor.py` | 交易所状态核对/清算/outbox；对冲快照年龄问题见上。 |
| `hedge.py`（仅对冲） | LOCKED/group/generation复核；decide持交易锁并重新读双腿身份/数量、报价、TTL，再持久UNLOCKING；条件对冲保持限价，恢复只原订单核对。 |
| `emergency.py` | 独立文件数据库异常通道，无凭据输出。 |
| `polling.py` | 按Bot标识哈希获得本机单实例锁，持久恢复等待。 |
| `notices.py` | 发现旧扫描话术；其余错误对用户说明。 |
| `settings_controls.py` | 交易参数编辑保存事务化，cycle_minutes明确拒绝/清理旧pending。 |
| `trading_settings.py` | 旧cycle_minutes仅兼容读取，不再输出/使用；未来可做单次配置迁移。 |
| `vendor/__init__.py`, `vendor/models.py`, `vendor/PROVENANCE.json` | 空包及旧Candle纯Schema、来源记录；无扫描调用。 |

| `telegram.py` | 两端全文及差异读完；身份鉴权、持久回调去重、outbox、交易状态、交易参数和管理键盘符合执行端边界。发现对冲端缺429冷却、两端只读resume旧话术。 |

## 补充问题与可选改进

1. **对冲Telegram限流处理漂移**：`/root/auto-trader-longtime-02/src/longtime/telegram.py:121` 将429当普通失败，不解析`retry_after`；`:221`固定30秒重发，两个失败就耗尽outbox预算。普通端`telegram.py:138–166`已处理持久共享cooldown，对冲端没有。建议把通用429冷却移植并覆盖发送/接收联合冷却、重启保留和预算测试；当前无需变化交易策略。
2. **恢复提示遗留**：普通`telegram.py:405`、对冲`:353`，`trading_enabled=False`时仍显示“已恢复扫描”。实际只恢复新仓消费，建议改为“已恢复接收有效信号；当前为只读验证模式”。
3. **坏发布告警解释可优化**：此次已持久明确REJECTED，但通用`notices.py`目前会渲染“交易状态检查异常”，用户看不到“仅这条信号拒绝，其余继续”。建议增加`SIGNAL_REJECTED`特定文案；拒绝记录保持不可重放，按钮仅确认/查看而不能重新交易。
4. **历史兼容要区别对待**：旧`cycle_minutes`配置只读兼容、旧incident恢复分支、v1 envelope（仍要求600s）都不会重新启动分析。若用户要求删除所有兼容，应先迁移历史配置/菜单而不是直接删除账本。`market.py`/Candle/`reachable_tp`当前只有tests使用，确为可清理死代码。
5. **后续证据增强**：消费者的`observed_at`字段目前只有存在性验证，生产者控制数据时效；可以校验ISO时间格式用于审计，但600秒有效期继续以published_at为基准，不能把信号TTL悄悄改成采集时间。
6. **行情接口调用效率**：每个feed轮询会重新校验所有尚有效且已消费的信号；2Hz且普通1–3+有限双仓情况下成本小，可后续先查本地claim减少重复JSON/Pydantic。不能用单一max-id游标破坏个别拒绝/重启语义。

## 审核完成范围与边界

- 两端全部活跃`src`文件均完成阅读或同源逐差异核对：普通22个Python文件，对冲23个Python文件，各1份vendor来源JSON；不把缓存/egg-info当执行源码。
- 两端README、AGENTS、OPERATING_RULES及对冲HEDGE_STRATEGY已读。README与600秒/只消费/无模型流程一致。
- 通过引用搜索确认 `market.py`、vendor Candle和`reachable_tp`在活跃生产导入图中没有被执行服务引用，仍有测试导入。
- 本次修改仅两端`signal_consumer.py`和`tests/test_signal_consumer.py`，主代理统一部署。目标Ruff通过、消费者测试各21项通过；全量Ruff/mypy/pytest由主代理统一执行。
- 本审计未连接Bybit/Telegram真实接口，未运行任何真实交易流程，未验证本次修改部署后的生产运行；不能以源码阅读代替现场验收。

## 本次阅读文件清单及内容哈希

| 路径 | 行数 | SHA256（前12位） |
|---|---:|---|
| `/root/auto-trader-longtime/src/longtime/__init__.py` | 1 | `4472e8709198` |
| `/root/auto-trader-longtime/src/longtime/__main__.py` | 3 | `94cbccdd95dd` |
| `/root/auto-trader-longtime/src/longtime/cli.py` | 99 | `905667c02e52` |
| `/root/auto-trader-longtime/src/longtime/config.py` | 33 | `eb52865bbd4c` |
| `/root/auto-trader-longtime/src/longtime/emergency.py` | 34 | `5251998b5e0a` |
| `/root/auto-trader-longtime/src/longtime/exchange.py` | 199 | `6622e2c12cb8` |
| `/root/auto-trader-longtime/src/longtime/execution.py` | 559 | `778ad2c82bb8` |
| `/root/auto-trader-longtime/src/longtime/market.py` | 81 | `096f60c5067b` |
| `/root/auto-trader-longtime/src/longtime/model.py` | 12 | `abfcad0e00d1` |
| `/root/auto-trader-longtime/src/longtime/monitor.py` | 404 | `8b9d03b74d8b` |
| `/root/auto-trader-longtime/src/longtime/notices.py` | 91 | `7b86f2730ea8` |
| `/root/auto-trader-longtime/src/longtime/polling.py` | 99 | `ba20e8237b9d` |
| `/root/auto-trader-longtime/src/longtime/risk.py` | 161 | `e3ac89b37ea1` |
| `/root/auto-trader-longtime/src/longtime/service.py` | 195 | `eb81bac69cb7` |
| `/root/auto-trader-longtime/src/longtime/settings_controls.py` | 187 | `263d12852b3f` |
| `/root/auto-trader-longtime/src/longtime/signal_consumer.py` | 194 | `515a2b91d0d8` |
| `/root/auto-trader-longtime/src/longtime/store.py` | 211 | `805a9aea1fbe` |
| `/root/auto-trader-longtime/src/longtime/telegram.py` | 697 | `d55b0456e981` |
| `/root/auto-trader-longtime/src/longtime/trading_settings.py` | 67 | `d214a0280ee2` |
| `/root/auto-trader-longtime/src/longtime/transport.py` | 142 | `405fce4f33fe` |
| `/root/auto-trader-longtime/src/longtime/vendor/PROVENANCE.json` | 7 | `30615f4463b0` |
| `/root/auto-trader-longtime/src/longtime/vendor/__init__.py` | 0 | `e3b0c44298fc` |
| `/root/auto-trader-longtime/src/longtime/vendor/models.py` | 46 | `abef7cdddf25` |
| `/root/auto-trader-longtime-02/src/longtime/__init__.py` | 1 | `4472e8709198` |
| `/root/auto-trader-longtime-02/src/longtime/__main__.py` | 3 | `94cbccdd95dd` |
| `/root/auto-trader-longtime-02/src/longtime/cli.py` | 99 | `905667c02e52` |
| `/root/auto-trader-longtime-02/src/longtime/config.py` | 33 | `eb52865bbd4c` |
| `/root/auto-trader-longtime-02/src/longtime/emergency.py` | 34 | `5251998b5e0a` |
| `/root/auto-trader-longtime-02/src/longtime/exchange.py` | 203 | `21fc9b745def` |
| `/root/auto-trader-longtime-02/src/longtime/execution.py` | 564 | `8151d05874bf` |
| `/root/auto-trader-longtime-02/src/longtime/hedge.py` | 581 | `7d61693ce9f1` |
| `/root/auto-trader-longtime-02/src/longtime/market.py` | 81 | `096f60c5067b` |
| `/root/auto-trader-longtime-02/src/longtime/model.py` | 12 | `abfcad0e00d1` |
| `/root/auto-trader-longtime-02/src/longtime/monitor.py` | 434 | `0564a4fd556f` |
| `/root/auto-trader-longtime-02/src/longtime/notices.py` | 100 | `da554b588937` |
| `/root/auto-trader-longtime-02/src/longtime/polling.py` | 99 | `ba20e8237b9d` |
| `/root/auto-trader-longtime-02/src/longtime/risk.py` | 164 | `e4ebc625df68` |
| `/root/auto-trader-longtime-02/src/longtime/service.py` | 195 | `590a7a1fe88b` |
| `/root/auto-trader-longtime-02/src/longtime/settings_controls.py` | 187 | `197e1563d6c3` |
| `/root/auto-trader-longtime-02/src/longtime/signal_consumer.py` | 194 | `515a2b91d0d8` |
| `/root/auto-trader-longtime-02/src/longtime/store.py` | 213 | `a82aafc7c239` |
| `/root/auto-trader-longtime-02/src/longtime/telegram.py` | 654 | `06e9362931ba` |
| `/root/auto-trader-longtime-02/src/longtime/trading_settings.py` | 67 | `32d512f68d36` |
| `/root/auto-trader-longtime-02/src/longtime/transport.py` | 171 | `f029a4d0cf50` |
| `/root/auto-trader-longtime-02/src/longtime/vendor/PROVENANCE.json` | 7 | `30615f4463b0` |
| `/root/auto-trader-longtime-02/src/longtime/vendor/__init__.py` | 0 | `e3b0c44298fc` |
| `/root/auto-trader-longtime-02/src/longtime/vendor/models.py` | 46 | `abef7cdddf25` |
