# Bybit Multi-Source Signal

面向 Bybit USDT 线性永续的多数据源交易机会分析与 Telegram 通知服务。

当前 V1 只读取公开市场数据并发送分析通知，不读取账户，不管理仓位，也不具备交易能力。

完整需求和架构：

- `docs/requirements/2026-08-17-bybit-multi-source-signal-system-dev-requirements.md`
- `docs/superpowers/specs/2026-08-17-bybit-multi-source-signal-system-design.md`
- `docs/superpowers/plans/2026-08-17-bybit-multi-source-signal-system-implementation.md`

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -c requirements-dev.lock.txt -e ".[dev]"
.\.venv\Scripts\python -m pytest
```

运行配置校验：

```powershell
Copy-Item config\system.example.yaml config\system.local.yaml
.\.venv\Scripts\python -m bybit_signal config-check --config config\system.local.yaml
```

Telegram Token 必须通过环境变量 `BYBIT_SIGNAL_TELEGRAM__TOKEN` 注入，不得写入配置文件。
