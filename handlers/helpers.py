"""Хелперы для handlers: сессии, уведомления админам, парсинг callback_data."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from aiogram import Bot
from aiogram.types import Message

from config import Config
from database.engine import session_scope
from keyboards.main import main_keyboard
from utils.formatting import escape

logger = logging.getLogger("START")


@asynccontextmanager
async def db_session() -> AsyncIterator:
    """Сессия БД с гарантированным rollback при необработанном исключении."""
    async with session_scope() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def parse_cb(callback_data: str) -> tuple[str, ...]:
    return tuple(callback_data.split(":"))


async def notify_admins(bot: Bot, config: Config, text: str, reply_markup=None) -> None:
    """Рассылка карточки всем администраторам. Ошибка доставки одному — не фатальна."""
    sent = 0
    for admin_id in sorted(config.admin_ids):
        try:
            await bot.send_message(
                admin_id, text, reply_markup=reply_markup, parse_mode="HTML",
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001 — админ мог заблокировать бота
            logger.warning("[ADMIN] Notify failed for %s: %s", admin_id, e)
    if sent == 0:
        logger.error("[ADMIN] No admins received the notification (ADMIN_IDS configured?)")


async def send_main_menu(message: Message, text: str, config: Config | None = None) -> None:
    cfg = config or _config()
    from config import is_admin as _is_admin

    admin = _is_admin(message.from_user.id if message.from_user else None, cfg)
    keyboard = main_keyboard(admin)
    try:  # 5.9: зрителю — меню зрителя
        from sqlalchemy import select as _select

        from database.engine import session_scope as _scope
        from database.models import User as _User
        from keyboards.main import viewer_keyboard
        from services.audience import VIEWER, kind_of

        if message.from_user:
            async with _scope() as s:
                uid = await s.scalar(_select(_User.id).where(_User.telegram_id == message.from_user.id))
                if uid and (await kind_of(s, uid))[0] == VIEWER:
                    keyboard = viewer_keyboard(admin)
    except Exception:  # noqa: BLE001
        pass
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


def _config() -> Config:
    from handlers.common import get_config

    return get_config()


def safe_html(text: str | None) -> str:
    return escape(text) if text else ""
