# Repository instructions for Codex

## Deployment handoff

When this repository is opened on a VPS, read `VPS_CODEX_HANDOFF.md` first and follow it as the deployment and acceptance contract. The default target is Debian 13 with systemd and the repository must remain on the verified `main` revision during deployment.

## Current authority

- Current model/config contract: `gpt-5.6-terra / medium`, `ultrashort-v5`, `signal-analysis-v9`.
- Current documentation authority starts at `docs/CURRENT_DOCUMENTS.md`.
- Files dated 2026-08-18 under `docs/superpowers/` are historical evolution records when they conflict with current code or 2026-08-19 documentation.
- Reference repositories and local CMI/prompt projects are design references only; they are not runtime dependencies and must not be copied into this repository.

## Hard boundaries

- This is a public-market direction signal and Telegram notification service. Do not add account, balance, position, leverage, order, PnL, private exchange API, or automatic trading behavior.
- Approximate take-profit is display-only. Do not use it for direction, ranking, monitoring, invalidation, or lifecycle decisions.
- Threshold crossing requests a fresh model review; it does not automatically invalidate direction.
- A model monitoring decision of `REJECTED` is a normal zero-rule outcome, not a failure and not a reason to tune a threshold mechanically.
- Scheduled analysis must produce exactly two primary signals from five analyzed candidates. Do not send WATCH-only notifications.
- Never commit `.env`, `config/system.local.yaml`, `runtime/`, Telegram credentials, OpenAI keys, or Codex authentication files.

## Change and validation rules

- Prefer environment/configuration fixes on a VPS. Do not rewrite strategy, prompts, or signal semantics to work around a deployment problem.
- If code must change, explain the precise Debian failure first, make the smallest scoped fix, and run Ruff, strict mypy, the full pytest suite, compileall, configuration checks, and the realistic scenarios in `VPS_CODEX_HANDOFF.md`.
- A deployment is not complete merely because systemd is active. Observe a startup cycle, the next natural `:00/:30` cycle, exactly two Telegram primary signals, SQLite delivery records, and threshold activation or a valid zero-rule outcome.
- Report every failure with the exact workflow step, direct cause, fix, and rerun evidence. Ask the user only for missing credentials, account/model access, or an external decision that cannot be discovered safely.
