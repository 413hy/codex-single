> 重构前历史说明，当前运行方式以README.md为准。

# Bybit Longtime

独立的 **Bybit Mainnet Demo Trading** 自动交易系统。私有HTTP连接固定为
`https://api-demo.bybit.com`，不会切换正式盘或Testnet。

本项目复用VPS原选币器和公开行情客户端，抽取原Bybit签名传输，并重构成独立的执行、持久化和通知模块。
原项目位置、基线和取舍见 [复用清单](docs/REUSE.md)。无旧项目运行时Python导入或数据库依赖。

## 策略

1. 启动时核对Bybit余额、当前仓位和订单，恢复自己尚未确认的订单意图。
2. 按保存的周期轮询间隔运行选币，默认20分钟（自然`:00/:20/:40`）。启动补跑当前时间桶，同一桶不会重复执行。
3. 任意方向已有真实仓位的币在采集单币行情/GPT之前跳过。尚未查清的开仓意图也会阻止重复开仓。
4. 保留原选币器60币预采池、成交量、点差、波动、盘口及异常行情评分。参考信号系统：取前10个候选采集公开行情，按筛选专用3m/5m/15m/30m/1h/4h六周期的证据完整度优先、原排名次序选出6份有效证据；筛选模型横向复核后选0—3个，再进入方向分析。不强制凑数，不递补，不另设持仓上限。
5. 固定`gpt-5.6-terra`、`medium`，无工具、严格Schema。15m/30m/1h/2h各取完整一周已收盘背景，不足一周直接SKIP，不发送方向模型、不递补；1m/3m/5m/10m取近12小时辅助。以周内量价结构判断趋势，成交量、成交额、分日VWAP和高成交K线优先，指标辅助；不把K线方向量冒充主动买卖量或逐价成交分布。10m由UTC对齐5m聚合。另采三次盘口快照及最多1000笔近300秒主动成交，补充局部量价反应；明确不等于连续DOM或完整Footprint。Prompt版本direction-v10-orderflow-evidence-20260915。
6. 程序用可执行报价计算当前默认净TP0.5U（可在开仓设置修改），检查最近24小时1m行情：仅已收盘、有成交且整根价格都超过TP外侧轻量缓冲的分钟计时，累计严格超过300秒才通过，否则跳过。多单看最低价、空单看最高价；影线、等于TP、未收盘及重复分钟不计。该时间是保守下界，不是逐笔精确时长；原1—2 tick缓冲保留。持仓后原TP仍由交易所执行。
7. 单笔保证金约10U，按合约精度允许±0.50U；超出则跳过。新仓3倍杠杆（合约上限不足3倍时按上限，通常约30U名义金额）。全仓，兼容现有单向/双向模式，不修改账户持仓模式。
8. 每笔重新读取余额、保证金状态、仓位和订单，预留约20U可用保证金及费用余量，再市价开仓。
9. 读取实际成交订单、成交数量、均价和开仓手续费，固定计算TP与约2.7U净亏损SL；先挂Reduce-Only/Close-On-Trigger条件市价SL，再挂Reduce-Only GTC限价TP。
10. 之后不再分析或主动管理持仓，只等待TP/SL。持仓没有时间上限。

正常报价是公开主网行情，私有账户和执行始终是Demo。轮询间隔不是持仓期限。
市价单可能部分成交；只保护实际成交数量，不补仓凑10U。若极小部分成交导致本金本身不足2.7U，LONG止损最多到合法最小tick，损失预算不会为凑2.7U而扩大。

### 成本与利润口径

