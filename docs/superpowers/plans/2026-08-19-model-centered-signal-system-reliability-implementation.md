# 模型中心信号系统可靠性重构实施计划

> 对应设计：`docs/superpowers/specs/2026-08-19-model-centered-signal-system-reliability-design.md`
>
> 当前生产保护：验证期间保持 `config/system.local.yaml` 的 `monitoring.enabled=false`；只有本文全部验收通过后才恢复实时监测。

## 实施原则

1. 当前权威设计优先于 2026-08-18 的旧设计、计划和测试说明。
2. 先理解现有数据流和未提交修改，再做局部编辑；不覆盖或回退无关工作。
3. 主信号、监测、失败重试分别具有独立成功条件。
4. 模型负责方向和阈值语义；程序只负责机械执行、版本、持久化和通知。
5. 每个任务先写失败测试或可复现检查，再实施，再运行专项测试。
6. 最终必须通过真实用户流程灰盒，不能只用函数或命令成功代替。

## Task 1：建立可复现基线与变更边界

目标：确认现有脏工作区、生产进程、失败周期和相关模块，避免覆盖已有工作。

读取与检查：

- `git status --short`、相关文件 diff 和最近提交；
- `src/bybit_signal/orchestration/cycle.py`；
- `src/bybit_signal/service.py`；
- `src/bybit_signal/analysis/codex.py`；
- `src/bybit_signal/domain/models.py`、`domain/enums.py`；
- `src/bybit_signal/monitoring/engine.py`、`monitoring/realtime.py`；
- `src/bybit_signal/notifications/*.py`；
- `src/bybit_signal/storage/sqlite.py`；
- 当前单元与场景测试。

保存生产 SQLite 的只读查询证据，不修改生产历史。记录两个基线失败：

- `cycle_20260819T022706Z_cd56c33a`；
- `cycle_20260819T023000Z_8bf56ff0`。

验收：可以从日志和数据库明确证明“模型与新鲜度成功、监测激活导致整轮失败”和“失败通知未读取真实原因”。

## Task 2：主信号与监测彻底解耦

目标：监测关闭或单币规则不可激活时，已经合格的两个方向信号仍成功落库并通知。

主要文件：

- `src/bybit_signal/orchestration/cycle.py`；
- `src/bybit_signal/domain/models.py`；
- `tests/unit/test_storage_and_cycle.py`；
- `tests/unit/test_service.py`。

步骤：

1. 增加回归测试：`monitoring.enabled=false` 时不得运行强制激活。
2. 增加回归测试：一个选中币阈值全部不可执行时，周期仍 `SUCCESS` 且 `selected_signal_count=2`。
3. 将监测激活结果改为逐币状态，不再用 `assessments={}` 清空主分析。
4. 保存“信号已投递、监测未启用”的诊断和 failure event 输入。
5. 紧急复核同样不因新规则失败而丢弃方向结论。

验收：重放 JCT、XPIN 类型失败时，两条方向信号保留，只有相应币种监测标记不可用。

## Task 3：建立模型中心运行契约和场景提示

目标：让默认模型理解系统每一步、输入输出和后续影响，不再作为普通 JSON 生成器使用。

主要文件：

- 新建项目内模型操作手册/技能说明；
- `prompts/signal_analysis_zh.md`；
- 新建定时分析、紧急复核、阈值复核共享契约或场景片段；
- `src/bybit_signal/analysis/codex.py`；
- `src/bybit_signal/analysis/tool_registry.py`；
- `tests/unit/test_codex_analyzer.py`。

步骤：

1. 把系统流程、模块职责、人工通知边界、阈值生命周期和失败后果写入共享模型契约。
2. 定时 Top 5、单币紧急复核、阈值复核使用独立任务提示，共享方向与结构语义。
3. 明确禁止仓位/杠杆/订单、目标驱动、同方向唤醒和微观指标单独唤醒。
4. 明确输出两个主信号、形成中 15m/30m/1h、下一根 15m、上一轮比较和方向失效结构。
5. 工具说明包含数据时效、覆盖、不可用语义和只读边界。
6. 测试 prompt payload 确实包含运行步骤、阈值用途和数据局限。

验收：模型在离线对话测试中能准确复述每一步职责，并按场景返回正确合同。

## Task 4：模型阈值复核与 0–3 条可选反向威胁规则

目标：最终规则必须由默认模型确认，且只代表当前方向受到反向结构威胁。

主要文件：

