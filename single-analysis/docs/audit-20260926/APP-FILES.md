# 应用、调度与发布逐文件复审

以 REPORT.md 修复清单和最终验证为准；分项报告中的“待部署”表示源码审核时状态。

| 文件 | 检查与结论 |
|---|---|
| src/analysis_core/__init__.py | 包说明，无启动副作用。 |
| app.py | 两阶段普通方向、快照、双仓去重与发布顺序；错误不伪造；补模型后双仓时效，删除未复验自动解除事故；同轮唯一信号、取消收尾。 |
| bot.py | 授权chat/user双校验，命令事务去重；按钮输入频率保存/取消；轮内详情；仅分析功能。已加入429服务端持久冷却及投递延后。 |
| config.py | SecretStr仅分析Bot；交易凭据不接受；TV和方向模型profile分离，超时30—600秒。 |
| notifications.py | 按原cycle/signal查历史；显示初判与最终置信度；长轮摘要20币限制；不重新发布。 |
| notices.py | 逐币故障与系统故障区分；仅通知。 |
| polling.py | 每Bot抽象socket锁与持久远端长轮询恢复窗；取消/重启不并发长轮询。 |
| scheduling.py | 上海固定锚点、事务claim、恢复/保存/错过周期语义；跨日非整除频率连续。 |
| signals.py | 发布TTL固定600、INSERT OR IGNORE不续期；只读快照同事务，独立观测时间20秒；双向/归属/LOCKED/generation校验。 |
| store.py | SQLite WAL/FULL，单轮单币唯一、事件/outbox；清理无调用重试方法，callbacks表仍用于Bot命令去重。 |
| deploy/single-analysis.service | 单实例入口、退出回收进程组、目录写入限制；当前模型隔离是进程隔离，不是不同Linux账户。 |
| pyproject.toml | src布局，三份原生产提示及新版本作为package-data；标准检查入口。 |
| requirements.lock.txt | 固定部署依赖清单；并未据此宣称所有依赖无漏洞。 |
| .env.example | 仅空Bot与路径配置，没有交易密钥。 |
| .gitignore | 排除环境、运行库、缓存、安装元数据。 |
| README/AGENTS/ARCHITECTURE/WORKFLOW/ACCEPTANCE | 最新流程、600秒及固定周期一致；历史现场报告描述当时状态，不作为当前暂停状态。 |
| ai_crypto_signal_system_development_spec.md | 用户参考规格保留；Binance、30—60min与重试等旧约定均不覆盖最新用户流程。 |
| docs/refactor-20260926/* | 历史方案讨论、真模型验证、真实轮次和先前根因记录；保留，不当作本次部署的新证据。 |

测试逐文件：test_producer覆盖两阶段/去重/故障/时效/快照，test_cross_system_audit与fixtures/cross_system_worker覆盖真实三端类但隔离DB/假交易所；test_analysis_bot/test_user_journeys覆盖按键/权限/周期/详情/持久化；test_polling覆盖轮询重启/取消/并发；test_notifications覆盖摘要与两阶段置信度。数据模型测试见DATA-MODEL-FILES；其标注未完整阅读的test_tradingview和test_model_process余段已由主审补阅。

残余风险：可选聚合资产映射、页面变化、模型质量、日志增长、Bot限流见REPORT；不将测试通过等同于收益验证。源码与文件指纹见MANIFEST。
