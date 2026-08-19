# VPS Codex 部署交接单（Debian）

本文件是 VPS 中 Codex 的第一入口。目标不是重新设计系统，而是在 Debian 13 上把仓库 `main` 部署成与已验收本地版本相同的业务效果，并用真实运行证据证明结果。

目标仓库：`https://github.com/413hy/trading-cheap-bybit.git`

## 1. 给 VPS Codex 的任务

你负责在用户指定的 Debian VPS 上完成安装、配置、验证和 systemd 常驻运行。开始前依次阅读：

1. 本文件；
2. `AGENTS.md`；
3. `docs/CURRENT_DOCUMENTS.md`；
4. `docs/DEVELOPMENT.md`；
5. `docs/ARCHITECTURE.md`；
6. `docs/DEPLOYMENT_VPS.md`；
7. `.agents/skills/analyze-bybit-ultrashort-signals/SKILL.md` 与其 `references/operating-contract.md`；
8. `docs/TEST_REPORT.md`。

只做 Debian 环境适配、Secret 配置、安装和验收。不要根据历史规格或参考项目重新实现业务逻辑；不要修改 Prompt、策略版本、Top5/Top2 合同、阈值语义或 Telegram 键盘，除非真实 Debian 兼容性问题有明确证据，而且修复后完成全部回归测试。

## 2. 当前不可改变的运行合同

- 只分析公开市场数据并发送 Telegram 方向信号，不读取账户、余额、仓位、订单、杠杆或盈亏，不自动交易。
- 每个自然 `:00/:30` 运行一次完整周期；服务启动后立即补跑一轮。
- 全市场过滤出 5 个高波动且流动性可用的 Bybit USDT 线性永续，深采后由模型分析全部五币。
- 成功定时轮固定通知 2 个相对最值得参考的主信号，不发送 WATCH 凑数通知。
- 默认 `gpt-5.6-terra / medium`，单次硬超时 300 秒，模型输出技术失败时最多两次尝试。
- 当前策略与 Prompt：`ultrashort-v5 / signal-analysis-v9`。
- 每条主信号包含方向、可信度、参考价、市场状态、大致止盈展示位、形成中 15m/30m/1h、下一根 15m、相比上一轮、正式方向失效结构和摘要。
- 大致止盈位仅展示，不能参与方向、排序、阈值、生命周期或失效判断。
- 阈值只表示方向结构受到反向威胁，需要重新采集并让模型复核；crossing 本身不等于方向失效。
- 每币允许 0–3 条阈值。模型返回 `REJECTED` 表示当前没有高质量阈值，是正常零规则结果，不重试、不报错、不影响两个主方向信号。
- 同一 `(analysis_id, symbol)` 首个完整 crossing 会原子退役整组旧规则并通知；后续是否分析由 Telegram 内嵌键盘交给用户选择。重新分析成功后发布新 analysis ID 和新阈值。
- 旧规则一直有效到自然定时任务真正开始，只有在 `:00/:30` 边界才冻结；紧急流程和定时流程各自运行，以最新成功定时周期作为权威版本。
- Telegram 主导航必须保持 `ReplyKeyboardMarkup`：`resize_keyboard=true`、`is_persistent=false`、`one_time_keyboard=false`；禁止 `ReplyKeyboardRemove` 和 `remove_keyboard=true`。

## 3. 需要用户在 VPS 本地提供的内容

仓库不包含、也不应该包含：

- Telegram Bot Token；
- Telegram Chat ID 和 User ID 白名单；
- Codex/ChatGPT 登录状态或 OpenAI API Key。

如果缺少其中任一项，明确告诉用户缺少什么、在哪个 VPS 文件或命令中配置，然后等待用户完成。不得把 Secret 回显到对话、日志、Git diff 或提交中。

## 4. Debian 13 基础安装

以具有 sudo 权限的管理员执行：

```bash
sudo apt-get update
sudo apt-get install -y \
  git curl ca-certificates tzdata \
  python3 python3-venv python3-dev build-essential

id -u bybit-signal >/dev/null 2>&1 || \
  sudo useradd --create-home --shell /bin/bash bybit-signal
sudo install -d -o bybit-signal -g bybit-signal -m 0750 /opt/bybit-signal
sudo install -d -o bybit-signal -g bybit-signal -m 0700 /home/bybit-signal/.codex
```

