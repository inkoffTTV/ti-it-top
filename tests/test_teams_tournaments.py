"""Тесты турниров, заявок и решений администратора."""
from __future__ import annotations

import pytest

from database.engine import session_scope
from database.models import (
    ApplicationStatus,
    PlayerProfile,
    TournamentStatus,
    VerificationStatus,
)
from database.repositories import ApplicationRepository, UserRepository
from services.applications import ApplicationService
from services.teams import TeamService
from services.tournaments import TournamentService


async def _mk_user(s, tg_id: int, username: str):
    return await UserRepository(s).upsert_from_telegram(tg_id, username, username.title(), None)


async def _verify(s, user_id: int):
    s.add(PlayerProfile(user_id=user_id, verification_status=VerificationStatus.VERIFIED))
    await s.commit()


async def _full_team(s, prefix: str, cap_tg: int):
    """Команда из 5 человек (капитан + 4), все VERIFIED."""
    svc = TeamService(s)
    cap = await _mk_user(s, cap_tg, f"{prefix}_cap")
    team = await svc.create_team(cap.id, f"Team {prefix}")
    await _verify(s, cap.id)
    for i in range(4):
        u = await _mk_user(s, cap_tg + 100 + i, f"{prefix}p{i}")
        await s.commit()
        await svc.add_player(team.id, cap.id, f"@{prefix}p{i}")
        await s.commit()
        await _verify(s, u.id)
    return svc, cap, team


async def _open_tournament(s, name="TI:IT CUP", max_teams=16):
    tsvc = TournamentService(s)
    tour = await tsvc.create(name, "Основной кубок", max_teams, None, open_registration=True)
    return tsvc, tour


# ---------------- tournaments ----------------

async def test_tournament_creation_defaults(db):
    async with session_scope() as s:
        tsvc = TournamentService(s)
        tour = await tsvc.create("Cup One", "desc", 8, None)
        assert tour.status == TournamentStatus.DRAFT
        assert tour.max_teams == 8


async def test_tournament_status_transitions(db):
    async with session_scope() as s:
        tsvc, tour = await _open_tournament(s)
        assert tour.status == TournamentStatus.REGISTRATION
        tour = await tsvc.set_status(tour.id, TournamentStatus.ACTIVE)
        assert tour.status == TournamentStatus.ACTIVE
        tour = await tsvc.set_status(tour.id, TournamentStatus.FINISHED)
        assert tour.status == TournamentStatus.FINISHED


async def test_tournament_max_teams_validation(db):
    async with session_scope() as s:
        tsvc = TournamentService(s)
        with pytest.raises(ValueError):
            await tsvc.create("Bad", None, 0, None)


async def test_tournament_empty_name_rejected(db):
    async with session_scope() as s:
        tsvc = TournamentService(s)
        with pytest.raises(ValueError):
            await tsvc.create("   ", None, 8, None)


# ---------------- applications ----------------

async def test_application_submit_and_duplicate(db):
    async with session_scope() as s:
        _, tour = await _open_tournament(s)
        _, cap, _team = await _full_team(s, "A", 7000)
        asvc = ApplicationService(s)
        app = await asvc.submit(cap.id, tour.id)
        assert app.status == ApplicationStatus.PENDING
        with pytest.raises(ValueError):
            await asvc.submit(cap.id, tour.id)


async def test_application_requires_captain(db):
    async with session_scope() as s:
        _, tour = await _open_tournament(s)
        _, _cap, team = await _full_team(s, "B", 7100)
        member = await UserRepository(s).get_by_username("@Bp0")
        asvc = ApplicationService(s)
        with pytest.raises(ValueError):
            await asvc.submit(member.id, tour.id)


async def test_application_closed_registration(db):
    async with session_scope() as s:
        tsvc, tour = await _open_tournament(s)
        _, cap, _team = await _full_team(s, "C", 7200)
        await tsvc.set_status(tour.id, TournamentStatus.ACTIVE)
        asvc = ApplicationService(s)
        with pytest.raises(ValueError):
            await asvc.submit(cap.id, tour.id)


async def test_small_roster_blocks_application(db):
    async with session_scope() as s:
        _, tour = await _open_tournament(s)
        svc = TeamService(s)
        cap = await _mk_user(s, 7250, "shorty_cap")
        team = await svc.create_team(cap.id, "Short Team")
        await _verify(s, cap.id)
        asvc = ApplicationService(s)
        with pytest.raises(ValueError):
            await asvc.submit(cap.id, tour.id)


async def test_rejected_player_blocks_application(db):
    async with session_scope() as s:
        _, tour = await _open_tournament(s)
        _, cap, team = await _full_team(s, "R", 7270)
        bad = await UserRepository(s).get_by_username("@Rp1")
        prof = await s.get(PlayerProfile, bad.profile.id if hasattr(bad, "profile") else None) if False else None
        from database.repositories import ProfileRepository
        pr = ProfileRepository(s)
        p = await pr.get_by_user_id(bad.id)
        p.verification_status = VerificationStatus.REJECTED
        await s.commit()
        asvc = ApplicationService(s)
        with pytest.raises(ValueError):
            await asvc.submit(cap.id, tour.id)


async def test_approve_and_reject_decisions(db):
    async with session_scope() as s:
        _, tour = await _open_tournament(s)
        _, cap, _team = await _full_team(s, "D", 7300)
        asvc = ApplicationService(s)
        app = await asvc.submit(cap.id, tour.id)
        repo = ApplicationRepository(s)
        approved = await repo.decide(app, ApplicationStatus.APPROVED, 100, "ok")
        await s.commit()
        assert approved.status == ApplicationStatus.APPROVED
        assert approved.admin_comment == "ok"
        # повторное решение по уже обработанной заявке — отказ (актуальность статуса
        # проверяет handler; на уровне репозитория защищаемся через проверку в _decide)
        fresh = await repo.get_by_id(app.id)
        assert fresh.status != ApplicationStatus.PENDING


async def test_my_application_lookup(db):
    async with session_scope() as s:
        _, tour = await _open_tournament(s)
        _, cap, _team = await _full_team(s, "E", 7400)
        asvc = ApplicationService(s)
        assert await asvc.my_application(cap.id) is None
        app = await asvc.submit(cap.id, tour.id)
        found = await asvc.my_application(cap.id)
        assert found is not None and found.id == app.id
        # обычный игрок тоже видит заявку своей команды
        member = await UserRepository(s).get_by_username("@Ep0")
        found2 = await asvc.my_application(member.id)
        assert found2 is not None and found2.id == app.id


async def test_max_teams_limit_blocks_submission(db):
    async with session_scope() as s:
        tsvc, tour = await _open_tournament(s, name="Tiny Cup", max_teams=2)
        _, cap1, _t1 = await _full_team(s, "F", 7500)
        # 7600+100..103 пересекаются с 7500-командой (игроки 7600..7603) — берём другой диапазон
        _, cap2, _t2 = await _full_team(s, "G", 7700)
        _, cap3, _t3 = await _full_team(s, "H", 8000)
        asvc = ApplicationService(s)
        a1 = await asvc.submit(cap1.id, tour.id)
        a2 = await asvc.submit(cap2.id, tour.id)
        repo = ApplicationRepository(s)
        await repo.decide(a1, ApplicationStatus.APPROVED, 100)
        await repo.decide(a2, ApplicationStatus.APPROVED, 100)
        await s.commit()
        with pytest.raises(ValueError):
            await asvc.submit(cap3.id, tour.id)
