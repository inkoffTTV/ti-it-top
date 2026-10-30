"""ОПЦИОНАЛЬНАЯ интеграция Google Sheets.

Без GOOGLE_SHEETS_CREDENTIALS / GOOGLE_SHEETS_ID модуль полностью отключён:
is_enabled == False, все методы — no-op с логом. Ошибки sheets НИКОГДА
не ломают регистрацию или другие flows (все вызовы обёрнуты в try/except
на уровне вызывающего кода и внутри самого модуля).

Реальная запись выполняется через публичный webhook-эндпоинт Google Apps
Script (GOOGLE_SHEETS_ID — URL веб-приложения), чтобы не тянуть тяжёлые
зависимости google-auth/gspread. Если webhook не настроен — тихо пропускаем.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger("SHEETS")


class SheetsService:
    def __init__(self, webhook_url: str = "", timeout: float = 5.0):
        self.webhook_url = (webhook_url or "").strip()
        self.timeout = timeout

    @property
    def is_enabled(self) -> bool:
        return self.webhook_url.startswith("https://")

    async def append_row(self, sheet: str, row: dict) -> bool:
        """Отправляет строку в лист. Возвращает True при успехе; ошибки глотает осознанно."""
        if not self.is_enabled:
            logger.debug("[SHEETS] Integration disabled (no webhook), skipping '%s'", sheet)
            return False
        payload = {"sheet": sheet, "row": row}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as session:
                async with session.post(self.webhook_url, json=payload) as resp:
                    if resp.status >= 400:
                        logger.warning("[SHEETS] Webhook HTTP %s for sheet '%s'", resp.status, sheet)
                        return False
                    return True
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("[SHEETS] Webhook unavailable (%s), skipping sheet write", e)
            return False

    async def log_registration(self, record: dict) -> bool:
        return await self.append_row("registrations", record)

    async def log_application(self, record: dict) -> bool:
        return await self.append_row("applications", record)