如果 `/opt/bybit-signal` 为空：

```bash
sudo -u bybit-signal -H git clone \
  https://github.com/413hy/trading-cheap-bybit.git \
  /opt/bybit-signal
```

若目录中已有仓库，不要删除或强制 reset。先检查状态，再按 `docs/DEPLOYMENT_VPS.md` 的安全更新流程处理。

## 5. Python 环境

```bash
cd /opt/bybit-signal
sudo -u bybit-signal -H python3 -m venv .venv
sudo -u bybit-signal -H .venv/bin/python -m pip install --upgrade pip
sudo -u bybit-signal -H .venv/bin/python -m pip install \
  -c requirements-debian.lock.txt '.[dev]'
sudo -u bybit-signal -H .venv/bin/python -m pip check
```

`requirements-debian.lock.txt` 是 Debian 13/Python 3.13 专用约束文件，已移除 Windows-only `pywin32`。若安装失败，保存完整包名、版本、Python 版本和错误原因；不得擅自删除依赖约束后继续部署。

## 6. Codex CLI 安装与认证

本项目已用 `codex-cli 0.147.0` 验证。可以安装更新版本，但下列命令行能力必须仍存在：`exec`、`--ephemeral`、`--ignore-user-config`、`--ignore-rules`、`--output-schema`、`--output-last-message` 和重复 `--disable`。

按 OpenAI 官方安装方式，以服务用户安装：

```bash
sudo -u bybit-signal -H bash -lc \
  'curl -fsSL https://chatgpt.com/codex/install.sh | sh'
sudo -u bybit-signal -H bash -lc 'command -v codex && codex --version'
sudo -u bybit-signal -H bash -lc 'codex exec --help' | grep -E \
  -- '--ephemeral|--ignore-user-config|--ignore-rules|--output-schema|--output-last-message'
```

无头 VPS 优先使用设备码登录：

```bash
sudo -u bybit-signal -H bash -lc 'codex login --device-auth'
sudo -u bybit-signal -H bash -lc 'codex login status'
```

如果用户选择 API Key 计费，按官方方式从环境变量经标准输入登录，不要把 Key 写入仓库或 shell history：

```bash
printenv OPENAI_API_KEY | sudo -u bybit-signal -H codex login --with-api-key
```

`/home/bybit-signal/.codex/auth.json` 等价于密码文件，只允许服务用户读取，禁止复制到仓库。官方参考：

- <https://learn.chatgpt.com/docs/codex/cli>
- <https://learn.chatgpt.com/docs/auth>

## 7. 本地配置

```bash
cd /opt/bybit-signal
sudo -u bybit-signal -H cp config/system.example.yaml config/system.local.yaml
sudo -u bybit-signal -H cp .env.example .env
sudo chmod 600 .env config/system.local.yaml
sudo chown bybit-signal:bybit-signal .env config/system.local.yaml
```

在 `config/system.local.yaml` 中只改部署所需值：

- `telegram.enabled: true`；
- `telegram.allowed_chat_ids`；
- `telegram.allowed_user_ids`。

在 `.env` 中填写 `BYBIT_SIGNAL_TELEGRAM__TOKEN`。默认模型、推理强度、Prompt/策略版本、Top5/Top2、扫描门槛和监测配置保持示例文件当前值。

## 8. 启动前完整验收

按顺序执行，任何一步失败都先解释和修复，再继续：

```bash
cd /opt/bybit-signal

sudo -u bybit-signal -H .venv/bin/python -m ruff check .
sudo -u bybit-signal -H .venv/bin/python -m mypy src tests
sudo -u bybit-signal -H .venv/bin/python -m pytest -q
sudo -u bybit-signal -H .venv/bin/python -m compileall -q src

sudo -u bybit-signal -H .venv/bin/python -m bybit_signal \
  config-check --config config/system.local.yaml
sudo -u bybit-signal -H .venv/bin/python -m bybit_signal \
  telegram-check --config config/system.local.yaml --send-test
sudo -u bybit-signal -H .venv/bin/python -m bybit_signal \
  market-capture --config config/system.local.yaml --symbol CYSUSDT
sudo -u bybit-signal -H .venv/bin/python -m bybit_signal \
  run-cycle --config config/system.local.yaml --no-notify
sudo -u bybit-signal -H .venv/bin/python -m bybit_signal \
  run-cycle --config config/system.local.yaml --notify
```

