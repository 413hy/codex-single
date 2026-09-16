"""Read IDs from a /start sent to the new bot; never prints the Bot Token."""

import asyncio
import json

from longtime.config import Settings
from longtime.service import ProcessLock
from longtime.telegram import Telegram


async def main():
    settings = Settings()
    if not settings.telegram_token.get_secret_value():
        raise SystemExit("请先在 .env 填写 TELEGRAM_TOKEN，再向新Bot私聊发送 /start")
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    lock = ProcessLock(settings.runtime_dir / "service.lock")
    bot = Telegram(settings, None)
    try:
        await bot.preflight()
        updates = await bot.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        pairs = set()
        for update in updates:
            msg = update.get("message", {})
            if msg.get("chat", {}).get("type") == "private" and msg.get("text", "").startswith(
                "/start"
            ):
                pairs.add((msg["chat"]["id"], msg["from"]["id"]))
        if not pairs:
            print("尚未找到 /start：请打开新Bot，私聊发送 /start 后重试。")
        else:
            for chat, user in sorted(pairs):
                print(
                    json.dumps(
                        {"TELEGRAM_CHAT_ID": chat, "TELEGRAM_USER_ID": user}, ensure_ascii=False
                    )
                )
            if len(pairs) > 1:
                print("发现多个用户，请只填写你自己的那组ID。")
    finally:
        await bot.close()
        lock.close()


if __name__ == "__main__":
    asyncio.run(main())