- `src/bybit_signal/analysis/codex.py`；
- `src/bybit_signal/domain/models.py`；
- `src/bybit_signal/monitoring/engine.py`；
- `src/bybit_signal/monitoring/realtime.py`；
- 对应 analyzer/engine/realtime 测试。

步骤：

1. 将规则数量合同收敛为每币按需 0–3 条；无合格规则为正常结果。
2. 规则保存结构锚点、证据 ID、生成基线、反向威胁机制、确认方式和有效期。
3. 支持“价格结构为主、微观指标为组合确认”的可执行表达；微观指标不能独立生成事件。
4. 主分析后使用同一默认模型运行独立阈值复核；主信号通知不等待该步骤。
5. 程序只检查字段、数据可用性、是否已经满足、目标引用、版本和表达式可执行性，不调值。
6. 阈值复核明确拒绝时正常零规则结束，不做语义重试或失败通知；机械激活时普通基线变化直接更新，只有最新同指标基线已经越过阈值、规则已不可执行时才只重采/重审该币最多 3 次。重审无合格规则同样正常结束，真正技术错误耗尽才建立单币 failure event。

关键回归样本：

- KORU LONG 的 OI 扩张和 5m 上破不得通过；
- GPS/VELVET SHORT 的继续下跌、成交放大和 OI 去杠杆不得单独通过；
- 反向完成 K 线或反向价格结构加合格辅助确认可以通过。

验收：历史错误规则全部被模型复核拒绝；合法反向规则可由监测器执行。

## Task 5：修正一次性生命周期、冻结和版本提交

目标：首条规则满足后旧组永久失效，固定、紧急和手动重试互不阻塞且不会跨版本覆盖。

主要文件：

- `src/bybit_signal/monitoring/realtime.py`；
- `src/bybit_signal/storage/sqlite.py`；
- `src/bybit_signal/orchestration/cycle.py`；
- `src/bybit_signal/service.py`；
- 相关并发和持久化测试。

步骤：

1. 保留首事件原子记录和整组暂停，修复任何 sibling 重复接受路径。
2. 事件触发时冻结源 conclusion 和 evidence 引用，紧急任务不再临时查询可能已被新周期替换的 prior。
3. `:00/:30` 定时任务开始时停止接收旧事件；此前旧规则持续有效，已接受的紧急任务继续完成。
4. 删除固定周期对 `wait_until_idle()` 的等待，确保 `:30/:00` 准时启动。
5. 新规则提交使用 compare-and-set：源版本不是当前权威版本时只能通知结果，不能覆盖。
6. 固定周期是最新 Top 2 和监测集合权威；旧紧急/手动结果标记 `SUPERSEDED`。

验收：并发测试证明定时不等待、旧结果不覆盖、失败不复活旧组、重启后状态一致。

## Task 6：结构化 failure event 与因果解释

目标：所有用户可感知失败都说明流程、步骤、根因、影响和解决方式。

主要文件：

- `src/bybit_signal/domain/models.py`、`domain/enums.py`；
- `src/bybit_signal/storage/sqlite.py`；
- 新建失败分类/解释模块；
- `src/bybit_signal/orchestration/cycle.py`；
- `src/bybit_signal/service.py`；
- formatter 和存储测试。

步骤：

1. 定义 failure domain、scope、step、completed steps、cause、impact、resolution、retry action 和安全 diagnostics。
2. 新增 `failure_events` 持久化和查询。
3. 为扫描、采集、模型、合同、新鲜度、阈值、紧急复核和未捕获异常建立明确映射。
4. 把阶段诊断转为用户可读因果链，错误码只作辅助。
5. 去除按 `FAILED:<code>` 的跨周期静默；同一 failure ID 投递幂等，不同周期分别通知。
6. 恢复通知只在对应失败域实际成功后发送。

验收：JCT/XPIN 失败通知能准确说明前面步骤已成功、失败在阈值交付、为什么失败、信号是否保留以及怎样修复。

## Task 7：持久化精准重试任务

目标：用户点击哪个失败，就刷新必要数据并只重试相应失败域。

主要文件：

- `src/bybit_signal/storage/sqlite.py`；
- 新建 retry service；
- `src/bybit_signal/service.py`；
- cycle/analyzer 公开的安全重试入口；
- retry 单元与场景测试。

步骤：

