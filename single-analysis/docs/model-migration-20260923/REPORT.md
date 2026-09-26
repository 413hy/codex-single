说明：本报告是模型迁移当时的历史记录；现行 TradingView 提示词和数据年龄规则以 README.md 与 docs/tradingview/CURRENT-20260923.md 为准。

# 方向模型切换至 GPT-6 Sol medium（2026-09-23）

## 结果与范围

普通十币筛选继续固定 `gpt-5.6-terra / medium`；逐币方向分析改为 `gpt-6-sol / medium`。`screening_v3.md`、`direction_v11.md`、`direction_v12.md` 的内容和原 Schema 均未修改；首选必判 LONG/SHORT、其他币可 SKIP、同币每轮只调用一次、双仓优先、TradingView 仅作未确认的方向参考、信号发布后60秒有效等规则保留。没有更改交易端、调度或分析暂停状态。

本次迁移前代码节点为 `/root/single-analysis-backups/pre-sol-direction-20260923-052029`，`source.tar.gz` 的 SHA-256 为 `dff57d0da31a78deb573683aab34ac86008b768a15e8bd050386d1e3e44a01f8`，已通过 `sha256sum -c`。归档不含 `.env`、运行账本、模型认证文件及交易密钥；回滚指令见节点 `ROLLBACK.md`。回滚时必须保留当前暂停/调度状态、信号账本和消费去重记录，不得重放旧信号。

## 与 Sol 的两轮对齐

通过项目自身隔离的模型服务进程，对 `gpt-6-sol / medium` 做了两次真实设计审阅，提示词分别为 `sol_migration_review_v1.md` 和 `sol_alignment_review_v2.md`；输入、提示词哈希和原始输出留在 `runtime/sol-migration-review-20260923/` 与 `runtime/sol-alignment-review-20260923/`。两轮均建议保持生产方向提示词原文；本次未为“适配模型”虚改提示词。[官方 OpenAI 迁移指导](https://developers.openai.com/api/docs/guides/latest-model/gpt-6-astra.md#migration-quickstart)也要求保持所选模型支持的推理等级，并在代表性任务上验证。

审阅指出全局模型常量会误切换筛选，因此把筛选与方向设为独立、不可变的调用配置，提示词与用途绑定，审计记录用途、请求模型、推理等级、提示词与输入哈希、耗时。方向入口增加列式 K线的周期、时间顺序、完成状态及 `latest_closed_candles` 一致性校验；原行情采集层已检查原始 K线的周期跨度、未来收盘和连续性，新校验是送模前的第二道防线。启动和推理阶段均纳入单次超时；超时或取消时杀进程组、回收子进程，启动异常卡住时安排迟到进程的清理。方向行情从采集时点到模型调用和发布的年龄上限收紧为90秒；发布前的二次校验保留，超时不补调用。

第二轮模型审阅因未看到 `market.py`，将原行情层已有的周期/未来收盘校验误判为缺失；已对照实际代码及冻结行情核实。模型还建议服务器端模型身份回执，但当前 Codex CLI 通路只可验证启动参数、审计事件与真实输出，不能把调用参数说成服务器端独立身份认证。自由文本理由的所有经济解释也无法仅靠现有 Schema 机械证明；本次只对真实样本的关键数值做原始行回查。

## 真实模型与系统验证

- 隔离筛选调用：`SCREENING_MODEL_INPUT` 记录 Terra medium，真实输出完整评价十币，选 UNI/MET/NIL；证据 ID、名额与 Schema 校验通过。记录在 `runtime/sol-direction-validation-20260923/screening.json`。
- 隔离方向调用：`MODEL_INPUT` 两次均记录 Sol medium。固定 UNI/TradingView 样本判 LONG，历史 SOPH 偏空样本判 SHORT；均通过原 Schema，关键 K线时间、价格、成交量及订单流数字回查原始行相符。记录在同目录的 `primary_long_tv.json`、`historical_short.json`。这些请求不打开信号总线，不通知 Bot，不发布交易信号。
- 三份生产提示词 SHA-256 与 TradingView 接入前存档一致：筛选 `539cda7947281e56ad2a64b56204157fea4ab1d65bfc31c10a40c07320`，方向 v11 `987f340fba7ffc468bb8b0b4c48a9cba7cb85dddcfa70ec2d6d95abd2710483f`，方向 v12 `85e01c250d1e2d511a741a57719c79c3a8e5681c69fb45ac96fca9b36c56ea04`。
- `.venv/bin/ruff check src tests`、`.venv/bin/mypy src`、`.venv/bin/pytest -q` 全通过，最终为 **212 passed**。新增回归测试覆盖筛选/方向实际启动参数、提示词用途绑定、错误K线调用前拦截、90秒行情年龄边界、启动超时及迟到进程清理。

固定样本验证的是合规性、事实引用和调用路由，不能证明预测胜率或未来收益。90秒年龄上限会使慢于该窗口的方向结果安全失败；若真实运行中因此频繁跳过，应先查看模型耗时和行情采集延迟，再基于证据调整，不自动放宽，也不重放旧信号。

## 服务落地

已重启 `single-analysis.service` 加载新代码，服务为 `active`、`NRestarts=0`。重启后持久化的 `analysis_paused=true`，没有正在运行的周期；未手工运行 `cycle`，没有因迁移发布新信号。服务使用的可编辑安装路径为本项目 `src/analysis_core/model.py`，路由常量现场读取为筛选 Terra medium、方向 Sol medium。恢复分析仍应由原分析 Bot 的既有控制流程按上海固定时间点生效。
