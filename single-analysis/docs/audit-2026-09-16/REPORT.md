# 三系统深度测试报告（2026-09-16）

## 结论

本轮完成三项目独立全量回归、源码审查、故障注入、HTTP灰盒、跨项目隔离联调、真实行情与真实模型完整周期、在线Demo只读核查及Bot连接检查。最终 **501项通过**。发现并修复两类边界缺陷，新增可复现的回归测试。

不能据此断言所有未来故障或所有外部交易所时序均已穷尽。本轮没有主动在在线Demo账户建立测试仓，没有实际人工点击Telegram客户端，也没有在暂停状态下强行恢复20分钟调度。相关自动化覆盖与真实外部验证分开记录。旧验收报告中的真实订单证据不是本轮新交易证据。

## 范围与确认的需求

|系统|当前职责与验收标准|
|---|---|
|single-analysis|独立选币与模型服务；十份有效候选全部比较，普通相对排序选1—3，不能正常返回空名单；首选Schema只允许LONG/SHORT，允许如实低置信；其他币可SKIP，不二次追问。|
|分析端双仓输入|只读对冲库；快照20秒内；明确归属、LOCKED且双向真实仓位匹配；双仓优先且不占普通名额，同币每轮仅判向一次。|
|共享信号|逐币完成即发布，60秒有效；两个消费者独立持久认领；重复、重启、过期不重放；同币新轮次独立处理。|
|普通交易端|仅消费分析信号；保留资金、杠杆上限、数量精度、TP可达性、原TP/SL、持久订单意图和原目标退出规则。|
|对冲交易端|条件限价反向开仓；部分成交保留TP，全成才撤TP；SKIP保留双仓；有效方向先平逆向、再TP、再对冲；暂停普通开仓不阻断双仓管理；组归属及generation匹配。|
|分析Bot|精简通知；底部虚拟键盘提供币种完整理由、状态、最近分析、频率、异常；暂停/恢复单个位置按状态显示；chat/user授权、update去重、持久发送预算。|
|隔离|交易端不调用模型、不修改分析频率；分析端无交易凭据配置；三个独立Bot；Demo私有接口固定，凭据不输出。|

旧OPERATING_RULES中的6份证据、0—3候选、交易端分析频率等已被覆盖，不作为当前验收标准。对冲目录保留的原Executor SL回归不等于对冲App使用SL，因此本次额外测试实际HedgeExecutor及真实App路径。

## 全量与静态检查

|项目|最终全量|静态检查|
|---|---:|---|
|分析|137 passed|ruff src tests；mypy src：通过|
|普通交易|171 passed|ruff src tests scripts；mypy src：通过|
|对冲交易|193 passed|ruff src tests scripts；mypy src：通过|

systemd-analyze verify：三个部署unit检查通过。在线三个服务active/running，检查时NRestarts=0。

分析与对冲最终JUnit结果见本目录analysis-tests.xml、hedge-tests.xml。普通171项为本轮实测工具输出，无源码改动后重复运行。运行快照见runtime-snapshot.json；源码及测试SHA256见source-manifest.json。未安装覆盖率工具，故不提供虚构的行/分支覆盖率百分比。

## 本轮发现与修复

### 1. 模型启动窗口的取消竞态

初次并发全量运行出现131通过、1失败：取消后子进程仍存在。进一步用真实子进程及受控启动完成屏障稳定复现：进程已创建，但create_subprocess_exec尚未把Process返回调用方；原取消清理只包围communicate，漏掉启动窗口。

修复：通过独立启动任务及shield保留进程句柄，取消发生时等待获取句柄、杀进程组并wait回收，写MODEL_INTERRUPTED后重新抛出取消。不改模型、Prompt或Schema，不增加模型调用。

验证：新增确定性复现用例修复前失败、修复后通过，单项连续20次通过；模型进程26项通过；最终分析全量通过。真实行情模型周期也使用修复后的代码完成。

### 2. 双仓归属校验缺口

HedgeSniffer原来校验OPEN、双向槽位、快照数量与均价，但未显式拒绝owned=0，也未核对组symbol与仓位账本symbol。故障注入两种畸形账本均能被误纳入，修复前两项稳定失败。

修复：对每条仓位增加owned==1及symbol==group.symbol约束，不一致按故障失败，不静默采用。修复后故障注入、正常嗅探、过期快照以及实际对冲监控生成的快照联调均通过。

## 深度与灰盒覆盖

