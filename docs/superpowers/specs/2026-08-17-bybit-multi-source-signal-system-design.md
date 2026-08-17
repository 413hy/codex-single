# 历史设计草案（已废止）

早期草案曾计划把 CMI、Kronos 和外部研究适配器放入运行链路。完成参考审查和真实采集验证后，该方向已被自建、可移植的公开数据链取代，不能再用于实现或 VPS 部署。

当前权威设计：

- `docs/ARCHITECTURE.md`
- `docs/DEVELOPMENT.md`
- `docs/REFERENCE_REVIEW.md`
- `docs/DEPLOYMENT_VPS.md`

核心变化：交易所公开源数据直接进入本项目自建采集器；CMI 与所有参考仓库都不在运行时；系统只分析和通知，不含账户或执行能力。
