"""События, уведомления и общий Mini App чат."""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ActivityEvent, Notification


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def add_event(
    session: AsyncSession,
    *,
    kind: str,
    title: str,
    body: str | None = None,
    actor_user_id: int | None = None,
    target_user_id: int | None = None,
) -> ActivityEvent:
    event = ActivityEvent(
        kind=kind[:64], title=title[:160], body=body[:1000] if body else None,
        actor_user_id=actor_user_id, target_user_id=target_user_id,
    )
    session.add(event)
    await session.flush()
    if target_user_id is not None:
        session.add(Notification(
            user_id=target_user_id, kind=kind[:64], title=title[:160],
            body=body[:1000] if body else None, event_id=event.id,
        ))
    return event


async def unread_count(session: AsyncSession, user_id: int) -> int:
    from sqlalchemy import func
    return int((await session.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user_id, Notification.is_read.is_(False)
        )
    )) or 0)
