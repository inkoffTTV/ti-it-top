"""Блокировка доступа к боту и Mini App (5.7).

Заблокированный пользователь не может пользоваться ботом (любые сообщения и кнопки)
и Mini App (новой и /legacy/). Администраторов заблокировать нельзя.
Список telegram_id кешируется на 30 секунд и сбрасывается при каждой блокировке/разблокировке
(бот и Mini App работают в одном процессе, поэтому изменения видны сразу).
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import session_scope
from database.models import User, UserBan

logger = logging.getLogger("BANS")

_CACHE: dict = {"ids": set(), "at": 0.0}
TTL = 30.0
BLOCKED_TEXT = "Доступ к TI:IT TOP заблокирован организатором. Если это ошибка — напишите организаторам лично."


def invalidate() -> None:
    _CACHE["at"] = 0.0


async def banned_telegram_ids() -> set[int]:
    if time.monotonic() - _CACHE["at"] < TTL:
        return _CACHE["ids"]
    try:
        async with session_scope() as session:
            ids = set((await session.execute(select(UserBan.telegram_id))).scalars().all())
    except Exception:  # noqa: BLE001 — таблицы ещё нет / БД недоступна: никого не блокируем
        logger.exception("[BANS] load failed")
        return _CACHE["ids"]
    _CACHE.update({"ids": {int(x) for x in ids}, "at": time.monotonic()})
    return _CACHE["ids"]


async def is_banned(telegram_id: int | None) -> bool:
    return bool(telegram_id) and int(telegram_id) in await banned_telegram_ids()


async def ban(session: AsyncSession, user: User, *, reason: str | None, admin_telegram_id: int | None) -> UserBan:
    row = await session.scalar(select(UserBan).where(UserBan.user_id == user.id))
    if row is None:
        row = UserBan(user_id=int(user.id), telegram_id=int(user.telegram_id))
        session.add(row)
    row.reason = (reason or "").strip()[:300] or None
    row.banned_by = admin_telegram_id
    await session.flush()
    invalidate()
    return row


async def unban(session: AsyncSession, user_id: int) -> bool:
    res = await session.execute(delete(UserBan).where(UserBan.user_id == int(user_id)))
    invalidate()
    return bool(res.rowcount)
