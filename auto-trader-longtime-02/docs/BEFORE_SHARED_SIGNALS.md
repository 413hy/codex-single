> 重构前历史说明，当前运行方式以README.md为准。

# Bybit Longtime 02 — 独立 Demo 对冲系统

基于 `/root/auto-trader-longtime` 复制，当前目录 `/root/auto-trader-longtime-02`。
原项目保持原状。没有复制 `.env`、认证文件、运行账本、日志或虚拟环境。

## 策略

- 保留原选币、方向模型、入场止盈可达性检查、10U默认保证金、3倍默认杠杆及20U余额预留。
- 首仓默认沿用原净止盈0.5U；不设平仓止损。反向开仓数量等于被对冲仓位数量，对冲价格距离默认1.7U，不含手续费。
- 提前挂反向条件限价单，触发价与限价一致；触发后未全成继续挂限价，不转市价。
- 部分成交保留顺向止盈；全部成交后撤销双方止盈止损，等待下一轮模型判向。
- 首次部分成交30秒内全成只发全成通知；否则部分与全部各通知一次。通知去重和计时状态持久化。
- 顺向先退出：撤销剩余对冲单并确认终态，然后市价、只减仓平掉实际反向余仓；处理撤单期间新增成交。
- 双仓每轮额外分析，不受筛选0—3币限制；无方向或止盈可达性不通过，保持双仓。
- 有效方向先平逆向仓，然后以此次判断的报价为基准计算顺向0.5U收益距离和逆向1.7U距离，重新挂止盈与对冲。
- `/pause`暂停首次开仓，已有对冲和双仓分析继续。没有持仓时间限制。

详见 [对冲状态与边界](docs/HEDGE_STRATEGY.md)。

## 当前状态

独立 Demo 服务已部署、启用交易并设置开机自启（2026-09-15）。新凭据已通过账户与Bot验证。
真实筛选/方向分析已运行；一笔LSK开仓因交易所无可立即成交数量而零成交取消，未重复下单。
模型额度已恢复，复验周期成功，原额度告警自动解除。服务运行中，保留用户原先暂停开仓状态。
当前尚未完成实际持仓、对冲成交及最终平仓全流程验收，不能认定全部符合预期。
详情见 [最新验收记录](docs/ACCEPTANCE.md)。

## 配置新账户

本地编辑 `.env`，填写新的 Demo 子账户 API Key、Secret、Bot Token、Chat ID 和允许操作的 User ID。
无需在聊天中粘贴凭据。`.env`权限为600，已被Git忽略。

账户需使用全仓、USDT永续双向持仓模式。单向模式会阻止开仓，程序不自动更改账户模式。
私有接口固定 `https://api-demo.bybit.com`。同一账户换Key不是账户隔离，应使用独立Demo子账户。

```bash
cd /root/auto-trader-longtime-02
.venv/bin/bybit-longtime config-check
.venv/bin/bybit-longtime preflight
```

`preflight`只读账户与Bot身份。未填写新凭据时不会通过。
沿用主机已有模型登录，不复制认证文件；模型选择与原项目相同。

## 检查

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src
systemd-analyze verify deploy/bybit-longtime-02.service
```

## 后续启动

完成新账户配置与Demo验收后，才将 `TRADING_ENABLED=true`。
独立服务文件为 `deploy/bybit-longtime-02.service`，工作目录和可写目录均指向新项目。

```bash
mkdir -p runtime
cp deploy/bybit-longtime-02.service /etc/systemd/system/bybit-longtime-02.service
systemctl daemon-reload
systemctl enable --now bybit-longtime-02.service
journalctl -u bybit-longtime-02.service -f
```

上述独立服务部署已执行。不要使用原项目的服务名或共享runtime。

Telegram保留原菜单与权限校验，“止损设置”改为“对冲距离”；增加部分/全部对冲成交通知和对冲余仓平仓原因。
持仓显示来自交易所；独立SQLite账本包含每条腿的实际成交、费用与已实现盈亏。
