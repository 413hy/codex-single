# 当前文档导航与权威顺序

更新时间：2026-08-19（Asia/Shanghai）

本页是本地继续开发、提交个人仓库和迁移 VPS 时的唯一文档入口。若旧文档与下列当前文档冲突，以用户最新确认、当前源码及本页列出的 2026-08-19 文档为准。

## 1. VPS 部署必读

按顺序阅读：

1. [`VPS_CODEX_HANDOFF.md`](../VPS_CODEX_HANDOFF.md)：Debian VPS Codex 的可执行交接单、禁止事项和最终验收矩阵。
2. [`AGENTS.md`](../AGENTS.md)：Codex 自动读取的仓库级业务边界和部署验收规则。
3. [`README.md`](../README.md)：系统边界、主要能力与本地入口。
4. [`docs/DEVELOPMENT.md`](DEVELOPMENT.md)：完整需求镜像、数据合同、模型职责、阈值生命周期与代码导航。
5. [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)：当前 Top5 → Top2 → 通知 → 阈值复核/纠错 → 实时监测 → 紧急复核架构图。
6. [`docs/DEPLOYMENT_VPS.md`](DEPLOYMENT_VPS.md)：Debian 13/systemd 安装、配置、验证、更新与故障定位。
7. [`docs/TEST_REPORT.md`](TEST_REPORT.md)：当前全量、灰盒和生产验收证据。

VPS Codex 只需按上述文档做环境适配和部署验证，不应重新引入参考仓库、CMI、账户、仓位或自动交易模块。

## 2. 模型、Prompt 与策略合同

- [`docs/MODEL_EVALUATION_2026-08-18.md`](MODEL_EVALUATION_2026-08-18.md)：`sol/terra × medium/high` 三轮评测、默认模型选择和后续定向优化记录。
- [`prompts/signal_analysis_zh.md`](../prompts/signal_analysis_zh.md)：定时 Top5/Top2 与紧急方向分析 Prompt，当前版本 `signal-analysis-v9`。
- [`prompts/monitoring_review_zh.md`](../prompts/monitoring_review_zh.md)：独立阈值审核及按币纠错 Prompt。
- [`.agents/skills/analyze-bybit-ultrashort-signals/SKILL.md`](../.agents/skills/analyze-bybit-ultrashort-signals/SKILL.md)：项目内模型操作手册。
- [`.agents/skills/analyze-bybit-ultrashort-signals/references/operating-contract.md`](../.agents/skills/analyze-bybit-ultrashort-signals/references/operating-contract.md)：模型中心运行合同。

当前模型规则摘要：

- 默认 `gpt-5.6-terra / medium`，单次 300 秒硬超时；
- 模型只输出公开市场方向、四窗口预测、大致止盈展示位、正式方向失效结构及实时复核阈值，不处理账户/交易；
- 结构锚点周期与执行监测周期可以不同，例如完成 1m 可监测已确认 5m/15m 接受边界；
- 阈值只表示方向受到反向结构威胁，需要模型重新分析，不自动等于方向失效；
- 阈值是可选辅助参考。模型明确 `REJECTED` 表示本轮无高质量阈值，是正常结果：不进行语义重试、不记为异常、不发送失败通知；主方向信号不受影响。
- 激活前普通基线变化由宿主机械重基线；只有最新完成柱已经越过阈值、规则不再可执行时才重采/重审该币，模型仍可正常选择不再启用阈值。
- 阈值优先采用最近且脱离普通噪声的真实反向结构锚点；没有独立中间锚点时，仅在正式失效结构价仍可由更快完成柱提供实际提前复核窗口时使用。阈值可等于正式失效价，但不得越过正式失效价才报警。
- 旧规则持续有效到 `:00/:30` 定时任务真正开始；任务边界才冻结旧版，不再存在提前 5 分钟停止造成的监测盲区。

## 3. 当前权威设计与实施记录

- [`2026-08-19 模型中心可靠性设计`](superpowers/specs/2026-08-19-model-centered-signal-system-reliability-design.md)：当前系统总设计。
- [`2026-08-19 模型中心可靠性实施计划`](superpowers/plans/2026-08-19-model-centered-signal-system-reliability-implementation.md)：总重构实施与验收记录。
- [`2026-08-19 阈值复核按币静默纠错设计`](superpowers/specs/2026-08-19-monitoring-review-auto-repair-design.md)：首次审核、按币三次纠错、激活基线变化及最终通知语义。
- [`2026-08-19 阈值复核按币静默纠错实施计划`](superpowers/plans/2026-08-19-monitoring-review-auto-repair-implementation.md)：本轮实现和测试清单。
- [`2026-08-19 方向结构语义设计`](superpowers/specs/2026-08-19-direction-structure-signal-semantics-design.md)：大致止盈仅展示、方向失效与 crossing 的边界。

## 4. 参考材料审查

- [`docs/REFERENCE_REVIEW.md`](REFERENCE_REVIEW.md)：Binance MCP、followers-asm、CMI、prompt 目录等参考资料的取舍。它们只提供设计/源码思路，不是运行时依赖，也没有整仓复制。
- [`docs/requirements/2026-08-17-bybit-multi-source-signal-system-dev-requirements.md`](requirements/2026-08-17-bybit-multi-source-signal-system-dev-requirements.md)：需求演进记录。当前冲突项以本页和 `DEVELOPMENT.md` 为准。

## 5. 历史文档

`docs/superpowers/specs/` 与 `docs/superpowers/plans/` 下日期为 2026-08-18 的文件用于解释演进过程，包括旧目标/freshness、旧阈值和旧架构。它们不是部署或继续开发的权威入口，禁止据此重新引入：

- 目标到达即方向失效；
- 目标驱动阈值；
- 微观指标单独唤醒；
- 少于两个主信号或 WATCH 通知；
- 宿主替模型计算/调节策略阈值；
- 账户、仓位、杠杆或自动下单。

## 6. 交付清单

上传个人仓库或交给 VPS Codex 时至少包含：源码、`config/system.example.yaml`、两个 Prompt、项目 Skill、`VPS_CODEX_HANDOFF.md`、`README.md`、本页、`DEVELOPMENT.md`、`ARCHITECTURE.md`、`DEPLOYMENT_VPS.md`、`TEST_REPORT.md` 和模型评测文档。不要提交 `.env`、`config/system.local.yaml`、`runtime/`、Bot Token 或 Codex 登录状态。
