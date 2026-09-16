# 三系统重构与Demo联调验收

日期：2026-09-15。范围：`/root/auto-trader-longtime`、`/root/auto-trader-longtime-02`、`/root/single-analysis`。

## 当前结论

代码重构、共享发布/消费链路、四轮真实Demo交易场景，以及最新“十选1—3、首选必须明确方向”真实模型调用均已验证。**新分析Bot尚未配置，分析服务未常驻启动，不能称三个系统已全部完成上线。**

两个交易服务active/enabled、NRestarts=0，保留原暂停新开仓状态；已有仓位管理与信号监听继续运行。测试结束时两账户均无仓位、无挂单。新分析服务installed但inactive/disabled；需在其`.env`填入独立Telegram凭据后完成真实Bot验收并启用。不会把测试期间的手动分析周期描述为已运行20分钟常驻调度。

## 最终分工与策略

- 分析项目独自选币、调用模型、管理默认20分钟频率和分析通知。两个交易项目无扫描/模型调用代码，也无模型配置或频率编辑功能。
- 分析系统从对冲系统只读获取20秒内的新鲜交易所快照，仅纳入归属明确、已全成并撤掉止盈后的LOCKED组。交易监控每5秒更新。
- 最新要求：10份有效候选证据全部横向比较，按小长线相对方向置信度选1—3。普通首选必须LONG/SHORT；其他入选和额外双仓可SKIP。首选与双仓重合只调用一次，额外双仓不占普通名额。
- 低置信度如实披露；旧绝对筛选门不再清空名单。必要行情/模型错误或首选违规SKIP均记录失败，不伪造方向，不同轮补问。
- 每币完成即发布60秒信号。两个消费者只读同一个signals.db，各自持久认领，重启/重复/过期不重复交易。同币种不同轮次的新信号正常独立接收。
- 普通交易策略与凭据保持独立；对冲默认TP0.5U、反向限价距离1.7U、3x、10U保证金，5x上限保留。
- 对冲全成才撤TP；部分成交保留TP。有效判向先平逆向仓，先挂顺向TP，再挂新对冲。TP在对冲提交前已成交则不再开对冲；对冲已提交则先撤单确认终态，再按实际余仓清理。

## 自动化与静态检查

|项目|全套回归结果|最后局部修改后的相关复核|
|---|---|---|
|原交易系统|170 passed|信号消费者12 passed；Ruff/mypy通过|
|对冲交易系统|187 passed|信号消费者与对冲状态机29 passed；Ruff/mypy通过|
|分析系统|120 passed|Bot、发布器、筛选合同与模型进程57 passed；Ruff/mypy通过|

模型隔离/超时、Schema校验、双仓去重和名额、无方向等待、模型错误停止、信号有效期、丢失下单应答不重放、同币种下一轮、代次锁内核对、部分成交30秒通知、撤单成交竞态及恢复预算均有覆盖。部署unit校验及Python编译检查已执行。源文件SHA256清单见`runtime/live-hedge-test/source-manifest.json`。

## 真实模型与两端收信

1. 旧共享架构初次完整周期 `manual:82bf28fd8d47da27f619f5fb`：1次筛选、UNI/AKE各1次方向，成功发布；两交易端都记录SKIP_PAUSED。
2. 带真实XRP双仓周期 `manual:73d998a4397d431948b8b3f9`：嗅探到XRP；普通LSK/AKE/POWER仍占3个普通名额，XRP额外加入且优先分析一次。XRP为SKIP，实际保留双仓。POWER调用遇到`workspace out of credits`，本轮按PARTIAL_ERROR结束，没有二次追问。
3. **最新规则真实周期** `manual:9e20195087c18ebc1c623304`：接口恢复，10份候选证据全部发送，一次筛选选AKE/ARB/POWER；首选AKE的方向Schema只允许LONG/SHORT，实际输出LONG，ARB/POWER输出SKIP。周期SUCCESS，每币仅一次方向调用；两交易端分别记录AKE为SKIP_PAUSED，其他为SKIP_MODEL。新提示词输入SHA256与当前文件逐一匹配，旧周期没有冒充新版验收。

证据：`runtime/live-hedge-test/model-evidence-summary.json`、analysis.db的SCREENING_EVIDENCE/SCREENING_MODEL_INPUT/SCREENING_RESULT/MODEL_INPUT/MODEL_OUTPUT事件，以及两交易项目的`runtime/shared-signal-new-policy-receipts.json`。

