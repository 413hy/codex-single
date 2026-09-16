# single-analysis

两套Bybit Demo交易系统共用的自主信号分析项目。

## 工作流程

1. 每轮读取 `/root/auto-trader-longtime-02/runtime/trader.db` 的交易所仓位快照、心跳与对冲组，筛出LOCKED双向仓位。快照超过20秒则报警，本轮不静默忽略双仓。
2. 全市场正常选币：前10候选全部采集，10份有效证据横向比较小长线方向，普通入选1—3个。至少选出相对方向置信度最高的一名，允许如实标注低置信度。完全不读取普通止盈止损系统的仓位。
3. 双仓币种与普通入选去重，双仓优先方向分析，不占普通名额。给模型明确重点判向标记。普通排名第一必须输出LONG/SHORT；其他入选币与额外双仓允许SKIP。首选与双仓重合时合并一次调用，遵守首选必判要求。每币每轮最多一次方向调用。
4. 每个结果完成后立即写入 `runtime/signals.db`，两套交易服务只读接收。信号有效期为发布后60秒，额外双仓币不会被当成普通开仓推荐。
5. 交易服务按自身暂停、账户、仓位、资金及止盈可达性规则执行。原止盈止损/对冲策略保留。

默认统一20分钟，按实际启动时间间隔调度，下一轮时间持久保存；重启不补跑历史周期。空闲时恢复立即开始一轮；重复恢复不刷新计时，正在分析时恢复不另开一轮。分析耗时超过间隔时，结束后重新等待一个间隔。频率保存后从保存时刻重新计时，不中断当前分析。

## 配置与Bot

在本项目 `.env` 填写独立 `TELEGRAM_TOKEN`、`TELEGRAM_CHAT_ID`、`TELEGRAM_USER_ID`。没有交易密钥配置。
Bot对话框底部虚拟键盘：分析状态、最近分析、分析频率、分析异常，以及按当前状态显示的单个暂停/恢复按钮。原有文字命令继续可用。
频率修改：点击「分析频率」→ 直接回复分钟数（1—1440）→ 点击「保存」或「取消」；10分钟有效。仅绑定chat与user可操作。原指令保留作为备用。
`/retry_delivery`重发失败通知。分析故障在下个周期使用新行情复核，不强行重试当前币种。

## 运行与验证

```bash
cd /root/single-analysis
.venv/bin/single-analysis check
.venv/bin/single-analysis cycle  # 真实分析并发布信号，可能被正在运行且未暂停的交易系统执行
.venv/bin/single-analysis serve
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/pytest -q
```

独立服务文件：`deploy/single-analysis.service`。`serve`要求新Bot已配置；`cycle`可用于独立真实模型联调，通知保留在outbox。
信号持久库由本项目唯一写入；两个消费者各自在自己的账本原子认领信号。认领后崩溃不重放方向，订单按原ID恢复，避免重复交易。
模型代码、版本及提示词只维护 `src/analysis_core/model.py` 与 `src/analysis_core/prompts/`。
当前提示词为 `prompts/screening_v3.md` 与 `prompts/direction_v11.md`；首选通过 `direction_required=true` 约束Schema。旧版本保存在 `docs/prompt-history/`。这是用户明确要求的相对排序策略，不再使用旧筛选的绝对质量门清空结果。

行情不足、错误、模型服务故障、首选违规SKIP均记为失败，禁止伪造方向或静默报正常。交易端仍独立检查资金、仓位和止盈可达性，不保证每轮实际成交。

验收与仍待完成项目见 [ACCEPTANCE.md](docs/ACCEPTANCE.md)。

## Telegram通知

每轮分析合并为一条短消息，显示北京时间起止时段、大致耗时、首选/双仓标记和各币方向，不展开分析理由。每条汇总通知下方的内嵌键盘按币种提供详情按钮，点击显示该轮已保存的完整方向理由；币种按钮固定对应该通知的原轮次，不调用模型或重新发布信号。「最近分析」查看最新轮次汇总及详情入口。暂停/恢复为同一个位置的状态按钮，点击后随回复更新底部键盘按钮；旧消息的暂停操作仍是暂停，不会因重复点击误恢复。交易信号仍逐币即时发布，不等待汇总。分析服务已配置独立Bot并由systemd常驻运行；两交易系统保留各自暂停开仓状态。

底部虚拟键盘只提供管理菜单，不显示币种。分析异常页可通过按钮重试失败通知，分析本身仍等待新一轮，不重放旧信号。

主导航键盘允许展开/收起：统一 `resize_keyboard=true`、`is_persistent=false`、`one_time_keyboard=false`；发送入口禁止移除键盘，内嵌按钮与消息编辑不替换主导航。客户端收起后的图标呈现由Telegram控制。参见 [键盘与在线运行复核](docs/audit-2026-09-16/KEYBOARD-AND-LIVE-STATUS.md)。
