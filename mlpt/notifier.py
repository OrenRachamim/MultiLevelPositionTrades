"""Telegram notifications. Sending never blocks or breaks trading."""

from __future__ import annotations

import asyncio
import logging
from collections import deque

log = logging.getLogger("mlpt.notify")


class Notifier:
    """Base notifier: remembers the last messages (also used by the tests)."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.sent: deque[str] = deque(maxlen=200)

    def notify(self, text: str) -> None:
        self.sent.append(self.prefix + text)


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, prefix: str = "") -> None:
        super().__init__(prefix)
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)

    def notify(self, text: str) -> None:
        super().notify(text)
        try:
            self.queue.put_nowait(self.prefix + text)
        except asyncio.QueueFull:
            log.warning("telegram queue is full, message dropped")

    async def run(self) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                text = await self.queue.get()
                for attempt in range(4):
                    try:
                        r = await client.post(self.url, json={"chat_id": self.chat_id, "text": text})
                        if r.status_code == 200:
                            break
                        if r.status_code == 429:
                            await asyncio.sleep(float(r.json().get("parameters", {}).get("retry_after", 5)))
                            continue
                        # never log the URL: it contains the bot token
                        log.error("telegram rejected the message: HTTP %s %s", r.status_code, r.text[:200])
                        break
                    except Exception as e:  # network trouble: retry with back-off
                        log.warning("telegram send failed (%s), attempt %d", type(e).__name__, attempt + 1)
                        await asyncio.sleep(2 ** attempt * 2)

    async def test(self) -> tuple[bool, str]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(self.url, json={"chat_id": self.chat_id,
                                                      "text": self.prefix + "בדיקת חיבור - המערכת מחוברת ✅"})
            return (True, "נשלח") if r.status_code == 200 else (False, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            return False, type(e).__name__