## 四轮真实Demo场景

用户明确授权主动建立Demo双仓。强制建仓/测试信号均带`demo-fixture`或`demo-test`标记；测试信号注明“非模型分析结果”，normal_candidate=false，普通交易端不得据此开仓。没有修改或重放真实模型历史信号。

|轮次|设置与实际结果|
|---|---|
|1：双仓判向|XRP多空各21.3枚、3x；真实模型SKIP保持双仓。显式SHORT测试信号因TP不可达保持双仓；随后LONG测试信号平空，挂多TP1.4339，再挂21.3枚开空对冲，触发价=限价1.3306。没有平仓SL。|
|2：真实对冲全成|仅测试仓的冻结对冲距离改为0.01U，默认设置未改。真实条件限价单从Untriggered变为Filled，21.3枚全部成交，随后撤TP并进入LOCKED；零挂单，双仓各21.3枚。交易Bot仅一次全成通知，SENT/attempts=1。|
|3：TP在新对冲提交前成交|仅测试仓TP距离0.005U，对冲恢复1.7U。LONG重新判向平空后，TP立即成交，系统未提交新对冲；子仓记UNFILLED、组DONE。信号仍有效时重启，订单/信号数量不变。|
|4：空头镜像与撤余单|新建第二组同量XRP双仓；仅测试仓TP距离0.02U，对冲1.7U。SHORT信号先平多，基准1.4102，下方TP1.4092，上方开多条件限价1.49。空仓TP成交后，待触发对冲被撤至Deactivated，cumExecQty=0；组DONE，无余仓。|

第一组ID：`385e9209e5d0d1262b13c64f`；第二组ID：`a30a64d5874b637d7f58be9c`。

关键订单：
- 第一轮TP：`8b52421b-bd47-4b42-a1b5-0d2153725727`；对冲：`1eb961f7-6871-47f0-8e99-31b27c392064`。
- 第二轮真实全成对冲：`7eda471a-90f8-4ab5-ad3d-5d610dfb5558`。
- 第四轮空头TP：`7362535d-99d1-4bcf-9385-f5ecfb3c85ea`；已撤开多对冲：`884af6c8-d0f9-49b3-a984-de53f63d3ba4`。

详细证据在对冲项目`runtime/shared-analysis-live-test/`：setup.log、after-long.json、short-trigger-start.json、short-trigger-check.json、before-restart.json、after-restart.json、after-tp-first.json、setup-round4.log、round4-check.json、cleanup-rounds-1-3.json、cleanup.json和final-exchange.json。所有真实订单意图、状态和回执留在原trader.db。

## 清理、账本与隔离

- 对冲旧订单1条，最终15条；新增14条均为本次测试意图。测试5笔已平仓，3个子仓UNFILLED，两个组DONE。未删除任何旧周期/订单/成交记录。
- 普通系统原有740条订单仍为740条；所有旧下单尝试次数保持。两账户及交易Bot凭据互不相同，未输出任何密钥。
- 两账户实际仓位与挂单都为0；两个原暂停开关保持true。普通系统历史交易异常保留，未冒称历史问题全部消失；对冲当前无OPEN异常。
- 逐笔交易所USDT资金流水合计 **-0.11645898 USDT**，与五笔测试账本net_pnl之和完全一致；10笔成交，未发生测试时段资金费。`transaction-audit.json`保留逐笔比对。账户totalWalletBalance是USD估值，不能直接用其前后差代替USDT成交净损益。
- 默认开仓设置再次确认：margin10、leverage3、tp0.5、sl1.7、revision0；测试小距离仅存在已结束测试仓的冻结记录中。
- 两项目`runtime/signal-split-final.json`保存账户/订单/暂停/旧意图审计；原重构备份位于各自`runtime/backups/before-signal-split-*`。

## 未覆盖或仍待完成

- 新分析Bot凭据仍缺失，尚未实测该Bot的真实收发、按钮和常驻20分钟调度。相关权限、频率预览确认、重复回调、持久投递已通过自动化测试；两个现有交易Bot真实通知已成功。
- 超过30秒的真实部分成交、撤单瞬间再成交、网络丢应答等不可稳定制造的分支依靠自动化故障注入验证，没有冒称交易所真实发生。
- 第二/三/四轮使用测试仓小距离缩短等待，证明真实订单生命周期；不声称自然市场已经完整走出默认1.7U亏损再对冲的全过程。
- 小长线方向的最低输出数量已验证；模型预测效果和收益率不是本次功能验收的结论。

