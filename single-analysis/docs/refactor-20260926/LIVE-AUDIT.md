# 真实环境联调与监督（2026-09-26）

## 本轮记录

- 环境：三套现有 systemd 服务、Bybit Demo 两个独立交易账户、共享生产 `signals.db`；没有改写旧信号或重放旧轮次。
- 轮次：`manual:a304065186f0c26c3bccb83f`，上海时间 13:18:08—13:23:26，约 317.5 秒。运行前普通与对冲交易服务均在监控；分析服务原本暂停，单次手工轮次完成后恢复服务并保留暂停状态。
- TradingView CEX 发现后选取 20 个均衡待审合约；19 个技术/背景证据成功，STONKUSDT 的公开技术页返回 HTTP 404，形成 1 个 `TV_EVIDENCE_FAILURE`。该候选未进入模型；本轮诚实标为 `PARTIAL_ERROR`，其他候选继续分析。后续代码已为逐币页面故障加入持久异常记录，并用回归测试验证。
- TradingView 初选 8 币：ENA、MUBARAK、ZEC、SOXL、ARK、BTC、ETH、BTR；这 8 币的 Bybit 二次证据全部成功。最终普通方向 3 个：MUBARAKUSDT LONG、ENAUSDT LONG、ZECUSDT SHORT。
- 在普通方向之后读取新鲜 LOCKED 双仓快照；本轮额外双仓 ZAMAUSDT 经 TradingView、Bybit 与方向模型分析，输出 SHORT。正常 3 币与额外双仓没有重复发布。
- 共享发布 ID 108—111 共 4 条，各自 `expires_at - published_at = 600` 秒。普通信号的 Bybit 证据在发布时约 63—77 秒，额外双仓约 28 秒。实际发布之前的代码已校验行情来源；审查又补上发布时的证据年龄门限。

## 两端独立消费

| 币种与信号 | 普通交易系统 | 对冲交易系统 |
| --- | --- | --- |
| MUBARAKUSDT LONG，普通 | OPEN | OPEN |
| ENAUSDT LONG，普通 | OPEN | OPEN |
| ZECUSDT SHORT，普通 | SKIP_SIZE_LIMIT | SKIP_SIZE_LIMIT |
| ZAMAUSDT SHORT，额外双仓 | SKIP_NOT_NORMAL_CANDIDATE | HEDGE_DIRECTION_APPLIED |

两端各自持久认领同一组 4 个 ID。ZEC 的跳过来自各自执行端数量限制；ZAMA 没有被普通端当新仓推荐。对冲端确认方向后，ZAMA 双仓进入后续策略状态，现场可见 ZAMA SHORT 仓。正常交易端现场可见 MUBARAK、ENA 新仓；对冲端存在相应未触发的反向条件单，属于原对冲策略，重启时保留并核对原订单 ID。

## 部署后核查

- 三个服务均恢复为 `active`；普通交易系统与对冲系统的信号轮询心跳正常。分析服务仍为原先的 `analysis_paused=true`，不会因这次启动而补跑周期。
- 本轮分析通知从 `PENDING` 变为 `SENT`，只投递一次。两端重启后，该轮各自仍只有 4 个消费记录，没有新旧信号重放。
- 分析库旧 `trades`、`orders`、`hedge_groups` 表原先均为 0 行。在线备份至 `/root/single-analysis-pre-refactor-archive/analysis-before-schema-cleanup-20260926.db` 后移除了三张空表；交易账本及其历史 `cycles` 记录保留。
- 旧验证临时 unit 的失败状态已清除。生产提示词仅保留当前三个版本；旧扫描器、旧模型文档和过时部署文档已移出活动源码树，仓库 `codex-select` 标签仍可用于重构前回滚。

## 验证边界

这次真实轮次发生在补充“TradingView 核心短周期新鲜度”和“逐币异常持久告警”代码之前。补充后的隔离回归覆盖了两项条件；未为了重复制造真实订单而再发布一个新轮次。TradingView 的 HTTP 404 属于外部页面故障，系统按单币排除并报告部分失败，不能保证第三方页面持续可用。
