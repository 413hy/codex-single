# VPS 部署与 Codex 交接

## 1. 前提

建议 Ubuntu 24.04、Python 3.12+、2 vCPU/4 GiB 以上。需要能出站访问 Bybit、Binance Futures、OKX、Telegram 和 Codex 服务。

不需要任何交易所 API Key。唯一业务秘密是 Telegram Bot Token。VPS 上还需为运行用户完成 Codex CLI 官方登录，并确认该账户可用 `gpt-5.6-sol`。

## 2. 安装

以下目录与 systemd 模板一致：

```bash
sudo useradd --create-home --shell /bin/bash bybit-signal
sudo mkdir -p /opt/bybit-signal
sudo chown -R bybit-signal:bybit-signal /opt/bybit-signal
sudo -u bybit-signal git clone YOUR_REPOSITORY_URL /opt/bybit-signal
cd /opt/bybit-signal
sudo -u bybit-signal python3 -m venv .venv
sudo -u bybit-signal .venv/bin/python -m pip install --upgrade pip
sudo -u bybit-signal .venv/bin/python -m pip install -c requirements-runtime.lock.txt .
sudo -u bybit-signal cp config/system.example.yaml config/system.local.yaml
sudo -u bybit-signal cp .env.example .env
```

编辑 `config/system.local.yaml`：

- `telegram.enabled: true`
- `allowed_chat_ids` 与 `allowed_user_ids` 填自己的数字 ID；
- 默认公开数据 URL、30 分钟周期、Top 5、最多 2 个强信号和监测限频通常无需修改。

编辑 `.env`：

```dotenv
BYBIT_SIGNAL_TELEGRAM__TOKEN=替换为真实Token
```

```bash
sudo chown bybit-signal:bybit-signal /opt/bybit-signal/.env
sudo chmod 600 /opt/bybit-signal/.env
```

不要把 `.env`、`config/system.local.yaml`、`runtime/` 或 Codex 登录状态提交到 Git。

## 3. Codex 登录

以服务用户执行官方登录：

```bash
sudo -u bybit-signal -H codex login
sudo -u bybit-signal -H codex --version
```

若 Codex CLI 安装在用户目录，确保 systemd 的 PATH 能找到它；也可以在 unit 的 `Environment=PATH=...` 明确配置。服务会创建 ephemeral 分析进程，不复用对话历史，但会读取该用户的官方认证状态。

## 4. 上线前验证

```bash
cd /opt/bybit-signal
sudo -u bybit-signal .venv/bin/python -m bybit_signal config-check --config config/system.local.yaml
sudo -u bybit-signal .venv/bin/python -m bybit_signal telegram-check --config config/system.local.yaml --send-test
sudo -u bybit-signal .venv/bin/python -m bybit_signal market-capture --config config/system.local.yaml --symbol CYSUSDT
sudo -u bybit-signal .venv/bin/python -m bybit_signal run-cycle --config config/system.local.yaml --no-notify
sudo -u bybit-signal .venv/bin/python -m bybit_signal run-cycle --config config/system.local.yaml --notify
```

预期：market-capture 显示四个完成周期、Bybit last/mark、盘口、成交和 OI；完整轮次最多 2 个 STRONG；Telegram 收到核心结论和详情按钮。

## 5. systemd

```bash
sudo cp deploy/systemd/bybit-signal.service /etc/systemd/system/bybit-signal.service
sudo systemctl daemon-reload
sudo systemctl enable --now bybit-signal.service
sudo systemctl status bybit-signal.service
sudo journalctl -u bybit-signal.service -f
```

应用日志还在 `/opt/bybit-signal/runtime/logs/service.log`。unit 使用 `Restart=always`、只读项目目录和仅可写 runtime；如果实际安装路径不同，必须同步修改 WorkingDirectory、EnvironmentFile、ExecStart 和 ReadWritePaths。

## 6. MCP（可选）

本项目 MCP 是本地只读 stdio，不应公开监听互联网。VPS Codex/MCP 客户端可配置：

```json
{
  "command": "/opt/bybit-signal/.venv/bin/python",
  "args": ["-m", "bybit_signal.mcp_server"],
  "cwd": "/opt/bybit-signal",
  "env": {
    "BYBIT_SIGNAL_CONFIG": "/opt/bybit-signal/config/system.local.yaml"
  }
}
```

它只能读取已落库信号，不能替代 30 分钟服务，也不能下单。

## 7. 更新与回滚

```bash
sudo systemctl stop bybit-signal.service
sudo -u bybit-signal git -C /opt/bybit-signal pull --ff-only
sudo -u bybit-signal /opt/bybit-signal/.venv/bin/python -m pip install -c /opt/bybit-signal/requirements-runtime.lock.txt /opt/bybit-signal
sudo -u bybit-signal /opt/bybit-signal/.venv/bin/python -m pip check
sudo -u bybit-signal /opt/bybit-signal/.venv/bin/python -m bybit_signal config-check --config /opt/bybit-signal/config/system.local.yaml
sudo systemctl start bybit-signal.service
```

更新前备份 `runtime/state/signal.db` 及其 WAL/SHM 文件，保持同一时点一致性。回滚使用已确认 commit 后重新安装；不要用强制 reset 覆盖 VPS 上尚未保存的配置。

## 8. 健康与故障定位

- `telegram-check` 失败：检查 Token、白名单、Bot 是否能与目标 chat 通信。
- `market-capture` 失败：检查公网 DNS/HTTPS、地区访问限制和 Bybit endpoint。
- `CODEX_START_FAILED`：systemd PATH 找不到 Codex CLI。
- `CODEX_TIMEOUT`：查看网络、账户额度和模型可用性；单轮失败不会产生伪信号。
- Binance/OKX 单独失败：属于可选旁证，Bybit 主证据合格时仍可分析，详情会披露 unavailable。
- `another service instance owns ...`：已有服务持有单实例锁，不要再启动第二个调度器。
- Telegram 发送失败：结论仍已落库，可用 `/latest` 或只读 MCP 查询；服务下一轮继续。

把 `README.md`、`docs/DEVELOPMENT.md`、`docs/ARCHITECTURE.md`、`docs/REFERENCE_REVIEW.md`、本文和 `docs/TEST_REPORT.md` 一并交给 VPS Codex。应要求它只做环境适配与验证，不重新引入参考仓库、CMI、账户或交易模块。