1. 新增 `retry_jobs`，保存短 token、failure ID、scope、状态、用户、时间、源版本和结果版本。
2. 对同一 failure event 原子领取，防止重复模型调用。
3. 实现精准重试矩阵：扫描、Top 5 采集、主模型、新鲜度、单币阈值、单币紧急和完整周期。
4. 所有超短线重试刷新必要行情，不直接复用旧快照。
5. 服务重启将 RUNNING 标记 INTERRUPTED，重新提供按钮，不静默继续。
6. 新的固定成功结果使旧 job `SUPERSEDED`，旧结果不能覆盖。

验收：重复点击、重启、并发固定周期和旧按钮均具有确定、幂等结果。

## Task 8：Telegram 失败卡、诊断详情和回调

目标：实现符合 Telegram 官方接口的人工重试体验，同时保持 ReplyKeyboard 生命周期。

主要文件：

- `src/bybit_signal/notifications/formatter.py`；
- `src/bybit_signal/notifications/keyboards.py`；
- `src/bybit_signal/notifications/telegram.py`；
- Telegram 单元和场景测试。

步骤：

1. 失败卡展示流程、步骤、已完成事项、因果、影响、解决方案和 analysis ID。
2. 增加 `按上述方案重试` 和 `查看完整诊断链` InlineKeyboard。
3. callback 使用 SQLite 短 token，保持 1–64 字节。
4. 所有 callback 先鉴权并立即 `answerCallbackQuery`；随后原子领取后台任务。
5. 用 `editMessageText`/`editMessageReplyMarkup` 移除已领取按钮并标注状态。
6. 成功发送新结果；失败产生新 failure event 和新按钮。
7. 保持主 ReplyKeyboard 的既定三个标志，任何流程不发送 remove keyboard。

验收：正常、重复、未授权、过期、已被新周期取代、服务重启中断均有测试；Telegram 轮询不被 5 分钟模型调用阻塞。

## Task 9：Top 5 与数据采集复核

目标：确保 Top 5 满足高波动机会和足够交易额，不让薄流动性仅靠瞬时振幅占满候选。

主要文件：

- `src/bybit_signal/selection/scanner.py`；
- `src/bybit_signal/providers/deep_market.py`；
- `src/bybit_signal/evidence/builder.py`；
- selection/provider/evidence 测试。

步骤：

1. 用近期真实 Top 5 检查机会分、交易额、点差和 depth 的权重与硬门槛。
2. 保持山寨币门槛不过高，但显著降低 30m 交易额过低、盘口过薄币种的入选概率。
3. 确保模型获得 OI 时间序列、数据完整度、时间对齐和微观数据窗口，而不是孤立当前值。
4. 缺失旁证明确降级，Bybit 核心不足才阻止相应候选。

验收：真实扫描 Top 5 的交易额和可交易性明显优于当前薄流动性样本，同时保留高波动机会。

## Task 10：自动化、历史回放和 Telegram 真实场景

执行：

1. 新增设计文档第 14 节的全部确定性场景。
2. 复制生产 SQLite 到隔离目录，回放真实失败和阈值事件，不写生产库。
3. 运行专项 pytest，再运行全量 pytest。
4. 运行 Ruff、mypy strict、pip check 和配置加载。
5. 向授权测试 chat 发送一条明确标注“测试”的失败卡，验证按钮、详情、重复点击和 ReplyKeyboard。

验收：所有失败场景修复后重新运行并通过；未验证功能必须视为阻塞，不能直接部署。

## Task 11：默认模型多轮对齐与真实数据影子灰盒

目标：确认 Prompt、技能、工具和采集字段真正服务于默认模型。

步骤：

1. 使用 KORU、ACE、GPS、VELVET、JCT、XPIN 完整证据与日志进行模型对话。
2. 要求模型复述系统、解释方向和审查每条历史规则。
3. 根据模型暴露的真实缺口调整数据、工具或提示，再重复测试，不能为通过测试降低语义要求。
4. 在隔离配置和数据库中运行至少一个完整真实固定周期。
5. 启用隔离实时监测，观察自然事件；没有事件时不得人为降低阈值。
6. 若自然触发，跑满通知、失效、重采、紧急复核、新版本；若没有自然触发，用同一真实规则进行确定性行情序列回放补充生命周期验证。

验收：默认模型理解系统并输出两个合格方向；最终规则都是反向结构威胁；不出现同方向连锁唤醒。

## Task 12：文档、部署与生产首轮验证

主要文档：

- `README.md`；
- `docs/ARCHITECTURE.md`；
- `docs/DEVELOPMENT.md`；
- `docs/DEPLOYMENT_VPS.md`；
- `docs/TEST_REPORT.md`；
- 模型评估/操作说明。

步骤：

