> 原项目复制资料：以下部署状态、账户验收和旧止损策略不代表新系统。新系统以 README.md 和 HEDGE_STRATEGY.md 为准。

# 2026-09-11 全系统逐文件审核

范围：76个原项目源码、测试、文档、依赖与部署文件全文分8批交实际gpt-5.6-terra/medium审核；新增tests/test_system_audit.py随修复交跨文件复审。密钥、认证文件、依赖缓存不读取；运行账本以只读状态核查，保留全部历史。

本轮确认并修复：监控过早解除故障及更新心跳；外部仓位保护持续检查与owned槽隔离；通知发送前持久化预算及并发锁；单笔验收禁止另起周期的旧回调；前10候选唯一性；扫描K线重复/逆序/非整周期拒绝；OI周期与时效核验；20柱成交额基准纠偏；基准行情冻结截止；REST逐笔样本覆盖说明。未启用流模块修正事件心跳和乱序过期处理，未启用筛选周期类补哈希格式验证。

策略保持一周主趋势、12h辅助、TP24h完整分钟累计>300秒；固定10U保证金/新仓3x/TP0.5U/SL4U，不修改已有保护单或重试预算。

验证：203项pytest、Ruff、mypy通过。模型首轮意见存在跨层误判：筛选层独立六周期不能套方向八周期；--disable工具名不是启用；Bybit无原生10m；scanner全排名由screening截前10。每条实际意见及处置见runtime/system-audit-20260911/dispositions.json和各batch JSON。

已知边界：真实费用的两个来源均缺失时保持INTENT、告警并核对，不估算实付手续费；已有有效累计费用但逐笔明细尚未齐是不同状态。Telegram模糊响应下不保证exactly-once，最多两次自动尝试持久化；人工重试仍可单次发起。Codex CLI可使用既有认证状态，但交易模型无工具且不继承交易密钥，不声称整个OS完全隔离。

## 逐文件清单

