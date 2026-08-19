# 阈值复核与激活纠错实施记录

日期：2026-08-19

依据：[当前设计](../specs/2026-08-19-monitoring-review-auto-repair-design.md)

## 已实施

1. `CodexAnalyzer.review_monitoring` 把模型 `REJECTED` 视为正常零规则结果，只执行一次语义复核；技术/Schema 错误仍有限重试。
2. review 后由宿主机械写入同批证据的 primary/confirmation 基线、距离元数据，并把有效期约束到 1800–3600 秒；不改变模型 threshold、comparator 或结构语义。
3. `SignalCycleService.review_and_activate_monitoring` 不再把 `REJECTED` 放入 `errors`；两币都无规则时 activation 为 `NOT_REQUIRED`，监测版本仍正常提交。
4. 激活前固定 threshold 尚未满足时，无论基线向哪侧移动都机械重基线；已经越线/不可执行时才重采该币并最多重审 3 次。
5. 激活重审返回 `REJECTED` 时记录 `NO_QUALIFIED_RULE` 并正常结束；只有真正技术错误耗尽才进入失败通知链。
6. 主 Prompt、review Prompt、项目 Skill、operating contract 与当前文档统一为 0–3 条可选阈值；默认 Prompt 版本升级为 `signal-analysis-v9`。
7. 调度冻结边界改为自然 `:00/:30`，消除 HEMI 15:25–15:30 漏提醒窗口。

## 自动化验收

- 明确 `REJECTED` 只调用模型一次、`exhausted=false`；
- 一币有规则、一币零规则时保留已通过规则且不重试；
- 两币零规则整体 READY、无 errors、activation `NOT_REQUIRED`；
- 激活前条件越线后重审返回零规则，整体 READY 且不通知失败；
- 普通基线变化机械更新；真正越线技术错误仍按币有限纠错；
- HEMI 最近真实 pivot、正式价等值/越界、自然半小时边界均有回归；
- 全量测试、静态检查、真实 shadow 与生产部署证据记录在 `docs/TEST_REPORT.md`。

## 部署检查

1. 使用 `config/system.shadow.yaml` 跑完整 Top5 → Top2 → review → activation 灰盒，禁止 Telegram。
2. 备份生产 SQLite，停止完整 Windows 进程树后启动新版本。
3. 确认 Prompt 版本为 `signal-analysis-v9`、主信号恰好两个、零规则不再产生异常卡。
4. 观察自然半小时边界前旧规则仍有效，边界开始后由新版本覆盖。
5. 方向准确率等获得后续市场数据后单独评估，阈值结果不作为方向正确性的替代指标。
