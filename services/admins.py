"""Постоянное управление администраторами."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from database.models import AdminUser


async def bootstrap_admins(session: AsyncSession, config: Config) -> set[int]:
    """Синхронизирует ADMIN_IDS как bootstrap и загружает сохранённых админов."""
    for telegram_id in sorted(config.admin_ids):
        row = await session.scalar(select(AdminUser).where(AdminUser.telegram_id == telegram_id))
        if row is None:
            session.add(AdminUser(telegram_id=telegram_id, active=True))
        elif not row.active:
            row.active = True
    await session.flush()
    rows = (await session.execute(select(AdminUser).where(AdminUser.active.is_(True)))).scalars().all()
    ids = {int(r.telegram_id) for r in rows}
    config.admin_ids.clear()
    config.admin_ids.update(ids)
    await session.commit()
    return ids
