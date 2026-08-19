# VPS 部署与 Codex 交接

## 1. 前提

默认部署基线为 Debian 13 stable（amd64/arm64）、Python 3.13、2 vCPU/4 GiB 以上；Debian 12/Python 3.12 也满足项目版本要求。需要能出站访问 Bybit、Binance Futures、OKX、Telegram 和 Codex 服务。

不需要任何交易所 API Key。唯一业务秘密是 Telegram Bot Token。VPS 上还需为运行用户完成 Codex CLI 官方登录，并确认该账户可用 `gpt-5.6-terra`。

VPS Codex 应先完整执行仓库根目录 [`VPS_CODEX_HANDOFF.md`](../VPS_CODEX_HANDOFF.md)，本页提供人工运维命令。

## 2. 安装

以下目录与 systemd 模板一致：

```bash
sudo apt-get update
sudo apt-get install -y \
  git curl ca-certificates tzdata \
  python3 python3-venv python3-dev build-essential

getent passwd bybit-signal >/dev/null || \
  sudo useradd --create-home --shell /bin/bash bybit-signal
sudo install -d -o bybit-signal -g bybit-signal -m 0750 /opt/bybit-signal
sudo install -d -o bybit-signal -g bybit-signal -m 0700 /home/bybit-signal/.codex
sudo -u bybit-signal -H git clone \
  https://github.com/413hy/trading-cheap-bybit.git \
  /opt/bybit-signal
cd /opt/bybit-signal
sudo -u bybit-signal -H python3 -m venv .venv
sudo -u bybit-signal -H .venv/bin/python -m pip install --upgrade pip
sudo -u bybit-signal -H .venv/bin/python -m pip install \
  -c requirements-debian.lock.txt '.[dev]'
sudo -u bybit-signal -H .venv/bin/python -m pip check
sudo -u bybit-signal -H cp config/system.example.yaml config/system.local.yaml
sudo -u bybit-signal -H cp .env.example .env
```

开发依赖保留在服务虚拟环境中用于上线前和更新后的完整回归；systemd 运行时只导入生产依赖。Debian 使用独立的 `requirements-debian.lock.txt`，不包含 Windows-only `pywin32`。

编辑 `config/system.local.yaml`：

- `telegram.enabled: true`
- `allowed_chat_ids` 与 `allowed_user_ids` 填自己的数字 ID；
- 默认公开数据 URL、30 分钟周期、Top 5、固定 2 个相对最优主信号和监测软预算通常无需修改。实时阈值 crossing 只唤醒复核，不直接等于方向失效；大致止盈位不参与阈值或生命周期。旧阈值持续到自然半小时任务真正开始，在 `:00/:30` 边界持久化冻结；已接受的紧急复核继续、定时流程不等待，只有最新权威分析经独立阈值复核后才能发布新规则。

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
sudo -u bybit-signal -H bash -lc \
  'curl -fsSL https://chatgpt.com/codex/install.sh | sh'
