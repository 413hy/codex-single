# 现场收尾核查

2026-09-26 15:05上海时间，三套systemd服务均active。分析保存60分钟、未暂停，下次16:00。分析服务等待15:00原进程自动轮次完成后正常重启，未中断、补跑或重放轮次；新版v2/v16从后续新轮次生效。

15:00轮scheduled:c2ffcbd3d17123c6138034ed在15:05:08结束，SUCCESS，约308秒。20个TV币证据全部成功；5个Bybit精细证据；普通最终FIL LONG、LONGXIA SHORT、LTC LONG，额外双仓RARE LONG。四条TTL均精确600秒，通知SENT且attempts=1。

| 币 | 普通端 | 对冲端 |
|---|---|---|
| FIL | OPEN | OPEN |
| LONGXIA | OPEN | OPEN |
| LTC | SKIP_SIZE_LIMIT | SKIP_SIZE_LIMIT |
| RARE（额外双仓） | SKIP_NOT_NORMAL_CANDIDATE | HEDGE_DIRECTION_APPLIED |

两端均独立认领四条，现场无OPEN交易事故。LTC为数量风控跳过，不是分析链路故障。对冲新版独立快照时间已实际写入，读取时约3秒，新HedgeSniffer成功确认LOCKED归属。

该自动轮使用重启前已载入的分析代码和旧生产提示词，不冒称新版提示词已跑完整生产轮。新版通过两次真实模型历史验证（普通和双仓各一次）及离线跨三端回归后切换。未为了验证新提示而额外发布历史或手动重复信号。

分析端仍有14:00的TMX历史BYBIT_REFINEMENT事故，本轮未初选TMX，因此没有采集成功证据来解除；修复代码不代表该历史事件已经复验恢复。保留此记录，不用删除/自动标记恢复掩盖。

结构化现场证据见live-verification.json；模型讨论/验证与逐文件清单见同目录报告。测试：分析221，普通200，对冲232，全通过；三端Ruff/mypy通过。
