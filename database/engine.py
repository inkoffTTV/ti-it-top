"""Движок БД и сессии (SQLAlchemy 2.x async)."""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from database.models import Base

logger = logging.getLogger("DATABASE")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_engine(database_url: str) -> async_sessionmaker[AsyncSession]:
    """Создаёт движок и фабрику сессий. Не удаляет существующую БД."""
    global _engine, _session_factory
    if _engine is not None:
        return _session_factory  # type: ignore[return-value]
    engine_kwargs: dict = {"echo": False, "future": True}
    if database_url.startswith("sqlite"):
        # Бот и Mini App пишут в один SQLite-файл из одного процесса.
        # Стандартные 5 с ожидания блокировки иногда дают «database is locked»
        # при параллельных записях; 15 с — запас без изменения схемы/режима журнала.
        engine_kwargs["connect_args"] = {"timeout": 15}
    _engine = create_async_engine(database_url, **engine_kwargs)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    logger.info("[DATABASE] Engine created for %s", _safe_url(database_url))
    return _session_factory


def _safe_url(url: str) -> str:
    """Убирает потенциальные секреты из URL для логов."""
    if "@" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.split("@", 1)
        return f"{scheme}://***@{host}"
    return url


async def create_tables() -> None:
    """Безопасная инициализация схемы: создаёт таблицы, если их нет.

    Никаких DROP TABLE / удаления файла БД.
    """
    assert _engine is not None, "Engine is not initialized"
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Лёгкие инкрементальные миграции для SQLite (добавление колонок).
        await conn.run_sync(_ensure_missing_columns)
        await conn.execute(text("UPDATE player_profiles SET approved_mmr = exact_mmr WHERE approved_mmr IS NULL AND mmr_verified = 1 AND exact_mmr IS NOT NULL"))
    await _seed_rank_ranges()
    await _repair_profile_ranks()
    logger.info("[DATABASE] Schema ensured (create_all + column migrations, no drops)")


# Колонки, добавляемые поверх create_all (для существующих БД без этих полей).
_MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "player_profiles": [
        ("hours_total", "INTEGER"),
        ("medal_rank", "INTEGER"),
        ("leaderboard_rank", "INTEGER"),
        ("rank_name", "TEXT"),
        ("exact_mmr", "INTEGER"),
        ("approved_mmr", "INTEGER"),
        ("mmr_verified", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("mmr_verified_by", "INTEGER"),
        ("mmr_verified_at", "TIMESTAMP WITH TIME ZONE"),
        ("preferred_positions", "TEXT"),
    ],
    "teams": [
        ("avatar_file_id", "TEXT"),
        ("avatar_file_type", "TEXT NOT NULL DEFAULT 'photo'"),
        ("role_mode", "TEXT NOT NULL DEFAULT 'PLAYER_CHOICE'"),
        ("description", "TEXT"),
    ],
    "team_members": [
        ("position", "INTEGER"),
    ],
}


def _ensure_missing_columns(sync_conn) -> None:
    from sqlalchemy import inspect, text

    try:
        inspector = inspect(sync_conn)
        tables = set(inspector.get_table_names())
    except Exception as e:  # noqa: BLE001 — диагностика недоступна, пропускаем миграцию
        logger.warning("[DATABASE] Column migration skipped (inspect failed): %s", e)
        return
    for table, columns in _MIGRATION_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, sqltype in columns:
            if name in existing:
                continue
            try:
                sync_conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")
                )
                logger.info("[DATABASE] Migrated: %s.%s added", table, name)
            except Exception as e:  # noqa: BLE001 — не роняем запуск из-за миграции
                logger.error("[DATABASE] Migration failed %s.%s: %s", table, name, e)

    # Один пользователь не может физически состоять более чем в одной команде.
    # На старой БД индекс добавится без удаления существующих данных.
    try:
        sync_conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_team_member_user "
                "ON team_members (user_id)"
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[DATABASE] Unique team membership index not created: %s", e)


async def _seed_rank_ranges() -> None:
    from sqlalchemy import select
    from database.models import RankRange
    from utils.ranks import rank_range_rows

    async with _session_factory() as session:
        existing = set((await session.execute(select(RankRange.rank_tier))).scalars().all())
        for tier, name, star, lo, hi in rank_range_rows():
            if tier not in existing:
                session.add(RankRange(
                    rank_tier=tier, rank_name=name, star=star,
                    mmr_min=lo, mmr_max=hi,
                ))
            else:
                row = await session.scalar(select(RankRange).where(RankRange.rank_tier == tier))
                if row is not None:
                    row.rank_name = name
                    row.star = star
                    row.mmr_min = lo
                    row.mmr_max = hi
        await session.commit()


async def _repair_profile_ranks() -> None:
    """Нормализует старые названия рангов в уже существующей БД.

    Actual rank_tier всегда важнее старого текстового rank_name.
    Если tier отсутствует, старую строку преобразуем только когда она
    однозначно распознаётся; MMR сам по себе здесь НЕ назначает медаль.
    """
    from sqlalchemy import select
    from database.models import PlayerProfile
    from utils.ranks import rank_label_from_tier, rank_name_to_tier

    async with _session_factory() as session:
        profiles = (await session.execute(select(PlayerProfile))).scalars().all()
        changed = 0
        for profile in profiles:
            label = None
            if profile.medal_rank is not None:
                label = rank_label_from_tier(profile.medal_rank)
            elif profile.rank_name:
                tier = rank_name_to_tier(profile.rank_name)
                if tier is not None:
                    profile.medal_rank = tier
                    label = rank_label_from_tier(tier)
            if label and profile.rank_name != label:
                profile.rank_name = label
                changed += 1
        if changed:
            await session.commit()
            logger.info("[DATABASE] Normalized %s player rank labels", changed)


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("[DATABASE] Engine disposed")


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database engine is not initialized. Call init_engine() first.")
    return _session_factory


def session_scope() -> AsyncSession:
    """Возвращает новую сессию (использовать как async context manager)."""
    return get_session_factory()()
