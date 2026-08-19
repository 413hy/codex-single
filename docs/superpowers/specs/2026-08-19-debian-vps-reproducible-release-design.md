# Debian VPS 可复现发布设计

日期：2026-08-19

目标仓库：`413hy/trading-cheap-bybit`

目标分支：`main`

## 1. 目标

把当前本地已验证、正在运行的 Bybit 超短线方向信号系统完整发布到目标仓库，并让 Debian VPS 上的 Codex 可以只依赖仓库内的说明、示例配置和验收命令，部署出与本地当前版本一致的业务效果。

“一致效果”指业务合同和可观察行为一致，不要求 Windows 与 Debian 的进程号、路径或时间戳一致：

- 每 30 分钟执行一次 `全市场过滤 → Top5 深度采集 → 模型分析 → Top2 Telegram 通知`；
- 默认模型为 `gpt-5.6-terra / medium`，分析超时 300 秒；
- 策略/Prompt 版本为 `ultrashort-v5 / signal-analysis-v9`；
- 方向分析是系统首要目标，大致止盈位只作展示；
- 实时阈值只监测方向结构受到反向威胁，触发后旧阈值立即失效，通知用户并由用户通过 Telegram 内嵌键盘决定是否重新分析；
- 阈值触发不自动等于方向失效，模型重新采集和复核后才给出新结论与新阈值；
- 定时流程与阈值流程相互独立，定时任务到点才暂停旧阈值并以新周期结果覆盖；
- 不读取账户、仓位或私有交易数据，不自动下单。

## 2. 发布边界

### 2.1 必须发布

- `src/` 中的完整当前源码；
- `prompts/` 中的方向分析与阈值复核 Prompt；
- `.agents/skills/analyze-bybit-ultrashort-signals/` 项目 Skill 及运行合同；
- `config/system.example.yaml` 和 `.env.example`；
- `deploy/`、Debian/systemd 部署文件及服务管理说明；
- `scripts/`、依赖锁文件、`pyproject.toml`；
- 全部单元测试、场景测试和当前测试报告；
- 当前权威需求、架构、开发、模型评估、VPS 部署和 Codex 交接文档。

### 2.2 禁止发布

- `.env`、`config/system.local.yaml`；
- Telegram Bot Token、Chat ID、OpenAI/Codex 凭据；
- `~/.codex/auth.json` 或任何 Codex 登录缓存；
- `runtime/`、数据库、日志、备份、模型原始输出和本地行情快照；
- `.venv/`、构建产物、缓存和编辑器状态。

发布前必须同时检查 Git 跟踪集合、待提交差异和文件内容，不能只依赖 `.gitignore`。

## 3. 仓库与文档结构

目标仓库使用 `main` 作为唯一初始正式分支。当前本地工作树的全部有效修改会形成可审计提交并推送，不以旧 `HEAD` 代替最新工作树。

文档权威顺序：

1. `VPS_CODEX_HANDOFF.md`：VPS Codex 的第一入口，说明任务边界、执行顺序、必要配置、验收标准和禁止事项；
2. `docs/DEPLOYMENT_VPS.md`：Debian 人工部署、systemd、更新、回滚和故障定位；
3. `docs/CURRENT_DOCUMENTS.md`：当前文档导航及新旧文档权威关系；
4. `docs/DEVELOPMENT.md` 与 `docs/ARCHITECTURE.md`：业务合同、模块职责和数据流；
5. `docs/TEST_REPORT.md`：本地、灰盒和生产验收证据。

历史设计文档保留作演进审计，但必须明确不具有当前运行权威，防止 VPS Codex 重新引入目标驱动失效、提前五分钟冻结、WATCH 通知、少于两个主信号或宿主擅自调节阈值等旧语义。

## 4. Debian VPS 部署设计

部署基线为当前稳定版 Debian，使用非 root 专用服务用户、Python 虚拟环境和 systemd 长期运行：

1. 安装 Git、Python、venv、编译/时区等基础依赖及 Codex CLI；
2. 克隆 `main`，创建 `.venv`，按锁文件安装依赖；
3. 从示例文件创建仅存于 VPS 的 `.env` 与 `config/system.local.yaml`；
4. 完成 Codex CLI 登录并以 `codex login status` 验证；
5. 运行配置检查、测试和 Telegram 检查；
6. 安装并启动 systemd unit；
7. 通过日志、SQLite 落库、Telegram 通知和下一次半小时周期完成生产验收。

Codex CLI 安装和认证以官方 OpenAI 文档为准。无头 Debian 优先使用 `codex login --device-auth`；程序化环境也可通过标准输入使用 API Key 登录，但凭据只保存在 VPS 安全环境中，永不进入仓库。官方文档明确要求把 `~/.codex/auth.json` 视为密码文件。

## 5. VPS Codex 交接合同

交接文档必须给 VPS Codex 一段可以直接执行的任务说明，要求它：

- 先阅读权威文档和项目 Skill，再检查 Debian 实际环境；
- 只做环境适配，不重写已经通过验证的业务逻辑、Prompt 和阈值语义；
- 缺少 Token、Chat ID 或 Codex 登录时明确向用户索取/提示，由用户在 VPS 本地配置；
- 不从仓库历史、历史规格或参考项目恢复旧实现；
- 所有命令失败时说明失败步骤、原因、修复动作和复验结果；
- 部署完成后执行完整验收矩阵并报告证据，而不是只报告 systemd 为 `active`。

## 6. 验证与验收

### 6.1 推送前质量门

- 敏感信息扫描无真实 Token、Chat ID 或 Codex 凭据；
- `git diff --check`；
- Ruff、严格 mypy、`pip check`、`compileall`；
- 本地/示例配置检查；
- 完整 pytest 测试集通过；
- PowerShell 与 systemd/部署文件静态检查；
- 正常周期、阈值触发、阈值拒绝、失败通知、Telegram 键盘等代表性场景通过。

### 6.2 推送后仓库验收

从远端 `main` 重新克隆到独立临时目录，确认：

- 远端提交包含本次完整源码和文档；
- 禁止文件不存在；
- 示例配置可通过校验；
- 锁定依赖可安装；
- 测试可从干净克隆运行；
- `VPS_CODEX_HANDOFF.md` 能让一个没有本地会话上下文的 Codex 明确完成部署和验收。

受限于本机不是 Debian VPS，systemd 的真实常驻、VPS 网络质量和 VPS 端 Codex 账号权限只能由 VPS Codex 在目标机最终验证；交接文档必须把这些列为上线必过项，不得把本地静态检查冒充生产验收。

## 7. 发布与回滚

- 目标仓库为空，因此直接建立并推送 `main`，不创建 PR；
- 推送后记录远端提交 SHA；
- VPS 更新前先备份本地配置和 `runtime/state/signal.db`；
- 回滚以 Git 提交 SHA 为代码边界，配置和数据库单独恢复；
- 任何回滚都不得把 Secret 写回 Git，也不得用历史文档覆盖当前业务合同。

## 8. 成功标准

仓库同步完成的判定不是“push 成功”，而是同时满足：

1. 远端 `main` 与本地最新有效工作树一致；
2. Secret 和运行数据未泄漏；
3. Debian/VPS Codex 文档无上下文也可执行；
4. 干净克隆通过规定检查；
5. VPS 端按文档完成真实半小时周期、Top2 Telegram 通知和阈值生命周期验收后，才可认定部署效果与当前系统一致。
