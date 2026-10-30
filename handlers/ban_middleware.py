"""Внешний middleware: заблокированный пользователь не может пользоваться ботом (5.7)."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import GLOBAL_CONFIG, is_admin
from services.bans import BLOCKED_TEXT, is_banned

_last_notice: dict[int, float] = {}
NOTICE_EVERY = 600.0  # напоминаем о блокировке не чаще раза в 10 минут


class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or is_admin(user.id, GLOBAL_CONFIG) or not await is_banned(user.id):
            return await handler(event, data)
        now = time.monotonic()
        if isinstance(event, CallbackQuery):
            try:
                await event.answer(BLOCKED_TEXT, show_alert=True)
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(event, Message) and now - _last_notice.get(user.id, 0.0) > NOTICE_EVERY:
            _last_notice[user.id] = now
            try:
                await event.answer(BLOCKED_TEXT)
            except Exception:  # noqa: BLE001
                pass
        return None