## 追加深度验证与Bot上线（2026-09-15）

本节覆盖前文“Bot未配置/服务未启动”的旧结论：独立分析Bot已配置并通过getMe/webhook检查，用户名codex_krak_bot，与两交易Bot凭据不同；single-analysis.service已enable并启动。

- 追加3组真实Demo双仓：两次多头解锁挂单，一次空头TP不可达保持双仓；包含带仓重启、SKIP、重复信号防重放与最终清理。最终6笔实际仓位全部CLOSED、未成交子仓UNFILLED、组DONE，无残留仓位或挂单。证据：对冲项目runtime/repeat-depth/。
- 新增连续8代状态恢复测试：全多、全空、多空交替共24次重新判向，全部通过。
- 发现并修复信号处理中重启后记录滞留SELECTED的问题：取消任务时持久记INTERRUPTED，仍只恢复原订单意图，绝不重放方向。两个交易端各自13项消费者回归通过；已发生的两条测试中断记录以审计事件标明后修正，无历史信号重跑。
- 本轮全套回归：分析120、普通170、对冲187通过；随后新增通知/菜单测试后分析126通过，新增多代测试3项通过。
- 追加两轮真实模型完整周期均SUCCESS：manual:168db186d16e1217fed7c3c0、manual:fcb110dfedc3920939887dec；正常首选明确方向，两端按各自暂停与仓位规则处理。
- 分析Bot真实投递积压分析记录成功。新版汇总样式sendMessage成功，message_id=30、attempts=1。菜单授权、频率预览确认、过期预览、暂停恢复、重复update、持久offset、两次失败预算及手动重发均有测试。
- 菜单业务流程用明确标记的隔离测试库验证，并真实投递测试结论；不冒称模拟update为用户实际点击。生产频率未被测试更改，保持默认20分钟。

### 最新Telegram布局

同一周期只排队一条cycle通知，不再逐币发消息再补完成通知。显示北京时间分析起止时段、约几分钟、完成/部分失败/中断状态；按币列出首选、双仓、方向及简短理由。最近分析展示同样的汇总。信号仍逐币完成即发布，通知合并不延迟交易信号。失败告警仍单独发送，避免错误被藏在汇总中。长结果有长度预算，防止超过Telegram限制。

真实部分成交超过30秒和撤单瞬时补成交仍属自动化故障注入覆盖；不能保证未来外部服务永不报错。真实接口异常会保留证据、限次重试或按原订单恢复。

### 常驻自动汇总与通知恢复证据

- 常驻周期`1200:1491236`为SUCCESS，AKE LONG、FIL SHORT、INJ SHORT；只有一条cycle汇总，Telegram message_id=32、SENT、attempts=1，显示北京时间22:51—22:54、约2分钟。证据runtime/live-hedge-test/automatic-summary-evidence.json。
- Telegram正式命令菜单setMyCommands成功安装。
- 隔离测试注入两次网络失败，达到预算后FAILED；模拟用户明确/retry_delivery后恢复真实sendMessage成功，message_id=33、attempts=1。再次deliver没有重发。证据bot-recovery-result.json；没有把注入失败冒称真实Telegram宕机。
- 新增队首失败不永久阻塞后续通知测试：两次失败后首条FAILED、下一条SENT。菜单/通知/发布集成23项通过。

### 最终追加轮次

追加完整模型周期manual:42823daedc522cd0d75960cc、manual:d87c1f8cd074c57a226d2bd3均SUCCESS。连同前两轮与常驻自动轮，本次共验证5轮成功分析；证据repeated-cycle-evidence.json保留信号及汇总投递回执。模型测试结束后已恢复常驻服务，下一自然时间桶继续自动运行。

对冲多代/策略/消费者33项回归通过，三个服务active/enabled、NRestarts=0。所有本次测试仓位结算完成、无挂单，交易默认参数未改，两交易端继续保留原暂停新开仓状态。普通系统存在1条本次测试前历史告警投递FAILED，未冒称该历史记录消失，未重发过时通知；本次分析与对冲通知正常。


## 2026-09-16 深度复核

最新独立验收见 [三系统深度测试报告](audit-2026-09-16/REPORT.md)。本轮501项通过，修复模型启动取消竞态及双仓归属检查缺口；通知现使用对话框底部虚拟键盘、精简汇总和按币查看详情。历史异常和真实测试边界见新报告。
