# 参考资料审查与取舍

## 原则

所有仓库、文档、Prompt 和本地项目都只作为设计证据。当前代码没有把它们作为运行时依赖，也没有直接复制其交易模块。采用的思想均按 Bybit、多币种、纯通知和现有数据合同重新实现。

## 审查基线

| 资料 | 审查版本/位置 | 结论 |
|---|---|---|
| nirholas/Binance-MCP | `df8861cf3d6815644912ae3c4437c082ff24c7a2` | 参考 MCP 工具边界、stdio 交互和明确的行情请求表达 |
| junsonle/Binance-mcp | `cbb849c16b8065b869c7dcef248b2602c74918a1` | 参考行情 Prompt 中 symbol/timeframe/depth 的明确口径 |
| followers-asm/refactor/bitget-btc-codex | `dd2996659267b6430c828999e4d4deab48a32a13` | 参考 Codex 独立裁决、完成柱、证据 ID、阈值迟滞/合并/限频和失败关闭 |
| 无图分析 Prompt | `E:\quantify\prompt\无图分析 Prompt.txt` | 参考证据与指令隔离、价格身份、字段级降级、唯一方向/目标、forming 1h 和最终自检 |
| sol_realtime_snapshot / CMI 对话 | `E:\quantify\sol_realtime_snapshot` 与本机 CMI 安装目录 | 参考多源采集、原子快照、数据质量和缺失不等于零；未作为运行时组件 |
| Louie price action | `E:\quantify\louie_price_action_system_v1` | 参考多周期职责、效率/重叠、确认 pivot 和结构目标 |
| PYTA OrderFlow | `E:\quantify\PYTA_OrderFlow_Quant_System_Spec_v0.1.0` | 参考盘口绝对深度、成交方向映射和覆盖门槛 |
| AI Quant 文档包 | `E:\quantify\AI_Quant_Codex_Document_Generation_Package` | 参考数据、分析、通知分层；执行层被删除 |

## 已吸收并重写

- MCP：只保留本地只读 stdio 工具，工具只查询 SQLite 中的健康、信号、详情、快照、阈值和历史；没有 HTTP/SSE 暴露，也没有交易工具。
- 数据：自行实现 Bybit REST/WS、Binance USD-M ticker 和 OKX Swap ticker 适配器；Bybit last/mark 分离，跨交易所不平均。
- 时间：快照冻结截止线、已完成 K 线、最大缺口、陈旧检查和 SHA-256 身份；forming 1h 仅由模型预测，不拿形成中 K 线确认结构。
- 价格行为：EMA、Wilder ATR/RSI、MACD、rolling range、方向效率、重叠度、成交量比和已确认 pivot 均由本项目计算。
- 订单流：只使用 REST 盘口快照和有完整覆盖的近期成交窗口；不声称拥有连续 L2 的 OFI、补单、吸收或 sweep 真值。
- Codex：每轮使用新的 ephemeral、read-only 会话，严格 Schema、证据 ID 和宿主二次校验；候选分数不是方向，最多 2 个强信号且允许零个。
- 监测：模型只生成当前实现可观测的动态阈值，宿主负责迟滞、首次观测武装、60 秒合并、单币冷却和小时软上限。
- Prompt：保留无图分析的证据纪律，删除图片基线、网页搜索、BTC 单币历史继承和仓位假设，改成 Top 5 + 追踪币批量 JSON 合同。

## 明确未采用

| 内容 | 原因 |
|---|---|
| Binance/Bitget 的账户、钱包、余额、仓位、杠杆、订单和执行器 | 与纯通知边界冲突，生产源码有静态禁用测试 |
| 直接调用 CMI 或复制其代码 | VPS 可移植性和单实例/外部程序依赖不符合当前方案；所需公开数据已自建采集 |
| Kronos-mini 实时预测 | 当前机器未配置固定模型权重/PyTorch 运行链，也没有对本次动态山寨币池完成独立验证；加入会增加延迟和伪精度 |
| TradingAgents / OpenBB 实时主链路 | 更偏低频新闻/股票研究，覆盖与 30 分钟山寨币信号不匹配，且会增加多模型成本与来源不确定性 |
| Freqtrade / VectorBT 在线调用 | 它们适合离线回测、无前视和参数敏感性验证，不是本轮实时市场事实；当前不放入生产服务 |
| 连续 L2 OFI、吸收、补单、sweep、强平流 | 当前只稳定采集 REST 快照/近期成交，没有足够序列完整性，禁止虚构这些结论 |
| 新闻与网页搜索 | 30 分钟批处理需要可重放的固定输入；当前未建立来源、截止时间和缓存合同，模型因此不得自行浏览 |
| 自动下单及一键下单按钮 | 用户已明确暂缓，系统没有私有凭证和执行 API |

这些组件未来只有在“数据合同、独立验证、延迟预算、许可证和故障降级”全部明确后，才应以隔离适配器新增；不能仅因为参考项目存在就接入。
