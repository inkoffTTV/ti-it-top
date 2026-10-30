"""Тесты БД: пользователи, дубликаты, целостность."""
from __future__ import annotations

import pytest

from database.models import TeamRole, VerificationStatus
from database.repositories import ProfileRepository, TeamRepository, UserRepository


async def test_user_creation(db):
    from database.engine import session_scope
    async with session_scope() as s:
        repo = UserRepository(s)
        u = await repo.upsert_from_telegram(111, "alice", "Alice", None)
        await s.commit()
        assert u.id is not None
        assert u.telegram_id == 111
        assert u.username == "alice"


async def test_duplicate_user_not_created(db):
    from database.engine import session_scope
    async with session_scope() as s:
        repo = UserRepository(s)
        u1 = await repo.upsert_from_telegram(222, "bob", "Bob", None)
        await s.commit()
        u2 = await repo.upsert_from_telegram(222, "bobby", "Bobby", "X")
        await s.commit()
        assert u1.id == u2.id
        assert u2.username == "bobby"  # username обновляется при смене
        assert await repo.count() == 1


async def test_profile_get_or_create_no_duplicates(db):
    from database.engine import session_scope
    async with session_scope() as s:
        users = UserRepository(s)
        u = await users.upsert_from_telegram(333, "cara", "Cara", None)
        profiles = ProfileRepository(s)
        p1 = await profiles.get_or_create(u.id)
        p2 = await profiles.get_or_create(u.id)
        assert p1.id == p2.id
        assert p1.verification_status == VerificationStatus.UNVERIFIED


async def test_team_member_unique_constraint(db):
    from sqlalchemy.exc import IntegrityError
    from database.engine import session_scope
    async with session_scope() as s:
        users = UserRepository(s)
        cap = await users.upsert_from_telegram(400, "cap", "Cap", None)
        pl = await users.upsert_from_telegram(401, "pl", "Pl", None)
        teams = TeamRepository(s)
        t = await teams.create("Alpha", cap.id)
        await teams.add_member(t.id, pl.id, TeamRole.PLAYER)
        await s.commit()
        # дубликат не создаётся: репозиторий защищает от него сам
        with pytest.raises(ValueError):
            await teams.add_member(t.id, pl.id, TeamRole.PLAYER)
        roster = await teams.get_roster(t.id)
        # капитан + игрок; дубликат игрока не добавлен
        assert len(roster) == 2
        assert sum(1 for m in roster if m.user_id == pl.id) == 1


async def test_foreign_key_missing_user_rejected(db):
    from database.engine import session_scope
    async with session_scope() as s:
        teams = TeamRepository(s)
        t = await teams.create("Beta", (await UserRepository(s).upsert_from_telegram(402, "c2", "C", None)).id)
        await s.commit()
        with pytest.raises(ValueError):
            await teams.add_member(t.id, 999999)  # несуществующий user
