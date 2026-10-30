"""API турнирного движка 5.2 (/api/v2): права, сетка, расписание по МСК, результаты, каскад."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select  # noqa: F401

from database.engine import session_scope
from database.models import Notification, TournamentMatch

pytest.importorskip("httpx")

from tests.test_miniapp_v2 import ADMIN_TG, client, h, mk_player, mk_tournament  # noqa: E402,F401
from tests.test_tournament_engine import _setup  # noqa: E402
from services.tournament_engine import timeutil as _timeutil  # noqa: E402


async def _gen(client, tid, **extra):
    body = {"format": "double_elim", "seeding": "standard", "bo_default": 3, "bo_final": 1, **extra}
    return await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG), json=body)


async def _keys(tid):
    async with session_scope() as s:
        rows = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid))).scalars().all()
        return {m.plan_key: m for m in rows}


async def test_engine_admin_only_and_generate(client):
    tid, teams = await _setup(8)
    player_tg = 10_000 + 100_000 * 999
    await mk_player(player_tg)
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(player_tg), json={"format": "double_elim"})
    assert r.status_code == 403
    r = await _gen(client, tid)
    assert r.status_code == 200, r.text
    eng = r.json()["engine"]
    assert [b["key"] for b in eng["brackets"]] == ["upper", "lower", "final"]
    assert eng["total"] == 14 and eng["settings"]["bo_final"] == 1
    # Публичная карточка турнира: сетка движка + прежний формат для старых клиентов.
    detail = (await client.get(f"/api/v2/tournaments/{tid}", headers=h(player_tg))).json()
    assert detail["bracket"]["engine"]["format"] == "double_elim"
    assert detail["bracket"]["bracket"]["rounds"][0]["matches"][0]["team_a"]["id"]
    # Неизвестный формат — понятная 400.
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG), json={"format": "ladder"})
    assert r.status_code == 400 and "Неизвестный формат" in r.json()["detail"]


async def test_schedule_result_cascade_and_notifications(client, monkeypatch):
    # Тест про 02.10.2026: «сейчас» — утро того дня, иначе сработает «время уже прошло»
    monkeypatch.setattr(_timeutil, "now_utc", lambda: datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc))
    tid, teams = await _setup(8)
    assert (await _gen(client, tid)).status_code == 200
    ms = await _keys(tid)
    u1 = ms["U1-1"]
    # Время по Москве: 18:00 МСК = 15:00 UTC.
    r = await client.post(f"/api/v2/admin/matches/{u1.id}/schedule", headers=h(ADMIN_TG), json={"when": "02.10.2026 18:00"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SCHEDULED" and r.json()["scheduled_text"] == "02.10.2026 18:00"
    detail = (await client.get(f"/api/v2/matches/{u1.id}", headers=h(ADMIN_TG))).json()
    assert detail["scheduled_at"] == "2026-10-02T15:00:00Z" and detail["scheduled_input"] == "2026-10-02T18:00"
    assert detail["can_edit"] is True and detail["log"][0]["action"] == "schedule"
    # Предупреждение с подтверждением: раньше старта турнира (17:50 МСК).
    r = await client.post(f"/api/v2/admin/matches/{ms['U1-2'].id}/schedule", headers=h(ADMIN_TG), json={"when": "02.10.2026 12:00"})
    assert r.status_code == 409 and r.json()["need_confirm"] and r.json()["warnings"]
    r = await client.post(f"/api/v2/admin/matches/{ms['U1-2'].id}/schedule", headers=h(ADMIN_TG), json={"when": "02.10.2026 12:00", "force": True})
    assert r.status_code == 200
    # Неверный счёт BO3 — 400 с объяснением.
    r = await client.post(f"/api/v2/admin/matches/{u1.id}/result", headers=h(ADMIN_TG), json={"score1": 1, "score2": 0})
    assert r.status_code == 400 and "2:0" in r.json()["detail"]
    for key in ("U1-1", "U1-2"):
        r = await client.post(f"/api/v2/admin/matches/{ms[key].id}/result", headers=h(ADMIN_TG), json={"score1": 2, "score2": 1})
        assert r.status_code == 200, r.text
    ms = await _keys(tid)
    assert (await client.post(f"/api/v2/admin/matches/{ms['U2-1'].id}/result", headers=h(ADMIN_TG), json={"score1": 2, "score2": 0})).status_code == 200
    # Смена победителя U1-1 затрагивает сыгранный U2-1 -> 409, затем каскад.
    r = await client.post(f"/api/v2/admin/matches/{u1.id}/result", headers=h(ADMIN_TG), json={"score1": 0, "score2": 2})
    assert r.status_code == 409 and r.json()["affected"][0]["no"] == ms["U2-1"].match_no
    r = await client.post(f"/api/v2/admin/matches/{u1.id}/result", headers=h(ADMIN_TG), json={"score1": 0, "score2": 2, "cascade": True})
    assert r.status_code == 200 and r.json()["winner_changed"] is True
    after = await _keys(tid)
    assert after["U2-1"].status in ("WAITING", "SCHEDULED") and after["U2-1"].winner_team_id is None
    # Игроки обеих команд получили уведомления о результате.
    async with session_scope() as s:
        n = int(await s.scalar(select(func.count(Notification.id)).where(Notification.kind == "match_result")) or 0)
    assert n >= 10
    # Тех. победа, статус и BO.
    r = await client.post(f"/api/v2/admin/matches/{after['U1-3'].id}/result", headers=h(ADMIN_TG), json={"forfeit_winner": 2})
    assert r.status_code == 200
    assert (await _keys(tid))["U1-3"].score2 == 2
    assert (await client.post(f"/api/v2/admin/matches/{after['U1-4'].id}/status", headers=h(ADMIN_TG), json={"status": "LIVE"})).json()["status"] == "LIVE"
    assert (await client.post(f"/api/v2/admin/matches/{after['U1-4'].id}/best-of", headers=h(ADMIN_TG), json={"best_of": 5})).json()["best_of"] == 5
    assert (await client.post(f"/api/v2/admin/matches/{after['U1-4'].id}/status", headers=h(ADMIN_TG), json={"status": "FINISHED"})).status_code == 400


async def test_schedule_view_mine_filter_and_player_view(client):
    tid, teams = await _setup(4)
    assert (await _gen(client, tid, format="single_elim", bo_default=1, bo_final=3)).status_code == 200
    captain_tg = 10_000 + 100_000 * (__import__("tests.test_tournament_engine", fromlist=["_SETUP_SEQ"])._SETUP_SEQ[0])
    data = (await client.get(f"/api/v2/tournaments/{tid}/schedule", headers=h(captain_tg))).json()
    assert data["exists"] and data["tz"]["label"] == "МСК" and len(data["items"]) == 3
    assert data["my_team_id"] == teams[0] and sum(1 for m in data["items"] if m["mine"]) >= 1
    m = data["items"][0]
    player_view = (await client.get(f"/api/v2/matches/{m['id']}", headers=h(captain_tg))).json()
    assert player_view["can_edit"] is False and "log" not in player_view
    r = await client.post(f"/api/v2/admin/matches/{m['id']}/result", headers=h(captain_tg), json={"score1": 1, "score2": 0})
    assert r.status_code == 403


async def test_draft_tournament_schedule_hidden_and_import_legacy(client, tmp_path, monkeypatch):
    draft = await mk_tournament(open_reg=False)
    await mk_player(4242)
    assert (await client.get(f"/api/v2/tournaments/{draft}/schedule", headers=h(4242))).status_code == 404
    # Перенос старой JSON-сетки через API.
    import services.bracket as bracket

    monkeypatch.setattr(bracket, "BRACKET_DIR", tmp_path / "brackets")
    tid, teams = await _setup(8)
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/bracket/generate", headers=h(ADMIN_TG),
                          json={"opening_team_a": teams[0], "opening_team_b": teams[4]})
    assert r.status_code == 200 and "engine" not in r.json()
    first = r.json()["bracket"]["rounds"][0]["matches"]
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/engine/import-legacy", headers=h(ADMIN_TG),
                          json={"format": "double_elim", "bo_default": 3, "bo_final": 1})
    assert r.status_code == 200, r.text
    ms = await _keys(tid)
    assert [ms["U1-1"].team1_id, ms["U1-1"].team2_id] == [first[0]["team_a"]["id"], first[0]["team_b"]["id"]]
    # Повторный перенос — понятная ошибка, а старая кнопка «Пересобрать» идёт в движок.
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/import-legacy", headers=h(ADMIN_TG), json={})).status_code == 400
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/bracket/generate", headers=h(ADMIN_TG),
                          json={"opening_team_a": teams[1], "opening_team_b": teams[2]})
    assert r.status_code == 200 and r.json()["engine"]["format"] == "double_elim"
    # Сброс требует подтверждения только если есть результаты/фиксация.
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/reset", headers=h(ADMIN_TG), json={})).status_code == 200
    assert await _keys(tid) == {}


async def test_tournament_start_date_is_moscow_time(client):
    r = await client.post("/api/v2/admin/tournaments", headers=h(ADMIN_TG), json={"name": "MSK Cup", "max_teams": 8, "start_date": "2026-10-02T17:50"})
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/api/v2/admin/tournaments/{r.json()['id']}", headers=h(ADMIN_TG))).json()
    assert detail["start_date"] == "2026-10-02T14:50:00Z"


async def test_coach_role_and_manual_matches(client):
    from database.models import Team, TeamMember, User

    tid, teams = await _setup(4)
    async with session_scope() as s:
        team = await s.get(Team, teams[0])
        cap_tg = (await s.get(User, team.captain_user_id)).telegram_id
        member = (await s.execute(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id != team.captain_user_id))).scalars().first()
        member_uid = member.user_id
    # Тренер: назначает капитан, не игрок в составе и в среднем MMR.
    r = await client.post(f"/api/v2/team/members/{member_uid}/coach", headers=h(cap_tg), json={"coach": True})
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/api/v2/teams/{teams[0]}", headers=h(cap_tg))).json()
    roles = {m["user_id"]: m["role"] for m in detail["roster"]}
    assert roles[member_uid] == "COACH"
    assert (await client.post(f"/api/v2/team/members/{member_uid}/coach", headers=h(cap_tg), json={"coach": False})).status_code == 200
    # Ручные матчи: админ сам ставит пары, меняет команды, удаляет.
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG), json={"format": "manual", "bo_default": 1})).status_code == 200
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/engine/matches", headers=h(ADMIN_TG),
                          json={"team1_id": teams[0], "team2_id": teams[1], "best_of": 3, "when": "02.10.2026 18:00"})
    assert r.status_code == 200, r.text
    mid = r.json()["match_id"]
    detail = (await client.get(f"/api/v2/matches/{mid}", headers=h(ADMIN_TG))).json()
    assert detail["manual"] and detail["scheduled_text"] == "02.10.2026 18:00" and len(detail["team_options"]) == 4
    r = await client.post(f"/api/v2/admin/matches/{mid}/teams", headers=h(ADMIN_TG), json={"team1_id": teams[2], "team2_id": teams[3]})
    assert r.status_code == 200
    assert {(await _keys(tid))["M1"].team1_id, (await _keys(tid))["M1"].team2_id} == {teams[2], teams[3]}
    assert (await client.post(f"/api/v2/admin/matches/{mid}/result", headers=h(ADMIN_TG), json={"score1": 2, "score2": 1})).status_code == 200
    eng = (await client.get(f"/api/v2/tournaments/{tid}/engine", headers=h(ADMIN_TG))).json()["engine"]
    assert eng["standings"][0]["rows"][0]["points"] == 3
    assert (await client.delete(f"/api/v2/admin/matches/{mid}", headers=h(ADMIN_TG))).status_code == 400  # сыгран
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/engine/matches", headers=h(ADMIN_TG), json={"team1_id": teams[0], "team2_id": teams[1]})
    assert (await client.delete(f"/api/v2/admin/matches/{r.json()['match_id']}", headers=h(ADMIN_TG))).status_code == 200


async def test_set_teams_warns_when_slot_is_fed_by_result(client):
    tid, teams = await _setup(4)
    assert (await _gen(client, tid, format="single_elim", bo_default=1, bo_final=1)).status_code == 200
    ms = await _keys(tid)
    r = await client.post(f"/api/v2/admin/matches/{ms['R2-1'].id}/teams", headers=h(ADMIN_TG), json={"team1_id": teams[0], "team2_id": teams[1]})
    assert r.status_code == 400  # обе команды уже стоят в 1-м раунде — дубль запрещён
    r = await client.post(f"/api/v2/admin/matches/{ms['R1-1'].id}/teams", headers=h(ADMIN_TG),
                          json={"team1_id": ms["R1-2"].team1_id, "team2_id": ms["R1-1"].team2_id})
    assert r.status_code == 400 and "уже стоит в матче" in r.json()["detail"]  # дубль запрещён
    r = await client.post(f"/api/v2/admin/matches/{ms['R1-1'].id}/teams", headers=h(ADMIN_TG),
                          json={"team1_id": ms["R1-1"].team2_id, "team2_id": ms["R1-1"].team1_id})
    assert r.status_code == 200  # поменять местами — можно


async def test_bye_team_cannot_be_duplicated_into_first_round(client):
    tid, teams = await _setup(6)  # 8 мест, 2 BYE
    assert (await _gen(client, tid, format="single_elim", bo_default=1, bo_final=1)).status_code == 200
    eng = (await client.get(f"/api/v2/tournaments/{tid}/engine", headers=h(ADMIN_TG))).json()["engine"]
    r2 = [m for b in eng["brackets"] for r in b["rounds"] if r["round_no"] == 2 for m in r["matches"]]
    bye_team = next(m["team1"] or m["team2"] for m in r2 if m["team1_bye"] or m["team2_bye"])
    r1 = [m for b in eng["brackets"] for r in b["rounds"] if r["round_no"] == 1 for m in r["matches"]]
    r = await client.post(f"/api/v2/admin/matches/{r1[0]["id"]}/teams", headers=h(ADMIN_TG),
                          json={"team1_id": bye_team["id"], "team2_id": r1[0]["team2"]["id"], "force": True})
    assert r.status_code == 400  # даже с force: команда с BYE уже стоит во 2-м раунде