sudo -u bybit-signal -H bash -lc 'command -v codex && codex --version'
sudo -u bybit-signal -H bash -lc 'codex login --device-auth'
sudo -u bybit-signal -H bash -lc 'codex login status'
```

无头 VPS 优先使用设备码登录。官方还支持通过标准输入使用 API Key 登录；不要把 Key 写入仓库或 shell history。`/home/bybit-signal/.codex/auth.json` 必须按密码文件保护。本项目已用 `codex-cli 0.147.0` 验证；更新版本必须仍支持 `exec`、ephemeral、忽略用户配置/规则、JSON Schema 和 output-last-message 参数。

systemd 模板已把服务用户的 `.local/bin` 加入 PATH，并只开放 `.codex` 认证目录的必要写权限。服务会在系统临时空目录创建 ephemeral 分析进程，不复用对话历史；`--ignore-user-config/--ignore-rules` 和 feature disable 会阻止全局 Skill、shell、插件或浏览器污染分析，但仍读取该用户的官方认证状态。仓库内 `.agents/skills/analyze-bybit-ultrashort-signals/` 会作为版本化文本显式注入，宿主白名单市场工具通过 JSON 协议执行，不依赖 Codex 原生工具。

官方参考：

- <https://learn.chatgpt.com/docs/codex/cli>
- <https://learn.chatgpt.com/docs/auth>

## 4. 上线前验证

```bash
cd /opt/bybit-signal
sudo -u bybit-signal -H .venv/bin/python -m ruff check .
sudo -u bybit-signal -H .venv/bin/python -m mypy src tests
sudo -u bybit-signal -H .venv/bin/python -m pytest -q
sudo -u bybit-signal -H .venv/bin/python -m compileall -q src
sudo -u bybit-signal .venv/bin/python -m bybit_signal config-check --config config/system.local.yaml
sudo -u bybit-signal .venv/bin/python -m bybit_signal telegram-check --config config/system.local.yaml --send-test
sudo -u bybit-signal .venv/bin/python -m bybit_signal market-capture --config config/system.local.yaml --symbol CYSUSDT
sudo -u bybit-signal .venv/bin/python -m bybit_signal run-cycle --config config/system.local.yaml --no-notify
sudo -u bybit-signal .venv/bin/python -m bybit_signal run-cycle --config config/system.local.yaml --notify
```

预期：market-capture 显示六个完成周期、Bybit last/mark、盘口、成交和 OI；成功轮次恰好 2 个主信号，每条具备形成中 15m/30m/1h 和下一根 15m 预测；Telegram 收到核心结论、详情和返回按钮。主信号先发送；模型无合格阈值属于正常结果，不重试、不发异常。只有规则在激活前已经越线或出现真正技术错误时，系统才按币静默重采/重审，且不得把两条方向信号清空。

## 5. systemd

```bash
sudo cp deploy/systemd/bybit-signal.service /etc/systemd/system/bybit-signal.service
sudo systemd-analyze verify /etc/systemd/system/bybit-signal.service
sudo systemctl daemon-reload
sudo systemctl enable --now bybit-signal.service
sudo systemctl status bybit-signal.service
sudo journalctl -u bybit-signal.service -f
```

应用日志还在 `/opt/bybit-signal/runtime/logs/service.log`。unit 使用 `Restart=always`、`KillMode=control-group`、只读项目目录，并只允许写入 runtime 与服务用户的 Codex 认证目录；程序内 300 秒超时在 Debian 上使用独立 POSIX 进程组清理完整 Codex 进程树。如果实际安装路径不同，必须同步修改 WorkingDirectory、EnvironmentFile、ExecStart、PATH 和 ReadWritePaths。

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
cd /opt/bybit-signal
sudo -u bybit-signal /opt/bybit-signal/.venv/bin/python -m pip install \
  -c /opt/bybit-signal/requirements-debian.lock.txt '.[dev]'
sudo -u bybit-signal /opt/bybit-signal/.venv/bin/python -m pip check
sudo -u bybit-signal /opt/bybit-signal/.venv/bin/python -m pytest -q /opt/bybit-signal/tests
sudo -u bybit-signal /opt/bybit-signal/.venv/bin/python -m bybit_signal config-check --config /opt/bybit-signal/config/system.local.yaml
sudo systemctl start bybit-signal.service
```

更新前备份 `runtime/state/signal.db` 及其 WAL/SHM 文件，保持同一时点一致性。回滚使用已确认 commit 后重新安装；不要用强制 reset 覆盖 VPS 上尚未保存的配置。

## 8. 健康与故障定位

- `telegram-check` 失败：检查 Token、白名单、Bot 是否能与目标 chat 通信。
- `market-capture` 失败：检查公网 DNS/HTTPS、地区访问限制和 Bybit endpoint。
- `CODEX_START_FAILED`：systemd PATH 找不到 Codex CLI。
- `CODEX_TIMEOUT`：单次模型分析超过 300 秒时程序会终止完整进程树并按本轮剩余次数重试；查看网络、账户额度和模型可用性，失败轮次不会产生伪信号。
- Binance/OKX 单独失败：属于可选旁证，Bybit 主证据合格时仍可分析，详情会披露 unavailable。
- `another service instance owns ...`：已有服务持有单实例锁，不要再启动第二个调度器。
- Telegram 发送失败：结论仍已落库，可用 `/latest` 或只读 MCP 查询；服务下一轮继续。
- 监测配置失败：先检查诊断中的按币静默纠错历史；三次仍失败后方向通知仍有效，Telegram 会显示最终失败步骤/原因/解决方式，可用按钮只重跑该币监测配置。
- Telegram 的“完整诊断”用于查看因果链；“重试失败步骤”在后台幂等执行，服务重启后中断任务可再次点击，旧版本已被新定时轮替代时会安全停止。

把仓库根目录 `VPS_CODEX_HANDOFF.md` 作为 VPS Codex 的首个入口，再按 `docs/CURRENT_DOCUMENTS.md` 的“部署必读”顺序提供文档。应要求它只做环境适配与验证，不重新引入参考仓库、CMI、账户或交易模块。

模型选择灰盒已经完成，正式默认值为 `gpt-5.6-terra / medium`，详见 `docs/MODEL_EVALUATION_2026-08-18.md`。迁移到新 VPS 时仍要重新运行本节的 no-notify 与 notify 验证，因为 Codex CLI 版本、网络延迟和交易所地区可用性属于部署环境变量。
