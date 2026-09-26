# single-analysis

为两套 Bybit Demo 交易系统统一生成方向信号。本项目只分析与发布，不下单，也不保存交易 API 密钥。

## 当前流程

1. 按上海时间固定周期运行，默认每 20 分钟。只跳到下一个固定时间点；暂停、重启或长轮次错过的周期不补跑。
2. 从 TradingView 公开 CEX Screener 发现 BYBIT USDT 永续合约；Bybit 公共合约清单只用于确认这些币仍可交易。旧 Bybit 全市场评分扫描器已移出生产代码。
3. 抓取发现池每币的 TradingView 同合约技术快照（5m、15m、30m、1h、2h、4h、1d）、技术评级、成交与表现、同市场榜单、Ideas，以及可用的币种级背景。第一阶段模型比较证据，初选 1—10 个币和大致 LONG/SHORT。
4. 对初选币补充 Bybit 已收盘 K 线、量价指标、盘口和成交样本，以及 TradingView 公开新闻背景。第二阶段模型横向比较全部有效候选，给出最终 1—3 个币的 LONG/SHORT 方向。主目标是未来约 1—2 小时，一周结构只作背景。首选必须明确方向；低置信度如实标注。
5. 读取对冲系统 20 秒内、归属明确且 LOCKED 的双向仓位快照。若与最终 1—3 币重合，直接复用最终方向，不再次抓取或判向；其他双仓币单独抓取 TradingView 和 Bybit 证据并判向，可返回 SKIP。双仓不占普通名额。
6. 每币本轮最多发布一次至共享 `runtime/signals.db`，信号发布后 **10 分钟**有效。两个交易系统分别只读消费并各自持久去重；旧信号不重放。分析 Bot 只发送本轮汇总和固定轮次的详情按钮。

TradingView 网站内部请求并非公开稳定数据 API；无需登录即可使用的公开页面是当前数据源。页面指标可能尚未收盘，Ideas 是用户观点，新闻与币种聚合字段只是弱背景，均保留来源和缺失状态。数据适配器位于 `tradingview_sources.py`，可按 `name/stage/collect` 接口扩展；核实身份或核心技术快照失败时不伪造方向。重构前版本可通过仓库 `codex-select` 标签回滚。

## 模型与调度

- TradingView 初选：`gpt-5.6-terra / medium`，`tv_selection_v1.md`。
- Bybit 复核最终方向和额外双仓：`gpt-6-sol / medium`，分别使用 `direction_selection_v1.md` 与 `direction_v15.md`。
- 仅隔离模型服务进程调用模型；失败不二次追问、不降级旧扫描流程。提示词、Schema、输入与输出审计保存在本地分析库。
- 频率可由分析 Bot 设置为 10—1440 分钟且为 10 的整数倍，以 1970-01-01 上海时间 00:00 为固定锚点跨日连续计算。交易 Bot 不修改频率。

## 运行与验证

```bash
cd /root/single-analysis
.venv/bin/single-analysis check
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/pytest -q
```

`single-analysis cycle` 会真实分析并向交易系统发布信号，联调时必须明确其影响。`serve` 由 `deploy/single-analysis.service` 管理。分析 Bot 只支持管理菜单、暂停/恢复、频率设置、历史分析与异常查询；每轮币种详情固定对应原轮次，不重新调用模型或发布信号。

新流程与来源说明见 [重构报告](docs/refactor-20260926/REPORT.md)。
