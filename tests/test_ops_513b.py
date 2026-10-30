"""5.13 (часть 2): результат по OpenDota, прогнозы, болельщики, картинка итогов, журнал, перепроверка составов."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from database.engine import session_scope
from database.models import (
    MatchReport,
    PlayerProfile,
    TeamFollow,
    TeamMember,
    Tournament,
    TournamentMatch,
    TournamentMatchGame,
    TournamentStatus,
    User,
)
from services import opendota_match as od
from services import tournament_extras as extras
from services.tournament_engine import reminders, timeutil
from tests.test_ops_513 import _tg
from tests.test_tournament_engine import _setup

pytest.importorskip("httpx")

from tests.test_miniapp_v2 import ADMIN_TG, client, h  # noqa: E402,F401

BASE = od.STEAM64_BASE


async def _bracket(client, n=4, fmt="single_elim", bo=3):
    tid, teams = await _setup(n)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": fmt, "bo_default": bo, "bo_final": bo, "seeding": "standard"})).status_code == 200
    async with session_scope() as s:
        m = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid, TournamentMatch.round_no == 1)
                             .order_by(TournamentMatch.position))).scalars().first()
    return tid, teams, m


async def _steam(team_id: int, start: int) -> list[int]:
    """Привязывает Steam игрокам команды; возвращает account_id."""
    out = []
    async with session_scope() as s:
        profiles = (await s.execute(select(PlayerProfile).join(TeamMember, TeamMember.user_id == PlayerProfile.user_id)
                                    .where(TeamMember.team_id == team_id))).scalars().all()
        for i, p in enumerate(profiles):
            p.steam_id64 = str(BASE + start + i)
            out.append(start + i)
        await s.commit()
    return out


def _game(radiant: list[int], dire: list[int], radiant_win: bool, start_time: int | None = None) -> dict:
    players = [{"account_id": a, "isRadiant": True, "hero_id": 1 + i} for i, a in enumerate(radiant)]
    players += [{"account_id": a, "isRadiant": False, "hero_id": 20 + i} for i, a in enumerate(dire)]
    return {"players": players, "radiant_win": radiant_win, "duration": 2405, "start_time": start_time}


def test_parse_ids():
    assert od.parse_ids("8012345678, https://www.dotabuff.com/matches/8012345679 8012345678") == [8012345678, 8012345679]
    assert od.parse_ids("abc") == []
    assert od.account_id(str(BASE + 5)) == 5


async def test_opendota_report_auto_confirmed_and_games_saved(client):
    tid, teams, m = await _bracket(client)
    a1 = await _steam(m.team1_id, 1000)
    a2 = await _steam(m.team2_id, 2000)
    cap1 = _tg(teams.index(m.team1_id))
    # BO3: команда 1 выиграла за Radiant, потом проиграла за Dire, потом снова выиграла.
    data = [_game(a1, a2, True), _game(a2, a1, True), _game(a1, a2, True)]
    out = await extras.create_report(m.id, await _uid(cap1), None, None, None, [111111111, 222222222, 333333333],
                                     _dota_data=(data, {1: "Anti-Mage", 20: "Pudge"}))
    assert out["verified"] and out["dota"]["score1"] == 2 and out["dota"]["score2"] == 1
    assert out["dota"]["games"][0]["heroes1"][0] == "Anti-Mage" and out["dota"]["games"][0]["duration"] == "40:05"
    async with session_scope() as s:
        rep = await s.get(MatchReport, out["id"])
        assert rep.status == "CONFIRMED" and rep.score1 == 2  # без подтверждения соперника
    detail = (await client.get(f"/api/v2/matches/{m.id}", headers=h(cap1))).json()
    assert detail["report"]["dota"]["verified"] and len(detail["report"]["dota"]["games"]) == 3
    admin_items = (await client.get("/api/v2/admin/match-reports", headers=h(ADMIN_TG))).json()["items"]
    assert any(x["id"] == out["id"] and x["dota"] for x in admin_items)
    async with session_scope() as s:
        admin_id = int(await s.scalar(select(User.id).where(User.telegram_id == ADMIN_TG)))
    await extras.decide_report(out["id"], admin_id, True)
    async with session_scope() as s:
        games = (await s.execute(select(TournamentMatchGame).where(TournamentMatchGame.match_id == m.id))).scalars().all()
        assert [g.dota_match_id for g in games] == [111111111, 222222222, 333333333]
        assert (await s.get(TournamentMatch, m.id)).winner_team_id == m.team1_id
    board = (await client.get(f"/api/v2/public/tournaments/{tid}")).json()
    assert len(board["games"][str(m.id)]) == 3


async def test_opendota_rejects_wrong_or_unfinished_series(client):
    tid, teams, m = await _bracket(client)
    a1 = await _steam(m.team1_id, 3000)
    a2 = await _steam(m.team2_id, 4000)
    uid = await _uid(_tg(teams.index(m.team1_id)))
    with pytest.raises(Exception, match="не завершена"):
        await extras.create_report(m.id, uid, None, None, None, [1, 2], _dota_data=([_game(a1, a2, True), _game(a2, a1, True)], {}))
    with pytest.raises(Exception, match="нет участников"):
        await extras.create_report(m.id, uid, None, None, None, [5], _dota_data=([_game([1, 2, 3], [4, 5, 6], True)], {}))
    # Опознан один игрок — серия засчитана, но требует подтверждения соперника.
    out = await extras.create_report(m.id, uid, None, None, None, [7, 8],
                                     _dota_data=([_game(a1[:1] + [9, 9, 9, 9], a2, True), _game(a1[:1], a2, True)], {}))
    assert not out["verified"] and out["dota"]["warnings"]


async def _uid(tg: int) -> int:
    async with session_scope() as s:
        return int(await s.scalar(select(User.id).where(User.telegram_id == tg)))


async def test_predictions_and_leaderboard(client):
    tid, teams, m = await _bracket(client, bo=1)
    await client.get("/api/v2/me", headers=h(9601))
    await client.get("/api/v2/me", headers=h(9602))
    r = await client.post(f"/api/v2/matches/{m.id}/predict", headers=h(9601), json={"team_id": m.team1_id})
    assert r.status_code == 200 and r.json()["prediction"]["mine"] == m.team1_id
    assert (await client.post(f"/api/v2/matches/{m.id}/predict", headers=h(9602), json={"team_id": m.team2_id})).status_code == 200
    # Игрок матча за свой матч не голосует.
    r = await client.post(f"/api/v2/matches/{m.id}/predict", headers=h(_tg(teams.index(m.team1_id), 1)), json={"team_id": m.team1_id})
    assert r.status_code == 400
    st = (await client.get(f"/api/v2/matches/{m.id}", headers=h(9601))).json()["prediction"]
    assert st["total"] == 2 and st["open"]
    assert (await client.post(f"/api/v2/admin/matches/{m.id}/result", headers=h(ADMIN_TG), json={"score1": 1, "score2": 0})).status_code == 200
    assert (await client.post(f"/api/v2/matches/{m.id}/predict", headers=h(9601), json={"team_id": m.team2_id})).status_code == 400
    lb = (await client.get(f"/api/v2/tournaments/{tid}/predictions", headers=h(9601))).json()
    assert lb["items"][0]["correct"] == 1 and lb["me"]["place"] == 1
    assert (await client.get(f"/api/v2/matches/{m.id}", headers=h(9602))).json()["prediction"]["correct"] is False


async def test_fans_notified_once_when_match_starts(client):
    tid, teams, m = await _bracket(client, bo=1)
    await client.get("/api/v2/me", headers=h(9701))
    fan = await _uid(9701)
    async with session_scope() as s:
        s.add(TeamFollow(user_id=fan, team_id=m.team1_id))
        await s.commit()
    assert not [x for x in await reminders.fan_start_check() if x[2] == m.id]  # матч ещё не начался
    assert (await client.post(f"/api/v2/admin/matches/{m.id}/status", headers=h(ADMIN_TG), json={"status": "LIVE"})).status_code == 200
    hits = [x for x in await reminders.fan_start_check() if x[2] == m.id]
    assert len(hits) == 1 and 9701 in hits[0][0] and "twitch.tv" in hits[0][1]
    assert not [x for x in await reminders.fan_start_check() if x[2] == m.id]  # один раз


async def test_results_card_journal_and_roster_recheck(client):
    tid, teams, m = await _bracket(client, n=2, bo=1)
    assert (await client.get(f"/api/v2/public/tournaments/{tid}/results.png")).status_code == 404
    assert (await client.post(f"/api/v2/admin/matches/{m.id}/result", headers=h(ADMIN_TG), json={"score1": 1, "score2": 0})).status_code == 200
    png = await client.get(f"/api/v2/public/tournaments/{tid}/results.png")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/status", headers=h(ADMIN_TG), json={"status": "FINISHED"})
    assert r.status_code == 200 and r.json()["results_sent"] >= 2  # админ + игроки команд
    journal = (await client.get("/api/v2/admin/journal", headers=h(ADMIN_TG))).json()["items"]
    assert any("внёс результат" in e["what"] and "1:0" in e["what"] for e in journal)
    # Перепроверка составов: без привязанного Steam — понятная ошибка.
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/recheck-rosters", headers=h(ADMIN_TG))).status_code == 400
    from services.player_checks import tournament_players

    async with session_scope() as s:
        p = await s.scalar(select(PlayerProfile).join(TeamMember, TeamMember.user_id == PlayerProfile.user_id)
                           .where(TeamMember.team_id == teams[0]))
        p.account_id = 123456
        await s.commit()
        uid = int(p.user_id)
    assert await tournament_players(tid) == [uid]
    async with session_scope() as s:
        t = await s.get(Tournament, tid)
        assert t.status == TournamentStatus.FINISHED and timeutil.now_utc() - timedelta(days=1) < timeutil.now_utc()
