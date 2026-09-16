# 主导航键盘与在线运行复核

## 实现与部署

分析Bot统一ReplyKeyboardMarkup参数resize_keyboard=true、is_persistent=false、one_time_keyboard=false。发送入口统一直接发送和旧outbox中的导航参数，拒绝移除键盘；编辑消息不得附带ReplyKeyboard。InlineKeyboard原样保留，仅作用于对应消息。服务启动不发送清除键盘消息。

两交易Bot已具备相同参数与发送入口防护，本轮复查并运行全套回归，无需修改或重启持仓中的交易服务。分析Bot已重启加载新代码，部署前后analysis_paused、analysis_interval、next_analysis_at逐项一致，未改变用户运行状态或调度时间。

最终分析147、普通171、对冲193，共511项通过，三个项目Ruff/mypy通过。HTTP灰盒覆盖主菜单、配置保存、旧消息队列参数规范化、发送内嵌键盘、编辑消息、重建Bot、禁止移除键盘的发送前拦截。测试中的remove_keyboard坏输入仅用于证明请求被拦截，不会发往Telegram。

键盘是否收起及输入框旁图标的最终呈现由Telegram客户端决定。代码保证不要求常驻、不要求一次性使用、不移除键盘；未宣称所有客户端版本的视觉表现绝对一致。已存在的旧菜单参数在下一次主导航回复时更新，不为更新键盘重发历史交易/分析通知。

## 本轮在线核查（2026-09-16约10:30）

- 三服务active/running，NRestarts=0；SQLite quick_check均ok。
- 分析已由用户恢复，当前间隔20分钟。最近scheduled轮次09:38:53、10:02:23、10:22:24均SUCCESS；10:02到10:22约20分钟，非旧UTC边界连续执行。
- 分析outbox全部SENT，无OPEN异常；两个信号消费者心跳新鲜，对冲监控快照约4秒，普通监控约20秒（其原30秒轮询）。
- 两交易端均已恢复开仓。真实Demo GET核查两端PONSUSDT空仓各50；普通端存在同量Reduce-Only TP限价与Reduce-Only/Close-On-Trigger SL条件市价；对冲端存在同量Reduce-Only TP及反向开多50的条件限价（非Reduce-Only），符合当前单仓阶段。
- 核查期间PUFFERUSDT由OPEN变为CLOSED/TP，与实时接口已无该仓一致。两端最近信号均有独立消费回执；XRP为SKIP_TP_UNREACHABLE，未把策略跳过当成系统故障。
- 24小时日志无Traceback；包含旧重构前模型错误、旧对冲成交时间等待告警。普通端09:10有Telegram getUpdates 502和投递失败日志；对应轮询异常现为RESOLVED。
- 普通端仍保留5条历史OPEN入场异常、1条FAILED通知；对冲端无OPEN异常/FAILED通知。未删除、重发或伪装已解决。

结论：抽查时当前调度、消费、实际保护单和服务运行状态符合对应需求；历史问题仍保留。只读状态快照不能证明未来所有订单竞态或Telegram客户端视觉行为均正常。