1. 更新文档为本设计的当前语义，明确旧文档仅为历史。
2. 提交前检查 staged files，避免混入无关修改。
3. 保持生产监测关闭，部署新代码并验证一个固定周期恰好两条信号。
4. 验证失败通知和手动重试后再开启实时监测。
5. 观察首个生产规则版本；出现同方向触发、重复唤醒、跨版本覆盖或无因果失败时立即重新关闭监测。
6. 记录生产 PID、日志、SQLite analysis ID、Telegram 投递和验收结果。

最终验收：固定流程、模型中心分析、规则复核、一次性生命周期、并发版本、失败因果、精准重试、Telegram 键盘和部署文档全部符合设计；实际方向准确率留待获得新的后续行情后单独结算。

## 2026-08-19 执行记录

- Task 1–9：已实现并通过自动化与真实公开数据检查。
- Task 10：Ruff、严格 mypy（src+tests）、pip check、配置、Skill、PowerShell 与 150 项 pytest 全部通过；Telegram 失败卡/重试记录已在生产落库并投递。
- Task 11：真实定时灰盒 `cycle_20260819T041649Z_7a61e315` 与紧急灰盒 `urgent_20260819T041940Z_3021cb26` 跑满，方向/阈值语义和新版本覆盖通过；未来行情误差按用户要求另行结算。
- Task 12：代码、配置与文档已部署。生产主分析 `cycle_20260819T043406Z_c14183ed` 成功发送两个信号；独立阈值复核随后因 Codex 工作区额度耗尽而安全失败。修复全失败时保持冻结后再次部署，`cycle_20260819T043818Z_b0eac978` 验证明确错误码、Telegram 失败卡及旧规则持续冻结。
- 当时外部待办：Codex 工作区额度曾在 12:35 阻塞阈值复核；13:00 后额度已恢复并完成多个真实 v7 周期。历史阻塞记录保留用于说明失败安全，不再是当前待办。

### 2026-08-19 13:00 后续校正

- 以当时完成 K 线复盘 GPS/JCT：确认同一引擎按各自完整规则执行，GPS 的两根 5m 确认过迟，JCT 的 1m+5m 组合过敏；问题归因到模型规则选择而非监测器分叉。
- Prompt/Skill 升级为 v7/v5：明确迟滞仅用于 re-arm、约 2 个 1m ATR 仅为启发式、已确认枢轴例外以及完整确认时延审计。v7 对 GPS 改为单根已确认 5m 枢轴，对缺少中间结构锚点的 JCT 规则选择拒绝。
- 阈值事件持久化 primary 与全部 confirmations，Telegram 和紧急 trigger reason 可还原完整执行条件。
- 修复 Windows 服务脚本只停 launcher 而遗留 worker 的问题；状态、启动和停止均按本项目 Python 进程树识别。
- 增加激活基线保护：模型复核后，无论新完成柱向 threshold 还是向安全侧移动，只要条件仍未满足就机械更新同一 threshold 的基线；只有最新基线已经越线、规则不可执行时才重采/重审该币最多3次。宿主从不替模型移动阈值。
- 14:00 自然周期准点运行，Top5 成交额和 Top2 数量通过；TRIA/HEMI 均缺少可执行的中间反向结构锚点，模型拒绝规则但保留两个方向通知，监测继续冻结。此结果作为正确降级接受，不为追求“必须有阈值”而硬改。

### 2026-08-19 15:00 v8 校正与 v9 最终收敛

- Prompt/Skill 明确锚点周期与执行周期可不同；同一 GPS 冻结证据由旧版连续拒绝变为首轮识别完成15m接受边界，并用完成1m在正式5m失效前执行。
- v9 把阈值明确降为可选辅助：初次 `REJECTED` 正常零规则结束；激活前普通基线变化机械重基线，只有最新基线已越线、规则不可执行时才使用最新重采证据按币重审最多3次。重审仍无规则正常结束，已通过/激活币冻结。
- 正式周期 `cycle_20260819T070029Z_a922bc31` 验证 HEMI 首轮激活、ACE 仅重试自身3次并最终安全拒绝；方向SUCCESS、主通知1次、最终failure1次、监测PARTIAL恢复。
- 最终失败步骤只保留真正的模型/Schema/采集/激活技术错误；JSON Decimal格式自检禁止尾随逗号；激活修复遇到额度/认证/模型不可用时立即以精确原因停止且不消耗剩余轮次。最新自动化数量与质量门禁见 `docs/TEST_REPORT.md`。
