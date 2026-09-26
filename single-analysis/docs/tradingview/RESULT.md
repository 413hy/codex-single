# 网页接入与验证结果

说明：以下是最初 v12 接入时的历史验证记录，不代表目前生产状态。当前版本和后续验证见 CURRENT-20260923.md。

完成日期：2026-09-23（上海时间）。

- 接入前备份：`/root/single-analysis-backups/pre-tradingview-20260923-104618`。最终所有SHA256校验通过；两份独立数据库快照integrity_check=ok；实际回滚尚未执行。
- 已启用`TRADINGVIEW_ENABLED=true`并重启single-analysis.service加载。分析仍为暂停状态，无运行轮次，未主动恢复或执行cycle。调度实现不变。
- 基线173项测试；完成后202项测试通过，ruff及mypy通过（39源文件）。
- 原选币、vendor证据、旧提示词、调度、信号合同等27个文件逐项与备份SHA256一致。
- 真实网页BTC永续样本：7个周期，每周期79个有效数值字段。未声称所有币种覆盖；其他币种将在各轮实际采集时验证，缺失/错误会失败。
- 真实模型验证通过隔离systemd模型服务运行，输入为新采集的原Bybit证据与TradingView网页参考，普通首选Schema测试。仅1次MODEL_INPUT和1次MODEL_OUTPUT，LONG结果符合Schema；理由引用网页并明确形成中、报价时间不可核验和非独立确认。保留局部订单流反证。结果未发布到生产，signals.db未创建。
- 验证原始记录：`runtime/tradingview-validation-20260923b/{context.json,validation.db,result.json}`。首次序列化失败记录保留在无后缀目录，模型调用为0。
- 保留原v11，v12仅增加TradingView参考合同；真实验证prompt hash与最终v12一致。版本哈希见manifest.json。
- 限制：网页内部接口可能变化；HTTP Date和周期时间不证明最新成交时间。形成中评级不是已确认突破，不能当方向指令或独立证据。单次真实验证不证明策略收益，也不能保证模型今后不会误述事实。
- 2026-09-23后续用户规则覆盖原接入设计中的45秒HTTP/周期时效与streaming硬门槛，并移除方向证据采集后的90秒上限、最近K线及末尾未完成柱的固定45秒门槛。模型仍收到来源标签、开盘时间、采集时间、未确认收盘和报价时间不可验证标记。根目录参考脚本的全市场排行和社区Ideas没有接入选币或判向；现有技术分析页字段更直接对应已选中的BYBIT永续币。
- 回滚时需移除新增TRADINGVIEW_ENABLED配置项，保持现有Bot凭据、运行账本与消费者去重；不得恢复旧信号数据库或补跑。提示词见PLAN.md，操作步骤在备份节点ROLLBACK.md。

## 服务复核

```text
MainPID=1073786
ActiveState=active
SubState=running
analysis_paused=true
```