Demo官方可用接口不含`/v5/account/fee-rate`，本机真实调用返回`10001`。
采用固定保守估算：开仓/止损taker 0.11%、限价TP maker 0.04%，另计0.05%名义金额滑点余量。
这些数值覆盖本次Demo历史成交中观察到的0.055%和0.11% taker费率；参考[Bybit手续费表](https://www.bybit.com/en/help-center/article/Trading-Fee-Structure)。
开仓后使用实际开仓手续费重算。价格步长使预计TP超过0.55U时跳过，不降低0.5U目标。新仓意图持久化2.7U止损预算；已有仓位及旧意图沿用原始参数。
GTC限价单争取挂单成交，但不保证maker；实际费用以成交记录为准。

`realized_pnl`记录价格毛PnL，`fees`记录真实开平仓交易费，`net_pnl`采用Bybit实际`closedPnl`汇总，**不再重复扣费**。
`details.other_pnl_adjustment`保留交易所净值与“价格毛PnL减交易费”的差额，不擅自把它全部标为资金费。
不限时持仓可能经历资金费用，因此预设TP是费用估算下的目标，不保证最终净值恰好在区间内，也不会因此移动已有TP。

## 故障和重试按钮

- 新仓每个保护单首次失败后仅自动重试一次。次数在发送请求前写入数据库，重启不刷新预算。
- 不因TP/SL失败自动市价平仓，也不在30秒任务中无限补挂。
- 所有故障通知附“🔄 重试”内嵌按钮。只有`.env`绑定的chat和user同时匹配才能操作。
- 每次点击仅执行一次；点击重复回调、重复投递、已成功订单、已结束仓位都有幂等检查。
- TP/SL重试核对原仓位身份、原目标价格和当前订单。已有单不重复挂；确认旧单取消/拒绝后才生成新订单ID，补挂剩余数量的原目标保护单。
- 模型容量或额度故障同轮只报警一次并停止剩余模型请求，下一轮或按钮重新取行情；固定模型不自动切换。
- 行情/GPT重试先重新运行选币和取新行情，再判断方向。不会重放旧方向。
- 开仓超时先查原`orderLinkId`，不会因按钮点击直接再发一次开仓单。查不清时保持INTENT并报警，避免重复开仓。
- Telegram投递失败只自动重试一次，保留失败outbox，可用按钮或`/retry_delivery`恢复。
- SQLite异常有独立文件告警通道，数据库打不开时仍能发送一次含重试按钮的告警；若磁盘与Telegram也都不可用，只能保留systemd日志。
- 首次发现外部已有仓位只观察、跳过和报警，不补挂未知的原始价格，不接管旧策略。其结束写入观察事件；本项目不能凭空重建外部交易的完整开仓费用和时间，原交易系统账本应保留。

30秒任务只观察状态、检查保护单存在性、核对成交和记录平仓。仓位结束后取消**该生命周期自己的**剩余保护单，确认撤单后才释放该币，避免旧单影响之后的新仓。
若行情已平仓但订单/盈亏记录暂未同步，保持SETTLING，不伪造交易结果。部分平仓会逐笔汇总后再记录整仓结束。
平仓后的正常历史同步等待期为5分钟，期间每30秒继续核对；超过5分钟仍缺订单、盈亏或逐笔成交才去重提醒。等待起点和缺失原因持久化，重启不重置；接口异常仍及时报警。

## 项目结构

```text
src/longtime/
  vendor/         原程序选币器、行情客户端、最小行情模型、来源哈希
  transport.py    原签名代码及Demo/只读写入边界
  exchange.py     Bybit账户、订单、仓位、成交、分页
  risk.py         固定保证金、新仓3x、净TP/SL、24h可达性
  screening.py    信号系统前10→6份证据→模型选0—3个
  model.py        无工具Codex调用及严格方向Schema
  market.py       新行情采集与缺失/过期检查
  execution.py    持久开仓意图、真实成交核对、一次重试保护单
  monitor.py      启动恢复、被动观察、真实平仓核账
  store.py        SQLite WAL及事件outbox
  telegram.py    重要事件、故障去重、绑定用户回调
  emergency.py   SQLite故障告警后备
  service.py     自然时间周期、独立观察/通知任务、进程锁
  cli.py          命令行入口
```

模型prompt只随人工代码修改更新，程序不会自行调参、学习或改prompt；新仓参数只通过用户明确保存的开仓设置修改。

独立账户和Bot已确定，具体填写位置和获取ID方法见 [凭据填写说明](docs/CREDENTIALS.md)。

## 安装与配置

本机验证：Linux、Python 3.11.2、Codex CLI 0.153.4。项目Python要求≥3.11。

```bash
cd /root/auto-trader-longtime
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock.txt -e '.[dev]'
cp .env.example .env
chmod 600 .env
```

在VPS本地编辑`.env`，不要把密钥提交到Git或粘贴到公共日志：

```dotenv
BYBIT_API_KEY=Demo账户API密钥
BYBIT_API_SECRET=Demo账户API密钥Secret
TELEGRAM_TOKEN=本项目独立Bot的Token
TELEGRAM_CHAT_ID=接收消息的Chat_ID
TELEGRAM_USER_ID=允许点击按钮的User_ID
CODEX_BIN=/root/.local/bin/codex
RUNTIME_DIR=runtime
TRADING_ENABLED=false
```

需要服务用户已登录Codex、且账号支持`gpt-5.6-terra`。
本机复用现有Codex认证，不复制认证文件到项目。可执行：

```bash
/root/.local/bin/codex login status
```

固定模型对应CLI的`--model gpt-5.6-terra -c 'model_reasoning_effort="medium"'`，不把展示名`gpt-5.6-terra-medium`误当作另一个模型。
接口调用使用官方[非交互与Schema输出方式](https://developers.openai.com/codex/noninteractive)。

本次部署明确使用**独立Demo账户和独立Bot**；旧服务保持运行，不复用旧账户密钥或Bot Token。
同一Demo账户的新API Key并不提供账户隔离，需要使用不同的Demo账户。

## 启动前检查

```bash
.venv/bin/bybit-longtime config-check
.venv/bin/bybit-longtime preflight
.venv/bin/bybit-longtime scan
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src
systemd-analyze verify deploy/bybit-longtime.service
```

`preflight`只读取账户、仓位、订单和Bot身份，不下单、不改菜单、不删除webhook。
`scan`只采集公开行情。`TRADING_ENABLED=false`在传输层禁止任何私有写请求。

只读完整周期可运行：

```bash
.venv/bin/bybit-longtime cycle
```

此命令运行本时间桶；同一时间桶重复运行不会重复分析。测试数据也保留在数据库。
`TRADING_ENABLED=true`后该命令会执行真实Demo交易；不要把它当纯诊断命令。

## 真实Demo验收

在独立账户/Bot配置妥当、静态检查通过后，将`.env`的`TRADING_ENABLED`改为`true`。
先运行控制验收脚本，它只允许一个按照完整策略自然通过的候选开仓，不强制方向、不改小TP、不主动市价清仓：

```bash
.venv/bin/python scripts/accept_demo.py --wait-seconds 600
```

验收记录位于`runtime/demo-acceptance.json`。必须区分：

- 真实方向、账户、开仓成交和TP/SL存在已验证；
- 真实TP/SL自然结束、平仓核账和Telegram平仓通知已验证；
- 到达等待时间仍持仓，只记录“平仓验收待自然结束”，不伪装通过。

脚本退出保留交易所保护单，随后启动常驻服务继续观察。上线验收还应观察下一个自然20分钟周期，以及重启后相同币跳过、无重复下单。
不要为了验收平仓而移动TP/SL或提前市价退出。

## 当前VPS验收状态

独立账户与Bot已填写，本机已完成真实Demo自然TP退出，详情见[验收记录](docs/ACCEPTANCE.md)。2026-09-09按用户授权已启用Demo自动开仓及systemd开机启动。开发维护约定见[AGENTS.md](AGENTS.md)，策略、接口和运行规则见[规范清单](docs/OPERATING_RULES.md)。

正式启用本Demo策略时：将`.env`中的`TRADING_ENABLED`设为`true`，在绑定Bot发送`/resume`，执行`systemctl enable bybit-longtime.service`和`systemctl restart bybit-longtime.service`。仅`enable --now`不会重载已经运行的服务配置。

## systemd常驻

本机unit默认安装目录为`/root/auto-trader-longtime`；迁移路径时相应修改WorkingDirectory、ExecStart和ReadWritePaths。

```bash
mkdir -p runtime
cp deploy/bybit-longtime.service /etc/systemd/system/bybit-longtime.service
systemctl daemon-reload
systemctl enable --now bybit-longtime.service
systemctl status bybit-longtime.service --no-pager
```

一条命令看日志：

```bash
journalctl -u bybit-longtime.service -f
```

一条命令重启：

```bash
systemctl restart bybit-longtime.service
```

systemd提供开机启动、崩溃后30秒重启、进程组退出。进程文件锁防止同时启动两份同runtime实例。
日志同时写journal和`runtime/service.log`，单个文件10MB、保留10份轮转备份。
SQLite与全部交易证据保留，不自动清理；应按使用量监测磁盘并人工备份，不删除账本来释放重复交易限制。

正常交易仅发送一条成交开仓通知和一条最终平仓通知；不发送交易机会预告。通知使用中文方向，合并展示关键价格；保护单故障仍单独报警并附重试按钮。

### Telegram操作

底部键盘六键：当前持仓、运行状态、最近交易、暂停/恢复开仓、最近异常、开仓设置。最近异常显示最近5项未解决异常及重试按钮，无异常回复“最近无异常”。最近分析、异常处理、策略说明和重发失败通知保留命令入口。主导航统一ReplyKeyboardMarkup，resize_keyboard=true、is_persistent=false、one_time_keyboard=false；可收起后用输入框旁键盘图标再展开。禁止发送键盘移除标记；InlineKeyboard与编辑消息不清除主导航，重启不重置聊天键盘。实际图标显示由Telegram客户端决定。异常按币种说明原因、影响和处理方法，保留短编号与单次重试按钮；完整诊断保存在账本。

- `/status`：运行状态、持仓数；
- `/history`：最近真实平仓；
- `/pause`：暂停新开仓，已有TP/SL保留；
- `/resume`：恢复新开仓；
- `/retry_delivery`：人工重试失败通知；
- 故障下方按钮：仅重试该故障对应动作。

## 数据与恢复

SQLite：`runtime/trader.db`。主要表为cycles、signals、trades、orders、incidents、outbox、events、callbacks、state。
金额和数量存十进制字符串，时间存UTC Unix秒，行情证据内保留带时区时间戳。
trades包含需求中的全部字段；cycle、signal和orderLinkId能够关联原始选币、模型输入输出和交易回执。

启动以交易所为准，真实持仓不会因本地无记录而重复开仓。INTENT只按原订单ID核对；OPEN缺保护单只报警，等待按钮。
原始TP/SL一经按真实成交确定即冻结，不随市场、持仓时间或后续模型输出变化。

Bybit Demo文档说明历史订单保留7天。因此程序及时落地成交和订单证据；跨周持仓的开仓证据已经在本地。
若服务离线超过交易所历史保留期、或原数据库丢失，历史数据可能无法补全：保留待核账及报警，不能编造净PnL或重放未知订单。
见[Demo服务限制](https://bybit-exchange.github.io/docs/v5/demo)。

安全备份（SQLite在线backup，不直接只复制可能落后于WAL的主文件）：

```bash
.venv/bin/python - <<'PY'
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
Path('runtime/backups').mkdir(exist_ok=True)
source = sqlite3.connect('runtime/trader.db')
target = sqlite3.connect('runtime/backups/trader-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '.db')
source.backup(target)
target.close()
source.close()
PY
```

最新逐文件审核、模型复核和修复说明见[全系统审核报告](docs/FILE_AUDIT.md)。


最新执行补充（2026-09-10用户授权）：开仓终态订单已提供真实成交量、均价和USDT累计手续费时，立即按这些交易所数据建立保护，不等待逐笔成交历史；逐笔证据稍后只读补齐，不移动TP/SL。提交/重试保护前若最新成交价已到达或越过原TP或SL，核对原仓位剩余数量后改用Reduce-Only、Close-On-Trigger市价退出；不确定请求先核对原ID，不改其载荷。每条保护腿仍最多两次自动提交，耗尽后人工单次重试；不新增后台无限重试。已有有效保护单继续由交易所执行。明确拒单保留状态和原始错误，监控不覆盖具体原因。该原目标退出是对旧“失败不市价退出”规则的明确例外；其他主动持仓管理仍禁止。


### 底部开仓设置

点击“⚙️ 开仓设置”（或 `/settings`）可修改每笔保证金、每笔总价值、默认杠杆、净止盈目标、净止损预算和周期轮询间隔。初始值保持保证金10U、3倍、TP0.5U、SL2.7U。总价值指单笔杠杆后名义金额；输入总价值按当前杠杆换算保证金，向下保留8位小数。修改杠杆保留保证金，因此总价值随之变化。实际数量仍按合约精度、最小规模与可用资金限制执行；20U预留和最多5倍硬上限保持。

选择项目→输入正数→核对完整预览→保存。仅保存后生效，可取消，预览10分钟有效。配置持久化SQLite，重启保留，无需重启改参；重复按钮、旧预览及非绑定用户不能修改。保存、审计记录和回复同事务。已有仓位和已提交意图使用自己的冻结参数；下单准备期间若参数版本变化，该候选正常SKIP，避免混用版本。每笔新意图保存完整配置和版本，便于复盘。

周期轮询间隔默认20分钟，可填写1—1440的整数分钟，预览后保存。时间边界按UTC Unix时间对齐；例如5分钟为每小时`:00/:05/:10…`，60分钟为整点。修改后从保存后的下个边界生效，不立即补跑当前桶；正在运行的周期保持原截止时间。启动仍补跑当前有效桶且按间隔与时间桶去重，不重放历史桶。配置保存后无需重启，运行状态展示当前间隔及下次筛选时间。30秒仓位观察频率不受此设置影响。

Telegram轮询故障首次仍报警；间歇超时期间沿用同一异常，完整批次连续正常5分钟后解除。失败轮询采用最长30秒退避，重启重新观察恢复；消息投递和交易提交的持久重试预算保持不变。

模型服务故障按容量、额度、认证、限流、连接和超时分别说明。共同服务故障同轮停止后续模型请求；下一轮或人工重试重新取行情。有效筛选选0个也可解除旧模型故障，未通过Schema的结果不能证明恢复。最近异常显示原发生时间；历史未处理故障不会自动删除。
