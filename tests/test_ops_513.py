"""5.13: заморозка составов, снятие с турнира, расформирование, дубли, сводка, сторож."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from database.engine import session_scope
from database.models import (
    ApplicationStatus,
    PlayerProfile,
    RosterRequest,
    Team,
    TeamArchive,
    TeamMember,
    Tournament,
    TournamentApplication,
    TournamentMatch,
    TournamentStatus,
    User,
    VerificationStatus,
)
from services import digest, watchdog
from services.tournament_engine import service as engine
from tests.test_tournament_engine import _SETUP_SEQ, _setup

pytest.importorskip("httpx")

from tests.test_miniapp_v2 import ADMIN_TG, client, h  # noqa: E402,F401


def _tg(i: int, j: int = 0) -> int:
    """Telegram id игрока j команды i из последнего _setup."""
    return 10_000 + _SETUP_SEQ[0] * 100_000 + i * 10 + j


async def _status(tid: int, status: TournamentStatus) -> None:
    async with session_scope() as s:
        t = await s.get(Tournament, tid)
        t.status = status
        t.registration_open = status == TournamentStatus.REGISTRATION
        await s.commit()


async def _members(team_id: int) -> set[int]:
    async with session_scope() as s:
        return set((await s.execute(select(User.telegram_id).join(TeamMember, TeamMember.user_id == User.id)
                                     .where(TeamMember.team_id == team_id))).scalars().all())


async def _user_id(tg: int) -> int:
    async with session_scope() as s:
        return int(await s.scalar(select(User.id).where(User.telegram_id == tg)))


async def test_roster_frozen_after_registration_admin_decides(client):
    tid, teams = await _setup(2)
    await client.get("/api/v2/me", headers=h(_tg(0)))
    # Регистрация открыта — капитан исключает игрока сразу.
    uid4 = await _user_id(_tg(0, 4))
    r = await client.delete(f"/api/v2/team/members/{uid4}", headers=h(_tg(0)))
    assert r.status_code == 200 and not r.json().get("pending")
    assert _tg(0, 4) not in await _members(teams[0])
    await _status(tid, TournamentStatus.ACTIVE)
    team = (await client.get("/api/v2/team", headers=h(_tg(0)))).json()["team"]
    assert team["roster_frozen"]
    # После закрытия — только запрос организаторам.
    uid3 = await _user_id(_tg(0, 3))
    r = await client.delete(f"/api/v2/team/members/{uid3}", headers=h(_tg(0)))
    assert r.status_code == 200 and r.json()["pending"] and "заморожен" in r.json()["message"]
    assert _tg(0, 3) in await _members(teams[0])
    r = await client.post("/api/v2/team/leave", headers=h(_tg(0, 2)))
    assert r.json()["pending"] and _tg(0, 2) in await _members(teams[0])
    items = (await client.get("/api/v2/admin/roster-requests", headers=h(ADMIN_TG))).json()["items"]
    mine = [x for x in items if x["team"]["id"] == teams[0]]
    assert {x["kind"] for x in mine} == {"REMOVE", "LEAVE"} and all("MMR" in x["facts"] for x in mine)
    remove = next(x for x in mine if x["kind"] == "REMOVE")
    leave = next(x for x in mine if x["kind"] == "LEAVE")
    assert (await client.post(f"/api/v2/admin/roster-requests/{remove['id']}", headers=h(ADMIN_TG), json={"approve": True})).status_code == 200
    assert (await client.post(f"/api/v2/admin/roster-requests/{leave['id']}", headers=h(ADMIN_TG), json={"approve": False})).status_code == 200
    members = await _members(teams[0])
    assert _tg(0, 3) not in members and _tg(0, 2) in members
    r = await client.post(f"/api/v2/admin/roster-requests/{remove['id']}", headers=h(ADMIN_TG), json={"approve": True})
    assert r.status_code == 400  # повторно не решается

    # Вход по приглашению — тоже через организаторов, и с фактами об игроке.
    async with session_scope() as s:
        newbie = User(telegram_id=_tg(9, 9) + 7, username="newbie513", is_registered=True)
        s.add(newbie)
        await s.flush()
        s.add(PlayerProfile(user_id=newbie.id, verification_status=VerificationStatus.VERIFIED, exact_mmr=7000,
                            approved_mmr=7000, mmr_verified=True, steam_id64="76561190000000001"))
        await s.commit()
        newbie_id, newbie_tg = int(newbie.id), int(newbie.telegram_id)
    inv = await client.post("/api/v2/team/invites", headers=h(_tg(0)), json={"user_id": newbie_id})
    assert inv.status_code == 200, inv.text
    r = await client.post(f"/api/v2/invites/{inv.json()['invite_id']}/accept", headers=h(newbie_tg))
    assert r.status_code == 200 and r.json()["pending"], r.text
    assert newbie_tg not in await _members(teams[0])
    async with session_scope() as s:
        req = await s.scalar(select(RosterRequest).where(RosterRequest.target_user_id == newbie_id, RosterRequest.status == "PENDING"))
    item = next(x for x in (await client.get("/api/v2/admin/roster-requests", headers=h(ADMIN_TG))).json()["items"] if x["id"] == req.id)
    assert item["kind"] == "ADD" and "7000" in item["facts"]
    assert (await client.post(f"/api/v2/admin/roster-requests/{req.id}", headers=h(ADMIN_TG), json={"approve": True})).status_code == 200
    assert newbie_tg in await _members(teams[0])
    # После завершения турнира состав снова свободный.
    await _status(tid, TournamentStatus.FINISHED)
    r = await client.post("/api/v2/team/leave", headers=h(newbie_tg))
    assert r.status_code == 200 and not r.json().get("pending")


async def test_withdraw_before_start_and_after_start(client):
    tid, teams = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "double_elim", "bo_default": 1, "bo_final": 1, "seeding": "standard"})).status_code == 200
    # До старта (регистрация открыта): команда просто уходит из сетки.
    assert (await client.post(f"/api/v2/tournaments/{tid}/withdraw", headers=h(_tg(3)), json={})).status_code == 400
    assert (await client.post(f"/api/v2/tournaments/{tid}/withdraw", headers=h(_tg(3, 1)), json={"confirm": True})).status_code == 403
    r = await client.post(f"/api/v2/tournaments/{tid}/withdraw", headers=h(_tg(3)), json={"confirm": True})
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        app = await s.scalar(select(TournamentApplication).where(TournamentApplication.team_id == teams[3]))
        assert app.status == ApplicationStatus.CANCELLED
        assert not (await s.execute(select(TournamentMatch.id).where(
            TournamentMatch.tournament_id == tid, (TournamentMatch.team1_id == teams[3]) | (TournamentMatch.team2_id == teams[3])))).all()
    assert (await client.post(f"/api/v2/tournaments/{tid}/withdraw", headers=h(_tg(3)), json={"confirm": True})).status_code == 400

    # Игры идут: снявшаяся команда проигрывает технически сейчас и в нижней сетке, когда там появится соперник.
    tid, teams = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "double_elim", "bo_default": 1, "bo_final": 1, "seeding": "standard"})).status_code == 200
    await _status(tid, TournamentStatus.ACTIVE)
    async with session_scope() as s:
        r1 = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid, TournamentMatch.round_no == 1,
                                                            TournamentMatch.bracket == "upper").order_by(TournamentMatch.position))).scalars().all()
    first, second = r1
    assert (await client.post(f"/api/v2/admin/matches/{first.id}/result", headers=h(ADMIN_TG), json={"score1": 1, "score2": 0})).status_code == 200
    quitter = second.team1_id
    idx = teams.index(quitter)
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/teams/{quitter}/withdraw", headers=h(ADMIN_TG), json={"confirm": True, "reason": "не пришли"})
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        m2 = await s.get(TournamentMatch, second.id)
        assert m2.status == "FINISHED" and m2.result_type == "forfeit" and m2.winner_team_id == second.team2_id
        lower = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid, TournamentMatch.bracket == "lower"))).scalars().all()
        lower_first = next(x for x in lower if quitter in (x.team1_id, x.team2_id))
        # Соперник в нижней сетке уже известен (проигравший первого матча) — техническое поражение сразу.
        assert lower_first.status == "FINISHED" and lower_first.winner_team_id != quitter
    assert idx >= 0


async def test_admin_disband_keeps_history_names(client):
    tid, teams = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "single_elim", "bo_default": 1, "bo_final": 1, "seeding": "standard"})).status_code == 200
    await _status(tid, TournamentStatus.ACTIVE)
    async with session_scope() as s:
        m = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid, TournamentMatch.round_no == 1)
                             .order_by(TournamentMatch.position))).scalars().first()
        victim = int(m.team1_id)
        name = (await s.get(Team, victim)).name
    assert (await client.post(f"/api/v2/admin/teams/{victim}/disband", headers=h(ADMIN_TG), json={})).status_code == 400
    r = await client.post(f"/api/v2/admin/teams/{victim}/disband", headers=h(ADMIN_TG), json={"confirm": True, "reason": "смурф"})
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        assert await s.get(Team, victim) is None
        assert (await s.scalar(select(TeamArchive).where(TeamArchive.team_id == victim))).name == name
        assert not (await s.execute(select(TeamMember.id).where(TeamMember.team_id == victim))).all()
        # Игр ещё не было — команда просто ушла из сетки, сетка пересобрана на троих.
        rows = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid))).scalars().all()
        placed = {t for x in rows for t in (x.team1_id, x.team2_id) if t}
        assert victim not in placed and placed == set(teams) - {victim}
        assert (await engine._team_names(s, {victim}))[victim] == f"{name} (расформирована)"
    # Бывший капитан снова свободен и может создать команду.
    assert (await client.post("/api/v2/team", headers=h(_tg(teams.index(victim))), json={"name": "Reborn 513"})).status_code in (200, 201)


async def test_duplicates_digest_and_watchdog(client, tmp_path):
    tid, teams = await _setup(2)
    async with session_scope() as s:
        p1 = await s.scalar(select(PlayerProfile).join(User, User.id == PlayerProfile.user_id).where(User.telegram_id == _tg(0, 1)))
        p2 = await s.scalar(select(PlayerProfile).join(User, User.id == PlayerProfile.user_id).where(User.telegram_id == _tg(1, 1)))
        p1.steam_id64 = p2.steam_id64 = "76561199999999999"
        t = await s.get(Tournament, tid)
        t.start_date = start = datetime.now(timezone.utc) + timedelta(hours=3)
        await s.commit()
    dup = (await client.get("/api/v2/admin/duplicates", headers=h(ADMIN_TG))).json()
    group = next(g for g in dup["steam"] if g["steam_id64"] == "76561199999999999")
    assert len(group["users"]) == 2 and all(u["team"] for u in group["users"])
    text = (await client.get("/api/v2/admin/digest", headers=h(ADMIN_TG))).json()["html"]
    assert "Engine Cup" in text and "Регистрация открыта" in text and "Дубли аккаунтов" in text
    # Сводка — раз в день после 9:00 МСК, повторный запуск в тот же день её не шлёт.
    state = tmp_path / "digest_last"
    # 08:00 МСК в день старта турнира (старт «через 3 часа» вечером — это уже завтра)
    day = digest.timeutil.to_local(start)
    morning = datetime(day.year, day.month, day.day, 5, 0, tzinfo=timezone.utc)
    assert await digest.digest_due(state, morning) is None
    assert await digest.digest_due(state, morning + timedelta(hours=1, minutes=5)) is not None
    assert await digest.digest_due(state, morning + timedelta(hours=3)) is None
    # Сторож: первый запуск, штатная и аварийная остановки.
    sd = tmp_path / "state"
    assert "первый запуск" in watchdog.startup_text(sd, "5.13.0")
    assert "аварийная" in watchdog.startup_text(sd, "5.13.0")
    watchdog.mark_clean_shutdown(sd)
    assert "штатная" in watchdog.startup_text(sd, "5.13.0")
    assert watchdog.disk_alert(tmp_path, min_free_gb=10 ** 6) is not None and watchdog.disk_alert(tmp_path, min_free_gb=0, min_free_pct=0) is None
    db = tmp_path / "x.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 4096)
    packed = watchdog.pack_backup(db, tmp_path / "out")
    assert packed.exists() and packed.stat().st_size < db.stat().st_size
    assert watchdog.backup_send_due(sd)
    watchdog.mark_backup_sent(sd)
    assert not watchdog.backup_send_due(sd)
    counter = watchdog.ErrorCounter()
    import logging

    log = logging.getLogger("t513")
    log.addHandler(counter)
    log.error("boom")
    log.error("Failed to fetch updates - TelegramNetworkError")
    assert counter.take()[0] == 1
    log.removeHandler(counter)