|文件|首轮证据|处置|
|---|---|---|
|`AGENTS.md`|batch-0.json|本次未发现需修改问题|
|`README.md`|batch-0.json|本次未发现需修改问题|
|`deploy/bybit-longtime.service`|batch-0.json|已审；跨层疑点按实际入口核对|
|`docs/ACCEPTANCE.md`|batch-0.json|本次未发现需修改问题|
|`docs/CREDENTIALS.md`|batch-0.json|本次未发现需修改问题|
|`docs/FILE_AUDIT.md`|batch-0.json|本次未发现需修改问题|
|`docs/MODEL_CONTRACT.md`|batch-0.json|本次未发现需修改问题|
|`docs/OPERATING_RULES.md`|batch-0.json|本次未发现需修改问题|
|`docs/REUSE.md`|batch-0.json|本次未发现需修改问题|
|`pyproject.toml`|batch-0.json|本次未发现需修改问题|
|`requirements.lock.txt`|batch-0.json|本次未发现需修改问题|
|`scripts/accept_demo.py`|batch-0.json|已修改并回归，详见处置|
|`scripts/live_full_cycle.py`|batch-0.json|本次未发现需修改问题|
|`scripts/observe_acceptance.py`|batch-0.json|本次未发现需修改问题|
|`scripts/public_smoke.py`|batch-0.json|本次未发现需修改问题|
|`scripts/review_screening.py`|batch-0.json|本次未发现需修改问题|
|`scripts/review_weekly_direction.py`|batch-0.json|本次未发现需修改问题|
|`scripts/review_weekly_tp.py`|batch-0.json|本次未发现需修改问题|
|`scripts/telegram_ids.py`|batch-0.json|本次未发现需修改问题|
|`scripts/validate_readonly.py`|batch-0.json|本次未发现需修改问题|
|`src/longtime/__init__.py`|batch-0.json|本次未发现需修改问题|
|`src/longtime/__main__.py`|batch-0.json|本次未发现需修改问题|
|`src/longtime/cli.py`|batch-1.json|本次未发现需修改问题|
|`src/longtime/config.py`|batch-1.json|本次未发现需修改问题|
|`src/longtime/emergency.py`|batch-1.json|本次未发现需修改问题|
|`src/longtime/exchange.py`|batch-1.json|本次未发现需修改问题|
|`src/longtime/execution.py`|batch-1.json|已审；跨层疑点按实际入口核对|
|`src/longtime/indicators.py`|batch-1.json|本次未发现需修改问题|
|`src/longtime/market.py`|batch-1.json|已审；跨层疑点按实际入口核对|
|`src/longtime/model.py`|batch-1.json|已审；跨层疑点按实际入口核对|
|`src/longtime/monitor.py`|batch-2.json|已修改并回归，详见处置|
|`src/longtime/notices.py`|batch-2.json|本次未发现需修改问题|
|`src/longtime/prompts/direction.md`|batch-2.json|本次未发现需修改问题|
|`src/longtime/prompts/screening.md`|batch-2.json|本次未发现需修改问题|
|`src/longtime/risk.py`|batch-2.json|本次未发现需修改问题|
|`src/longtime/screening.py`|batch-2.json|已修改并回归，详见处置|
|`src/longtime/service.py`|batch-2.json|本次未发现需修改问题|
|`src/longtime/store.py`|batch-2.json|本次未发现需修改问题|
|`src/longtime/telegram.py`|batch-3.json|已修改并回归，详见处置|
|`src/longtime/transport.py`|batch-3.json|已审；跨层疑点按实际入口核对|
|`src/longtime/vendor/PROVENANCE.json`|batch-3.json|已审；跨层疑点按实际入口核对|
|`src/longtime/vendor/__init__.py`|batch-3.json|本次未发现需修改问题|
|`src/longtime/vendor/models.py`|batch-3.json|已审；跨层疑点按实际入口核对|
|`src/longtime/vendor/public.py`|batch-3.json|已审；跨层疑点按实际入口核对|
|`src/longtime/vendor/scanner.py`|batch-3.json|已修改并回归，详见处置|
|`src/longtime/vendor/signal/__init__.py`|batch-3.json|本次未发现需修改问题|
|`src/longtime/vendor/signal/domain/__init__.py`|batch-3.json|本次未发现需修改问题|
|`src/longtime/vendor/signal/domain/enums.py`|batch-3.json|已审；跨层疑点按实际入口核对|
|`src/longtime/vendor/signal/domain/market_requirements.py`|batch-3.json|本次未发现需修改问题|
|`src/longtime/vendor/signal/domain/models.py`|batch-4.json|已审；跨层疑点按实际入口核对|
|`src/longtime/vendor/signal/evidence/__init__.py`|batch-4.json|本次未发现需修改问题|
|`src/longtime/vendor/signal/evidence/builder.py`|batch-4.json|已修改并回归，详见处置|
|`src/longtime/vendor/signal/providers/__init__.py`|batch-4.json|本次未发现需修改问题|
|`src/longtime/vendor/signal/providers/bybit.py`|batch-4.json|已审；跨层疑点按实际入口核对|
|`src/longtime/vendor/signal/providers/cross_exchange.py`|batch-4.json|已审；跨层疑点按实际入口核对|
|`src/longtime/vendor/signal/providers/deep_market.py`|batch-5.json|已审；跨层疑点按实际入口核对|
|`src/longtime/vendor/signal/providers/market_context.py`|batch-5.json|已修改并回归，详见处置|
|`src/longtime/vendor/signal/providers/public_streams.py`|batch-5.json|已修改并回归，详见处置|
|`src/longtime/vendor/signal/screening/__init__.py`|batch-5.json|本次未发现需修改问题|
|`src/longtime/vendor/signal/screening/models.py`|batch-5.json|已修改并回归，详见处置|
|`src/longtime/vendor/signal/screening/validation.py`|batch-5.json|已审；跨层疑点按实际入口核对|
|`tests/conftest.py`|batch-5.json|已修改并回归，详见处置|
|`tests/test_audit_regressions.py`|batch-5.json|已审；跨层疑点按实际入口核对|
|`tests/test_blackbox.py`|batch-5.json|本次未发现需修改问题|
|`tests/test_execution.py`|batch-6.json|已审；跨层疑点按实际入口核对|
|`tests/test_graybox.py`|batch-6.json|已审；跨层疑点按实际入口核对|
|`tests/test_indicators.py`|batch-6.json|本次未发现需修改问题|
|`tests/test_infrastructure.py`|batch-6.json|已审；跨层疑点按实际入口核对|
|`tests/test_market_direction.py`|batch-6.json|已修改并回归，详见处置|
|`tests/test_model_process.py`|batch-6.json|已审；跨层疑点按实际入口核对|
|`tests/test_risk.py`|batch-6.json|已审；跨层疑点按实际入口核对|
|`tests/test_scanner.py`|batch-6.json|已审；跨层疑点按实际入口核对|
|`tests/test_screening.py`|batch-7.json|已修改并回归，详见处置|
|`tests/test_signal_deep_market.py`|batch-7.json|已审；跨层疑点按实际入口核对|
|`tests/test_signal_evidence_builder.py`|batch-7.json|已修改并回归，详见处置|
|`tests/test_telegram_controls.py`|batch-7.json|已审；跨层疑点按实际入口核对|
|`tests/test_system_audit.py`|final-review.json|新增故障回归，跨文件复审|