- 真实模型子进程：Schema、环境隔离、无交易/Bot密钥继承、失败、超时、取消、输出缺失、服务故障停止后续调用。
- 筛选及发布：十份证据、覆盖/引用校验、1—3排名、首选强制多空、空名单失败、双仓优先/去重、不占名额、逐币发布、汇总单条、有效期不可延长。
- 普通HTTP灰盒：实际签名、transport、Exchange、Executor、SQLite、Monitor及Telegram；丢开仓应答、重启、保护拒单和重试预算、账户异常、结算事务失败回滚、暂停竞态。
- 对冲实际HTTP灰盒（本轮新增两项）：LONG/SHORT镜像，以实际HedgeExecutor通过HMAC签名与HTTP传输，开仓、反向全成、撤TP锁仓、恢复不重复提交、反向重新判向、下一代重新挂单；无SL，对冲仍限价。
- 对冲状态机：部分成交30秒前后通知、全部成交撤TP、TP先成交、撤单期间补成交、未知应答、旧代次拒绝、组内锁复核、连续8代全多/全空/交替24次判向恢复。
- 通知HTTP灰盒（本轮新增）：真正调用AnalysisBot.call/poll/deliver、MockTransport替代Telegram远端，验证底部reply keyboard、完整理由、重启后按钮映射、暂停/恢复、重复update不重复发送。未声称用户真的点击了客户端。

## 跨系统隔离联调

新增test_cross_system_audit.py及cross_system_worker.py，分别启动两交易项目自己的Python进程，加载各自实际App、Consumer、Executor、Store。只替换交易所和行情为明确模拟fixture，账本及信号库全在临时目录。

实际SignalBus发布普通LONG、SKIP、额外双仓、过期信号；两端独立消费，验证开仓保护单、SKIP/过期拒绝、额外双仓不误开普通仓、重复tick与重建App不重放。对冲进入LOCKED后，由实际HedgeSniffer读取监控快照，再由实际SignalBus发布SHORT复核信号；暂停普通开仓期间仍成功解锁、进入下一代，重启无重复订单，再次锁仓后旧generation被拒绝。全流程无SL。

该用例依赖本机两个兄弟项目路径；缺少兄弟目录时显式skip。本轮存在这些目录且实际通过。

## 本轮真实模型与消费验证

隔离运行目录：`/root/single-analysis/runtime/audit-2026-09-16/`，未写在线signals.db、未投递在线Bot测试通知。

周期：`manual:143d76b245ea16652e7c8ed8`，SUCCESS。

- 10份有效候选证据；一次筛选选ARBUSDT、PUFFERUSDT、POWERUSDT。
- 三次方向调用，三个不同signal_id，无二次追问。
- 首选ARB Schema为LONG/SHORT，实际LONG；PUFFER LONG；POWER SKIP。
- 当前direction_v11.md SHA256与全部方向输入事件匹配；Prompt未修改。
- 两套实际消费者在隔离账本记录ARB/PUFFER为SKIP_PAUSED、POWER为SKIP_MODEL；交易所方法调用为0。
- 首次消费脚本的“零调用”断言误把AsyncMock初始化布尔检查算作接口调用；修正为检查method_calls后复核，已持久认领信号没有重放。该脚本问题不是交易端下单故障。

证据：该隔离目录summary.json、analysis.db、signals.db、两个`*-receipts.json`及独立消费者账本。

## 在线状态与保留问题

本轮两次真实Demo只读核查：两账户读取成功、均0仓位/0挂单；两个交易暂停开关true，分析暂停true。三个SQLite quick_check为ok；对冲监控与两个消费者心跳新鲜。

三个Bot分别通过真实getMe/getWebhookInfo，ID互不相同，无webhook冲突；没有调用在线getUpdates抢占服务消息。

普通端仍有5条历史OPEN入场异常：对应SOXL、BZ、KORU、LSK等历史订单，五条均ENTRY REJECTED、交易UNFILLED；另有1条历史告警outbox FAILED（attempts=2）。本轮没有自动重发过时通知、删除账本或把它们伪装为已解决。分析端和对冲端当前无OPEN异常、无FAILED通知。

## 实际覆盖边界

1. 本轮无新增在线Demo订单；真实成交与清理来自旧报告的历史证据，本轮新验证为隔离执行链及在线只读状态。
2. 部分成交、撤单瞬间补成交、网络丢应答用故障注入验证，不能声称交易所本轮自然发生了这些情况。
3. 底部键盘的协议载荷、处理、重启和授权经过HTTP灰盒；最终手机/桌面客户端视觉和用户真实点击未人工验收。
4. 在线分析保留暂停，本轮未恢复并等待多个20分钟自然周期。频率、边界、去重相关已有自动化覆盖，旧常驻证据仅属历史。
5. 功能正确不代表方向预测有效或保证收益；无覆盖率百分比、无“所有路径已穷尽”承诺。
