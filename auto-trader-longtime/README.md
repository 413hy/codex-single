# auto-trader-longtime — 独立交易执行系统

选币与模型方向分析已迁移至 `/root/single-analysis`。本项目不调用模型，不运行选币，也不设置分析周期。

## 信号与交易

- 只读接收 `/root/single-analysis/runtime/signals.db` 的发布结果，每0.5秒检查一次。
- 发布后60秒有效；信号ID去重，过期不补执行，重启不重放。相同币种后续轮次有新的信号ID，可以独立处理。
- 普通候选按自身仓位、暂停开关、余额、精度、杠杆及TP可达性判断是否开仓。
- 双仓额外信号不作为普通开仓推荐；对冲系统仅在group_id和generation与当前LOCKED仓一致时处理。
- 暂停只停止普通新仓，已有仓位管理与对冲组信号处理继续。

普通止盈止损系统：收到有效方向后保留原资金、杠杆、数量及止盈可达性检查；开仓后按原策略设置TP和SL。交易逻辑与仓位核账保持原规则。

交易账户、Telegram Bot、数据库及日志各自独立；固定Bybit Demo接口，不切换资金实盘。

## 配置

`.env`仅配置本项目的Demo API、交易Bot和运行目录。`SIGNAL_DB`可指定发布库，默认路径如上。
旧CODEX_BIN/MODEL_TIMEOUT已从配置移除。旧数据库中的分析周期字段只在读取历史配置时忽略，新保存配置不再包含它们。
分析频率默认20分钟，只能通过新分析Bot管理；交易Bot的频率设置入口和旧按钮写入功能均已移除。

## 运行

```bash
cd /root/auto-trader-longtime
.venv/bin/bybit-longtime config-check
.venv/bin/bybit-longtime preflight
systemctl status bybit-longtime.service
journalctl -u bybit-longtime.service -f
```

`cycle`命令现在只接收当前有效信号，不运行模型；`scan`命令提示使用独立分析项目。
运行中的进程锁阻止第二个交易进程使用同一runtime。

## Bot

保留持仓、运行状态、最近交易、暂停/恢复开仓、交易异常和开仓设置。
`/analysis`只提示去分析Bot；模型故障通知不再由交易服务产生。历史异常和交易账本保留。

## 验证

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
.venv/bin/pytest -q
```

重构前源码和在线SQLite备份位于 `runtime/backups/before-signal-split-*`。
旧分析源码、旧接口测试及旧脚本存于 `docs/retired-analysis/`，不在运行包中。
详见 [三系统重构验收](docs/SIGNAL_SPLIT_ACCEPTANCE.md)。