最终模型复核、真实完整流程与部署结果见本文件末尾及docs/ACCEPTANCE.md。审核不等于收益保证。

## 修复后模型确认

实际gpt-5.6-terra/medium经跨文件复审与争议核实，最终blocking_issues为空，结论为“基于提供的最终源码与测试，未发现剩余可复现的阻断性合同违反”。证据runtime/system-audit-20260911/final-review.json及final-review.db。不能把模型未执行测试描述为它亲自跑通测试；测试由维护端实际执行，203项通过。

复审追加确认并修复INTENT崩溃恢复告警缺口：若进程在首次ENTRY告警前中断，监控继续按原ID核对但此前可能缺告警；现在False结果产生同一去重ENTRY/reconcile告警，不重发开仓。累计真实费用已经确认、仅逐笔明细待补的OPEN路径保持正常补齐。

滚动24h边界：非整分钟时只有1439根完整已收盘分钟完全位于窗口内，另两端为不完整分钟，均不计时；不会为凑1440根计入窗外时间。已新增固定时钟回归。

## 真实流程与文案复核

首次完整只读周期audit-full:1789098774116207517正常完成，6份证据选3币；NIULAI/RAYDIUM为LONG，MARSCOIN为SKIP。两项LONG均未通过TP可达性门槛。结论复核发现高成交K线范围与逐价成交区、盘中触及与收盘确认措辞混用，因此人工升级direction-v9-weekly-evidence-20260911，保留原Prompt与调用证据。

v9完整只读周期audit-full:1789099052378075895正常完成，6份证据选2币（NIULAI/RAYDIUM），两项LONG仍因TP不可达跳过。实际Terra逐条复核均PASS，无不支持的声明；维护端另核对日成交量、成交额及各主周期收盘值。完整一周主窗口、12h辅助、Prompt哈希、选币数量、零私有写入检查通过。两次均0新订单、0新incident；未触发后续PRE_ENTRY_ACCOUNT资金门槛和下单路径，不能声称此次实测验证了新增成交或保护单创建。相关资金/下单路径由回归测试覆盖。

原3份样本v9重放保留于replay-v9.json：MARSCOIN从SKIP变SHORT，说明模型有决策波动；RAYDIUM重放引用30m较早已收盘1.6117，最新实际为1.5903，措辞缺少时间限定。新行情完整流程正确区分了两者。不得将一次语义复核PASS宣传为模型不会误判、全部历史结论稳定或收益改善。

停服时发现新周期1490916已开始且无订单意图，正常systemd停止取消了扫描。补充cycle取消记录INTERRUPTED并重新抛出CancelledError的回归，原周期ID保留、不能重复claim；运行前备份账本。异常kill留下的历史RUNNING不等于实际进程仍运行，应依据服务及交易所状态判断，不删除记录强制重跑。

部署复核：仅本服务systemd重启后active/running、NRestarts=0；10个原仓与20张原保护单完全一致，无缺保护或在途意图。原2项协议未签署ENTRY异常保留。周期1490916中断有账本备份及MAINTENANCE_CYCLE_INTERRUPTED事件，未删除/重跑。最终取消处理和v9文案实际模型复审无阻断项（cancellation-review.json）。
