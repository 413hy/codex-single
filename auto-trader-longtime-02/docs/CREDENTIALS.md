> 原项目复制资料：以下部署状态、账户验收和旧止损策略不代表新系统。新系统以 README.md 和 HEDGE_STRATEGY.md 为准。

# 独立Demo账户和独立Telegram Bot配置

已确认部署采用独立账户和Bot，旧ai-trader服务继续运行，新项目不读取其凭据。
配置文件已创建：`/root/auto-trader-longtime/.env`，权限0600。

## 1. Bybit Demo API

在**独立的Demo账户**登录，切换到 **Demo Trading**，再进入头像/API管理创建API Key和Secret。
不是Testnet，也不是正式交易账户的API Key。同一Demo账户再创建一把Key仍然共享余额、仓位和订单，并不等于独立账户。

API需要查询账户/仓位/订单，以及合约下单、撤单、设置杠杆的读写权限；账户使用全仓，准备Demo USDT。
若设置IP白名单，填写这台VPS的出口IP。至少应有20U保留余额，加约10U单笔保证金及费用余量。

官方：[Demo账户创建密钥和端点说明](https://bybit-exchange.github.io/docs/v5/demo)。

## 2. 独立Bot

Telegram打开官方 **@BotFather**，发送`/newbot`，按提示创建新Bot并取得Token。
打开新Bot的私聊，发送`/start`。

官方：[获取Bot Token](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)。

## 3. 本地填写

在VPS终端执行：

```bash
nano /root/auto-trader-longtime/.env
```

填写等号右侧；不要把密钥发到聊天里，不要删除原有其他配置行：

```dotenv
BYBIT_API_KEY=你的独立Demo_API_Key
BYBIT_API_SECRET=对应的API_Secret
TELEGRAM_TOKEN=新BotFather给你的Token
TELEGRAM_CHAT_ID=你的数字Chat_ID
TELEGRAM_USER_ID=你的数字User_ID
CODEX_BIN=/root/.local/bin/codex
RUNTIME_DIR=runtime
TRADING_ENABLED=false
```

nano保存：Ctrl+O，回车；退出：Ctrl+X。

如果不知道两个Telegram ID，可以先填写前三项，两个ID暂时保持0。
向新Bot私聊发送`/start`后，在VPS执行：

```bash
cd /root/auto-trader-longtime
.venv/bin/python scripts/telegram_ids.py
```

输出仅包含Chat ID和User ID，不显示Token，不发送消息。把输出的两个数字填写回`.env`。
若有多组ID，只选择你自己发送`/start`的那组。个人私聊时两者通常相同，仍按实际输出填写。
该工具应在新服务启动前使用，避免与正式Bot轮询同时运行。

## 4. 填完后

先保持`TRADING_ENABLED=false`，运行只读预检：

```bash
cd /root/auto-trader-longtime
.venv/bin/bybit-longtime preflight
```

填好后告知“独立凭据已填写”。后续可以继续已授权的Demo验证和上线准备，不必把密钥粘贴到对话。
真实验证包括独立账户连通性、Bot身份/通知/按钮、策略合格的市价开仓、真实成交与TP/SL、自然退出核账，再启动常驻服务。

没有配置独立凭据前，真实账户下单和Bot消息不能验证，离线灰盒通过不代表这些外部步骤已经完成。
模型使用本机现有Codex认证和固定gpt-5.6-terra/medium，本版本不需要额外填写OpenAI API Key。