真实周期至少确认：

- 扫描覆盖 Bybit 全量可交易 USDT 线性永续并过滤出 Top5；
- Top5 深采包含完成 1m/5m/15m/30m/1h/4h、盘口、成交、OI、funding 和市场背景；
- 周期状态为 `SUCCESS`，主信号恰好 2 条；
- Telegram 收到核心结论，详情按钮和返回按钮可用；
- 阈值复核失败或零规则不会清空两个主方向信号；
- 没有泛化且无原因的 `ANALYSIS_FAILED`。

市场会变化，所以币种和方向不要求与本地历史样本相同；要求的是流程、Schema、通知格式和生命周期合同相同。

## 9. systemd 上线

确认 `command -v codex` 位于 unit 的 PATH 中，然后执行：

```bash
sudo cp /opt/bybit-signal/deploy/systemd/bybit-signal.service \
  /etc/systemd/system/bybit-signal.service
sudo systemd-analyze verify /etc/systemd/system/bybit-signal.service
sudo systemctl daemon-reload
sudo systemctl enable --now bybit-signal.service
sudo systemctl status --no-pager bybit-signal.service
sudo journalctl -u bybit-signal.service -n 200 --no-pager
```

unit 允许写入 `/opt/bybit-signal/runtime` 和 Codex 登录缓存目录，项目代码保持只读；停止服务时由 systemd control group 清理全部子进程，模型自身超时也会终止独立 POSIX 进程组。

## 10. 生产效果验收

不要只看到 `active (running)` 就宣布完成。至少继续观察到：

1. 启动补跑成功且只投递一次 Telegram 主通知；
2. 下一个自然 `:00` 或 `:30` 周期准点开始；
3. 该周期成功产生恰好两个主信号；
4. SQLite 有相应 cycle、conclusion 和 delivery 记录；
5. 日志无重复调度器、孤儿 Codex、无原因失败或旧阈值连续刷屏；
6. 如果模型给出阈值，监测器成功激活；如果模型拒绝阈值，日志将其视为正常零规则结果；
7. Telegram ReplyKeyboard 收起后可由输入框旁的小键盘按钮重新展开，详情 InlineKeyboard 不会移除主键盘。

可用命令：

```bash
sudo systemctl status --no-pager bybit-signal.service
sudo journalctl -u bybit-signal.service --since '1 hour ago' --no-pager
sudo tail -n 200 /opt/bybit-signal/runtime/logs/service.log
sudo pgrep -a -f 'bybit_signal serve|codex exec'
```

最终向用户报告：远端 commit SHA、Debian/Python/Codex 版本、配置检查、测试数量、真实 no-notify/notify analysis ID、Top5/Top2 数量、Telegram 投递、阈值状态、systemd 状态和下一次自然周期结果。任何未验证项必须明确列出，不得用推测替代。

## 11. 失败处理原则

- `CODEX_AUTH_REQUIRED`：检查服务用户而不是管理员用户的 `codex login status`。
- `CODEX_MODEL_UNAVAILABLE`：不要替换模型，向用户确认该账号是否有 `gpt-5.6-terra` 权限。
- `CODEX_TIMEOUT`：检查网络和模型状态；程序会结束完整进程组并按剩余尝试重试。
- Telegram 失败：检查服务用户读取 `.env` 的权限、Bot Token、chat/user 白名单和 Bot 会话。
- Bybit 失败：检查 DNS、HTTPS/WebSocket 出站及 VPS 所在地区限制；不要改为私有 API。
- Binance/OKX 失败：它们是可选旁证；Bybit 主证据合格时允许降级，但必须在详情披露。
- 阈值 `REJECTED`：正常，不修、不重试、不通知异常。
- systemd 重启循环：先 `systemctl stop`，读取完整 journal 和应用日志，定位精确步骤后修复；不要反复启动制造重复通知。

如果确实需要改代码，先备份数据库、创建独立 Git 提交、运行本文件第 8 节全部检查和第 10 节真实场景，再重新启动服务。
