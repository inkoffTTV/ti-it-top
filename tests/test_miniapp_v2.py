"""Тесты Mini App 5.0 (/api/v2) и исправлений старого API.

Работают на временной SQLite-БД (фикстура db) с настоящей подписью Telegram
initData (тестовый токен). Сеть и Telegram API не используются.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import time
import urllib.parse

import pytest

# httpx нужен только тестам (в requirements его нет). Если в venv сервера его
# нет, тесты API пропускаются; HTTP-проверку выполняет deploy/smoke_v2.py.
httpx = pytest.importorskip("httpx")

from config import GLOBAL_CONFIG, Config  # noqa: E402
from database.engine import session_scope
from database.models import (
    ApplicationStatus,
    Notification,
    PlayerProfile,
    Team,
    TeamMember,
    Tournament,
    TournamentApplication,
    TournamentStatus,
    User,
    VerificationStatus,
)

BOT_TOKEN = "123456:TEST-TOKEN-FOR-MINIAPP-V2"
ADMIN_TG = 900001


def sign(tg_id: int, *, token: str = BOT_TOKEN, auth_date: int | None = None, username: str | None = None) -> str:
    user = json.dumps({"id": tg_id, "first_name": f"User{tg_id}", "username": username or f"user{tg_id}"}, separators=(",", ":"))
    pairs = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAEtest", "user": user}
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(pairs)


def h(tg_id: int) -> dict[str, str]:
    return {"X-Telegram-Init-Data": sign(tg_id)}


@pytest.fixture()
async def client(db, monkeypatch, tmp_path):
    monkeypatch.setenv("MINIAPP_V2_TELEGRAM_DISABLED", "1")
    monkeypatch.delenv("MINIAPP_ALLOW_DEV_AUTH", raising=False)
    import miniapp_v2.common as common
    import miniapp_v2.routes_user as routes_user

    avatars = tmp_path / "avatars"
    avatars.mkdir()
    monkeypatch.setattr(common, "TEAM_AVATAR_DIR", avatars)
    monkeypatch.setattr(routes_user, "TEAM_AVATAR_DIR", avatars)
    monkeypatch.setattr(common, "UI_FLAG_FILE", tmp_path / "MINIAPP_UI")
    routes_user._rate.clear()
    common.public_cache.invalidate()

    # Любая попытка создать Telegram Bot во время теста — ошибка.
    import aiogram

    def _no_bot(*args, **kwargs):
        raise AssertionError("Telegram Bot must not be created in these tests")

    monkeypatch.setattr(aiogram.Bot, "__init__", _no_bot)

    cfg = Config(bot_token=BOT_TOKEN, admin_ids={ADMIN_TG})
    saved = set(GLOBAL_CONFIG.admin_ids)
    # Как в bot.py: ADMIN_IDS синхронизируются в таблицу admin_users при старте.
    from services.admins import bootstrap_admins

    async with session_scope() as s:
        await bootstrap_admins(s, cfg)
    GLOBAL_CONFIG.admin_ids.clear()
    GLOBAL_CONFIG.admin_ids.update(cfg.admin_ids)
    from miniapp.app import create_app

    app = create_app(cfg)
    assert app.state.v2_mounted is True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.avatars = avatars  # type: ignore[attr-defined]
        c.tmp = tmp_path  # type: ignore[attr-defined]
        yield c
    GLOBAL_CONFIG.admin_ids.clear()
    GLOBAL_CONFIG.admin_ids.update(saved)


async def mk_player(tg_id: int, *, mmr: int | None = None, verified: bool = False, steam: bool = True,
                    positions: str | None = None, matches: int | None = None, winrate: float | None = None) -> int:
    async with session_scope() as s:
        user = User(telegram_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}", is_registered=True)
        s.add(user)
        await s.flush()
        if steam or mmr is not None:
            status = VerificationStatus.VERIFIED if verified else (VerificationStatus.PENDING if mmr is not None else VerificationStatus.UNVERIFIED)
            s.add(PlayerProfile(
                user_id=user.id,
                steam_id64=str(76561198000000000 + tg_id) if steam else None,
                account_id=tg_id if steam else None,
                verification_status=status,
                exact_mmr=mmr,
                approved_mmr=mmr if verified else None,
                mmr_verified=verified,
                preferred_positions=positions,
                matches=matches,
                winrate=winrate,
            ))
        await s.commit()
        return int(user.id)


async def mk_tournament(*, max_teams: int = 8, open_reg: bool = True) -> int:
    async with session_scope() as s:
        t = Tournament(
            name="Cup", description="Test cup", max_teams=max_teams,
            status=TournamentStatus.REGISTRATION if open_reg else TournamentStatus.DRAFT,
            registration_open=open_reg,
        )
        s.add(t)
        await s.commit()
        return int(t.id)


async def notifications_of(user_id: int) -> list[str]:
    from sqlalchemy import select

    async with session_scope() as s:
        rows = (await s.execute(select(Notification.kind).where(Notification.user_id == user_id))).scalars().all()
        return list(rows)


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------

async def test_auth_signature_required(client, monkeypatch):
    r = await client.get("/api/v2/me")
    assert r.status_code == 401
    assert "Telegram" in r.json()["detail"]

    bad = sign(1, token="999:OTHER")
    assert (await client.get("/api/v2/me", headers={"X-Telegram-Init-Data": bad})).status_code == 401

    old = sign(1, auth_date=int(time.time()) - 3 * 86400)
    r = await client.get("/api/v2/me", headers={"X-Telegram-Init-Data": old})
    assert r.status_code == 401 and "устарела" in r.json()["detail"]

    r = await client.get("/api/v2/me", headers=h(1))
    assert r.status_code == 200
    assert r.json()["user"]["telegram_id"] == 1


async def test_dev_auth_is_disabled_without_explicit_flag(client, monkeypatch):
    # Старый .env содержал MINIAPP_DEV_USER_ID = id администратора.
    monkeypatch.setenv("MINIAPP_DEV_USER_ID", str(ADMIN_TG))
    for path in ("/api/v2/admin/stats", "/api/admin/overview", "/api/me"):
        r = await client.get(path, headers={"X-Telegram-Init-Data": "dev:x"})
        assert r.status_code == 401, path
    monkeypatch.setenv("MINIAPP_ALLOW_DEV_AUTH", "1")
    assert (await client.get("/api/v2/admin/stats", headers={"X-Telegram-Init-Data": "dev:"})).status_code == 200


async def test_admin_routes_forbidden_for_players(client):
    await mk_player(5)
    for path in ("/api/v2/admin/stats", "/api/v2/admin/users", "/api/v2/admin/teams", "/api/v2/admin/tournaments",
                 "/api/v2/admin/applications", "/api/v2/admin/support", "/api/v2/admin/verification", "/api/v2/admin/admins",
                 "/api/v2/admin/broadcast/history", "/api/v2/admin/ai/status"):
        assert (await client.get(path, headers=h(5))).status_code == 403, path
    assert (await client.post("/api/v2/admin/broadcast", headers=h(5), json={"text": "x"})).status_code == 403


# ---------------------------------------------------------------------------
# Профиль и MMR: null не превращается в числа
# ---------------------------------------------------------------------------

async def test_profile_without_data_returns_nulls(client):
    r = await client.get("/api/v2/profile", headers=h(10))
    assert r.status_code == 200
    data = r.json()
    assert data["mmr"] == {"confirmed": None, "declared": None, "state": "none"}
    assert data["medal"] is None
    assert data["stats"]["matches"] is None and data["stats"]["public"] is False
    assert data["steam"]["linked"] is False


async def test_hidden_opendota_stats_are_not_shown_as_zero(client):
    await mk_player(11, mmr=3000, verified=True, matches=0, winrate=None)
    data = (await client.get("/api/v2/profile", headers=h(11))).json()
    assert data["mmr"]["confirmed"] == 3000
    assert data["stats"]["matches"] == 0 and data["stats"]["public"] is False


async def test_mmr_submit_requires_steam_and_goes_to_review(client):
    await mk_player(12, steam=False)
    r = await client.post("/api/v2/profile/mmr", headers=h(12), json={"mmr": 2500})
    assert r.status_code == 400 and "Steam" in r.json()["detail"]

    await mk_player(13)
    assert (await client.post("/api/v2/profile/mmr", headers=h(13), json={"mmr": "abc"})).status_code == 400
    assert (await client.post("/api/v2/profile/mmr", headers=h(13), json={"mmr": 12000})).status_code == 400
    r = await client.post("/api/v2/profile/mmr", headers=h(13), json={"mmr": 2500})
    assert r.status_code == 200, r.text
    mmr = (await client.get("/api/v2/profile", headers=h(13))).json()["mmr"]
    assert mmr == {"confirmed": None, "declared": 2500, "state": "pending"}


async def test_positions_saved(client):
    await mk_player(14)
    r = await client.post("/api/v2/profile/positions", headers=h(14), json={"positions": [5, 1, 1]})
    assert r.status_code == 200 and r.json()["positions"] == [1, 5]
    assert (await client.post("/api/v2/profile/positions", headers=h(14), json={"positions": [7]})).status_code == 400


# ---------------------------------------------------------------------------
# Команды: полный цикл
# ---------------------------------------------------------------------------

async def test_team_full_lifecycle(client):
    from miniapp_v2 import routes_user

    cap = await mk_player(100, mmr=4000, verified=True)
    player = await mk_player(101, mmr=3500, verified=True)
    unverified = await mk_player(102, mmr=3000, verified=False)
    other = await mk_player(103, mmr=2800, verified=True)

    r = await client.post("/api/v2/team", headers=h(100), json={"name": "Night Owls", "description": "Ищем керри\nвечером"})
    assert r.status_code == 200, r.text
    team = (await client.get("/api/v2/team", headers=h(100))).json()["team"]
    assert team["is_captain"] and team["description"] == "Ищем керри\nвечером"
    assert team["positions_edit"] == "self" and team["avg_mmr"] == 4000 and team["mmr_count"] == 1

    r = await client.patch("/api/v2/team", headers=h(100), json={"name": "Night Owls 2", "description": "  "})
    assert r.status_code == 200, r.text
    team = (await client.get("/api/v2/team", headers=h(100))).json()["team"]
    assert team["name"] == "Night Owls 2" and team["description"] is None

    r = await client.post("/api/v2/team/invites", headers=h(100), json={"user_id": unverified})
    assert r.status_code == 400 and "MMR" in r.json()["detail"]
    routes_user._rate.clear()
    r = await client.post("/api/v2/team/invites", headers=h(100), json={"user_id": player})
    assert r.status_code == 200, r.text
    assert "team_invite" in await notifications_of(player)

    incoming = (await client.get("/api/v2/team", headers=h(101))).json()
    assert incoming["team"] is None and len(incoming["incoming_invites"]) == 1
    invite_id = incoming["incoming_invites"][0]["id"]
    assert (await client.post(f"/api/v2/invites/{invite_id}/accept", headers=h(103))).status_code == 403
    r = await client.post(f"/api/v2/invites/{invite_id}/accept", headers=h(101))
    assert r.status_code == 200, r.text
    assert "invite_accepted" in await notifications_of(cap)

    team = (await client.get("/api/v2/team", headers=h(100))).json()["team"]
    assert team["roster_count"] == 2 and team["avg_mmr"] == 3750

    # PLAYER_CHOICE: игрок ставит свою позицию, капитан чужую — нельзя.
    assert (await client.post(f"/api/v2/team/members/{player}/position", headers=h(101), json={"position": 2})).status_code == 200
    assert (await client.post(f"/api/v2/team/members/{player}/position", headers=h(100), json={"position": 3})).status_code == 403
    # CAPTAIN_CHOICE: наоборот.
    assert (await client.post("/api/v2/team/role-mode", headers=h(100), json={"mode": "CAPTAIN_CHOICE"})).status_code == 200
    assert (await client.post(f"/api/v2/team/members/{player}/position", headers=h(100), json={"position": 3})).status_code == 200
    assert (await client.post(f"/api/v2/team/members/{player}/position", headers=h(101), json={"position": 4})).status_code == 403
    team = (await client.get("/api/v2/team", headers=h(101))).json()["team"]
    assert team["positions_edit"] == "none"
    assert [m["position"] for m in team["roster"] if m["user_id"] == player] == [3]

    # Исключение игрока капитаном.
    assert (await client.delete(f"/api/v2/team/members/{cap}", headers=h(100))).status_code == 400
    assert (await client.delete(f"/api/v2/team/members/{player}", headers=h(101))).status_code in (403, 404)
    r = await client.delete(f"/api/v2/team/members/{player}", headers=h(100))
    assert r.status_code == 200, r.text
    assert "team_member_removed" in await notifications_of(player)

    # Приглашение -> отклонение; приглашение -> отзыв.
    routes_user._rate.clear()
    await client.post("/api/v2/team/invites", headers=h(100), json={"user_id": other})
    inv = (await client.get("/api/v2/team", headers=h(103))).json()["incoming_invites"][0]["id"]
    assert (await client.post(f"/api/v2/invites/{inv}/decline", headers=h(103))).status_code == 200
    routes_user._rate.clear()
    await client.post("/api/v2/team/invites", headers=h(100), json={"user_id": other})
    out = (await client.get("/api/v2/team", headers=h(100))).json()["team"]["outgoing_invites"]
    assert len(out) == 1
    assert (await client.delete(f"/api/v2/team/invites/{out[0]['id']}", headers=h(103))).status_code == 403
    assert (await client.delete(f"/api/v2/team/invites/{out[0]['id']}", headers=h(100))).status_code == 200
    assert (await client.get("/api/v2/team", headers=h(103))).json()["incoming_invites"] == []

    # Выход игрока.
    routes_user._rate.clear()
    await client.post("/api/v2/team/invites", headers=h(100), json={"user_id": other})
    inv = (await client.get("/api/v2/team", headers=h(103))).json()["incoming_invites"][0]["id"]
    await client.post(f"/api/v2/invites/{inv}/accept", headers=h(103))
    assert (await client.post("/api/v2/team/leave", headers=h(100))).status_code == 400
    assert (await client.post("/api/v2/team/leave", headers=h(103))).status_code == 200
    assert "team_member_left" in await notifications_of(cap)

    # Расформирование (раньше падало с NameError: TeamInvite).
    assert (await client.post("/api/v2/team/delete", headers=h(100), json={})).status_code == 400
    r = await client.post("/api/v2/team/delete", headers=h(100), json={"confirm": True})
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v2/team", headers=h(100))).json()["team"] is None


async def test_team_delete_blocked_after_approval(client):
    await mk_player(200, mmr=4000, verified=True)
    await client.post("/api/v2/team", headers=h(200), json={"name": "Approved Team"})
    tid = await mk_tournament()
    async with session_scope() as s:
        team = (await s.execute(__import__("sqlalchemy").select(Team))).scalars().first()
        s.add(TournamentApplication(tournament_id=tid, team_id=team.id, created_by=team.captain_user_id, status=ApplicationStatus.APPROVED))
        await s.commit()
    data = (await client.get("/api/v2/team", headers=h(200))).json()["team"]
    assert data["can_delete"] is False and data["can_rename"] is False
    r = await client.post("/api/v2/team/delete", headers=h(200), json={"confirm": True})
    assert r.status_code == 400
    r = await client.patch("/api/v2/team", headers=h(200), json={"name": "Renamed"})
    assert r.status_code == 400


async def test_bot_delete_team_service_no_nameerror(db):
    """services/teams.py: TeamInvite был не импортирован -> NameError в боте и Mini App."""
    from services.teams import TeamService

    uid = await mk_player(300)
    async with session_scope() as s:
        team = await TeamService(s).create_team(uid, "Bot Team")
        members = await TeamService(s).delete_team(team.id, uid)
        await s.commit()
    assert members == [uid]


# ---------------------------------------------------------------------------
# Поиск игроков / команды
# ---------------------------------------------------------------------------

async def test_players_search_filters(client):
    from miniapp_v2 import routes_user

    await mk_player(400, mmr=4000, verified=True)
    await client.post("/api/v2/team", headers=h(400), json={"name": "Searchers"})
    a = await mk_player(401, mmr=5000, verified=True, positions="1,2")
    await mk_player(402, mmr=2000, verified=True, positions="5")
    await mk_player(403, mmr=3000, verified=False, positions="1")  # не подтверждён — не виден
    await mk_player(404, steam=False)  # без профиля — не виден

    data = (await client.get("/api/v2/players", headers=h(400))).json()
    assert data["total"] == 2 and [p["mmr"]["confirmed"] for p in data["items"]] == [5000, 2000]
    assert data["captain_team"]["can_invite"] is True
    assert (await client.get("/api/v2/players?position=1", headers=h(400))).json()["total"] == 1
    assert (await client.get("/api/v2/players?min_mmr=3000", headers=h(400))).json()["total"] == 1
    assert (await client.get("/api/v2/players?max_mmr=3000&sort=mmr_asc", headers=h(400))).json()["items"][0]["mmr"]["confirmed"] == 2000
    assert (await client.get("/api/v2/players?q=user401", headers=h(400))).json()["total"] == 1

    routes_user._rate.clear()
    await client.post("/api/v2/team/invites", headers=h(400), json={"user_id": a})
    items = (await client.get("/api/v2/players", headers=h(400))).json()["items"]
    assert [p["invited"] for p in items if p["user_id"] == a] == [True]
    card = (await client.get(f"/api/v2/players/{a}", headers=h(400))).json()
    assert card["can_invite"] is True and card["invited"] is True and card["team"] is None

    teams = (await client.get("/api/v2/teams?q=search", headers=h(401))).json()
    assert teams["total"] == 1 and teams["items"][0]["name"] == "Searchers"
    tid = teams["items"][0]["id"]
    card = (await client.get(f"/api/v2/teams/{tid}", headers=h(401))).json()
    assert card["roster_count"] == 1 and card["avg_mmr"] == 4000


# ---------------------------------------------------------------------------
# Турниры и заявки
# ---------------------------------------------------------------------------

async def test_tournament_apply_and_admin_decision(client):
    captain = await mk_player(500, mmr=4000, verified=True)
    await client.post("/api/v2/team", headers=h(500), json={"name": "Five Stack"})
    tid = await mk_tournament(max_teams=2)

    detail = (await client.get(f"/api/v2/tournaments/{tid}", headers=h(500))).json()
    assert detail["can_apply"] is False and "минимум 5" in detail["apply_block_reason"]

    async with session_scope() as s:
        team = (await s.execute(__import__("sqlalchemy").select(Team))).scalars().first()
        for i in range(4):
            u = User(telegram_id=510 + i, username=f"m{i}", is_registered=True)
            s.add(u)
            await s.flush()
            s.add(PlayerProfile(user_id=u.id, verification_status=VerificationStatus.VERIFIED, exact_mmr=3000, approved_mmr=3000, mmr_verified=True))
            s.add(TeamMember(team_id=team.id, user_id=u.id))
        await s.commit()

    detail = (await client.get(f"/api/v2/tournaments/{tid}", headers=h(500))).json()
    assert detail["can_apply"] is True, detail
    r = await client.post(f"/api/v2/tournaments/{tid}/apply", headers=h(500))
    assert r.status_code == 200, r.text
    app_id = r.json()["application_id"]
    listing = (await client.get("/api/v2/tournaments", headers=h(500))).json()
    assert listing["items"][0]["my_application"] == "PENDING"

    apps = (await client.get("/api/v2/admin/applications?status=PENDING", headers=h(ADMIN_TG))).json()
    assert apps["total"] == 1 and apps["counts"]["PENDING"] == 1
    r = await client.post(f"/api/v2/admin/applications/{app_id}/decision", headers=h(ADMIN_TG), json={"status": "APPROVED"})
    assert r.status_code == 200, r.text
    assert "application_decision" in await notifications_of(captain)
    assert (await client.post(f"/api/v2/admin/applications/{app_id}/decision", headers=h(ADMIN_TG), json={"status": "REJECTED"})).status_code == 400
    detail = (await client.get(f"/api/v2/tournaments/{tid}", headers=h(500))).json()
    assert detail["approved_count"] == 1 and detail["teams"][0]["name"] == "Five Stack"


async def test_admin_approve_respects_tournament_limit(client):
    tid = await mk_tournament(max_teams=2)
    async with session_scope() as s:
        ids = []
        for i in range(3):
            u = User(telegram_id=600 + i, is_registered=True)
            s.add(u)
            await s.flush()
            t = Team(name=f"T{i}", captain_user_id=u.id)
            s.add(t)
            await s.flush()
            app = TournamentApplication(tournament_id=tid, team_id=t.id, created_by=u.id,
                                        status=ApplicationStatus.APPROVED if i < 2 else ApplicationStatus.PENDING)
            s.add(app)
            await s.flush()
            ids.append(app.id)
        await s.commit()
    r = await client.post(f"/api/v2/admin/applications/{ids[2]}/decision", headers=h(ADMIN_TG), json={"status": "APPROVED"})
    assert r.status_code == 400 and "Лимит" in r.json()["detail"]
    r = await client.post(f"/api/v2/admin/applications/{ids[2]}/decision", headers=h(ADMIN_TG), json={"status": "CANCELLED", "comment": "мест нет"})
    assert r.status_code == 200


async def test_admin_tournament_crud_and_bracket(client):
    r = await client.post("/api/v2/admin/tournaments", headers=h(ADMIN_TG), json={"name": "Friday Cup", "max_teams": 4, "start_date": "2026-10-02T17:00:00.000Z"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    detail = (await client.get(f"/api/v2/admin/tournaments/{tid}", headers=h(ADMIN_TG))).json()
    assert detail["status"] == "DRAFT" and detail["start_date"] == "2026-10-02T17:00:00Z"
    # Черновик не виден игрокам.
    await mk_player(700)
    assert (await client.get(f"/api/v2/tournaments/{tid}", headers=h(700))).status_code == 404
    assert (await client.patch(f"/api/v2/admin/tournaments/{tid}", headers=h(ADMIN_TG), json={"max_teams": 1})).status_code == 400
    assert (await client.patch(f"/api/v2/admin/tournaments/{tid}", headers=h(ADMIN_TG), json={"name": "Friday Cup 2", "start_date": None})).status_code == 200
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/status", headers=h(ADMIN_TG), json={"status": "REGISTRATION"})
    assert r.json()["registration_open"] is True
    assert (await client.get(f"/api/v2/tournaments/{tid}", headers=h(700))).json()["name"] == "Friday Cup 2"
    bracket = (await client.get(f"/api/v2/admin/tournaments/{tid}/bracket", headers=h(ADMIN_TG))).json()
    assert bracket["exists"] is False and bracket["team_count"] == 0
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/bracket/generate", headers=h(ADMIN_TG), json={"opening_team_a": 1, "opening_team_b": 2})
    assert r.status_code == 400  # нет одобренных команд — понятная ошибка, не 500


# ---------------------------------------------------------------------------
# Админка: проверка MMR, пользователи, поддержка, админы, рассылка
# ---------------------------------------------------------------------------

async def test_admin_verification_flow(client):
    uid = await mk_player(800, mmr=3100, verified=False)
    queue = (await client.get("/api/v2/admin/verification", headers=h(ADMIN_TG))).json()
    assert queue["total"] == 1 and queue["items"][0]["mmr"]["declared"] == 3100
    pid = queue["items"][0]["profile_id"]
    r = await client.post(f"/api/v2/admin/verification/{pid}", headers=h(ADMIN_TG), json={"decision": "approve", "mmr": 3150})
    assert r.status_code == 200, r.text
    mmr = (await client.get("/api/v2/profile", headers=h(800))).json()["mmr"]
    assert mmr == {"confirmed": 3150, "declared": 3150, "state": "confirmed"}
    assert "mmr_decision" in await notifications_of(uid)
    r = await client.post(f"/api/v2/admin/verification/{pid}", headers=h(ADMIN_TG), json={"decision": "reject", "comment": "не совпадает"})
    assert r.status_code == 200
    mmr = (await client.get("/api/v2/profile", headers=h(800))).json()["mmr"]
    assert mmr["confirmed"] is None and mmr["state"] == "rejected"


async def test_admin_users_stats_teams(client):
    await mk_player(900, mmr=3000, verified=True)
    await mk_player(901, steam=False)
    users = (await client.get("/api/v2/admin/users?limit=1", headers=h(ADMIN_TG))).json()
    assert users["limit"] == 1 and users["has_more"] is True and users["total"] >= 3
    unlinked = (await client.get("/api/v2/admin/users?status=UNLINKED", headers=h(ADMIN_TG))).json()
    assert all(u["status"] == "unlinked" for u in unlinked["items"])
    detail = (await client.get(f"/api/v2/admin/users/{users['items'][0]['user_id']}", headers=h(ADMIN_TG))).json()
    assert "history" in detail
    stats = (await client.get("/api/v2/admin/stats", headers=h(ADMIN_TG))).json()
    assert stats["counts"]["free_agents"] == 1 and stats["telegram_notifications"] is False
    assert (await client.get("/api/v2/admin/teams", headers=h(ADMIN_TG))).json()["total"] == 0


async def test_support_and_admin_reply_close(client):
    uid = await mk_player(1000)
    r = await client.post("/api/v2/support", headers=h(1000), json={"message": "Не могу привязать Steam"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    tickets = (await client.get("/api/v2/admin/support?status=OPEN", headers=h(ADMIN_TG))).json()
    assert tickets["total"] == 1 and tickets["counts"]["OPEN"] == 1
    assert (await client.post(f"/api/v2/admin/support/{tid}/reply", headers=h(ADMIN_TG), json={"reply": "Проверьте приватность"})).status_code == 200
    mine = (await client.get("/api/v2/support", headers=h(1000))).json()["items"][0]
    assert mine["status"] == "ANSWERED" and mine["reply"] == "Проверьте приватность"
    assert "support_reply" in await notifications_of(uid)
    assert (await client.post(f"/api/v2/admin/support/{tid}/close", headers=h(ADMIN_TG))).status_code == 200
    assert (await client.get("/api/v2/admin/support?status=CLOSED", headers=h(ADMIN_TG))).json()["total"] == 1


async def test_private_events_not_in_public_feeds(client):
    """Бот пишет ответы поддержки публичным событием с текстом — ленты их скрывают."""
    from database.models import ActivityEvent

    await mk_player(1050)
    async with session_scope() as s:
        s.add(ActivityEvent(kind="support_reply", title="Ответ поддержки", body="секретный ответ", target_user_id=1))
        s.add(ActivityEvent(kind="team_member_removed", title="Вы исключены из команды", body="x", target_user_id=1))
        s.add(ActivityEvent(kind="team_created", title="Создана команда", body="Public Team"))
        await s.commit()
    v2 = (await client.get("/api/v2/events", headers=h(1050))).json()["items"]
    legacy = (await client.get("/api/events", headers=h(1050))).json()["items"]
    for feed in (v2, legacy):
        kinds = {e["kind"] for e in feed}
        assert "team_created" in kinds
        assert not kinds & {"support_reply", "team_member_removed"}


async def test_admins_add_remove_guards(client):
    r = await client.delete(f"/api/v2/admin/admins/{ADMIN_TG}", headers=h(ADMIN_TG))
    assert r.status_code == 400
    assert (await client.post("/api/v2/admin/admins", headers=h(ADMIN_TG), json={"telegram_id": 777})).status_code == 200
    assert (await client.get("/api/v2/admin/stats", headers=h(777))).status_code == 200
    assert (await client.delete("/api/v2/admin/admins/777", headers=h(ADMIN_TG))).status_code == 200
    assert (await client.get("/api/v2/admin/stats", headers=h(777))).status_code == 403


async def test_broadcast_refuses_when_telegram_disabled(client):
    r = await client.post("/api/v2/admin/broadcast", headers=h(ADMIN_TG), json={"text": "Привет"})
    assert r.status_code == 409
    assert (await client.get("/api/v2/admin/broadcast/status", headers=h(ADMIN_TG))).status_code == 200


# ---------------------------------------------------------------------------
# Чат, лента, уведомления
# ---------------------------------------------------------------------------

async def test_chat_events_notifications(client):
    from miniapp_v2 import routes_user

    await mk_player(1100)
    await mk_player(1101)
    r = await client.post("/api/v2/chat", headers=h(1100), json={"text": "gl hf"})
    assert r.status_code == 200 and r.json()["item"]["mine"] is True
    assert (await client.post("/api/v2/chat", headers=h(1100), json={"text": "again"})).status_code == 429
    routes_user._rate.clear()
    first = r.json()["item"]["id"]
    polled = (await client.get(f"/api/v2/chat?after_id={first - 1}", headers=h(1101))).json()
    assert [m["text"] for m in polled["items"]] == ["gl hf"] and polled["items"][0]["mine"] is False
    assert (await client.delete(f"/api/v2/chat/{first}", headers=h(1101))).status_code == 403
    assert (await client.delete(f"/api/v2/chat/{first}", headers=h(ADMIN_TG))).status_code == 200
    polled = (await client.get(f"/api/v2/chat?after_id={first}", headers=h(1101))).json()
    assert first in polled["deleted_ids"]
    assert (await client.get("/api/v2/events", headers=h(1101))).status_code == 200
    n = (await client.get("/api/v2/notifications", headers=h(1101))).json()
    assert n["unread"] == 0
    assert (await client.post("/api/v2/notifications/read-all", headers=h(1101))).json()["ok"] is True


# ---------------------------------------------------------------------------
# Аватары: только локальные файлы, без Telegram
# ---------------------------------------------------------------------------

async def test_avatar_upload_and_local_only_serving(client):
    from PIL import Image

    await mk_player(1200)
    await client.post("/api/v2/team", headers=h(1200), json={"name": "Pixel"})
    team = (await client.get("/api/v2/team", headers=h(1200))).json()["team"]
    assert team["avatar_url"] is None
    # Команда с file_id из Telegram, но без локального файла: 404 без обращения к Telegram.
    async with session_scope() as s:
        t = await s.get(Team, team["id"])
        t.avatar_file_id = "AgACAgIAAxkBAAIB"
        await s.commit()
    assert (await client.get(f"/api/v2/teams/{team['id']}/avatar")).status_code == 404
    assert (await client.get(f"/api/teams/{team['id']}/avatar", headers=h(1200))).status_code == 404

    img = io.BytesIO()
    Image.new("RGBA", (900, 600), (200, 50, 50, 255)).save(img, format="PNG")
    r = await client.post("/api/v2/team/avatar", headers=h(1200), files={"file": ("a.png", img.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    url = r.json()["avatar_url"]
    got = await client.get(url)
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(got.content)) as im:
        assert max(im.size) <= 512
    bad = await client.post("/api/v2/team/avatar", headers=h(1200), files={"file": ("a.txt", b"hello", "text/plain")})
    assert bad.status_code == 400
    async with session_scope() as s:
        assert (await s.get(Team, team["id"])).avatar_file_id is None


async def test_avatar_sync_reports_failure_without_telegram(client):
    await mk_player(1300)
    await client.post("/api/v2/team", headers=h(1300), json={"name": "TG Only"})
    async with session_scope() as s:
        t = (await s.execute(__import__("sqlalchemy").select(Team))).scalars().first()
        t.avatar_file_id = "AgACAgIAAxkBAAIC"
        await s.commit()
    r = await client.post("/api/v2/admin/avatars/sync", headers=h(ADMIN_TG))
    assert r.status_code == 200
    data = r.json()
    assert data["checked"] == 1 and len(data["failed"]) == 1 and data["saved"] == []


# ---------------------------------------------------------------------------
# Старый API: исправления и отсутствие Telegram на загрузке страниц
# ---------------------------------------------------------------------------

async def test_legacy_overview_and_decision_fixed(client):
    await mk_player(1400, mmr=2000, verified=False)
    r = await client.get("/api/admin/overview", headers=h(ADMIN_TG))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["stats"]["pending_players"] == 1 and len(data["pending"]) == 1
    tid = await mk_tournament()
    async with session_scope() as s:
        u = await s.scalar(__import__("sqlalchemy").select(User).where(User.telegram_id == 1400))
        t = Team(name="Legacy", captain_user_id=u.id)
        s.add(t)
        await s.flush()
        s.add(TeamMember(team_id=t.id, user_id=u.id))
        app = TournamentApplication(tournament_id=tid, team_id=t.id, created_by=u.id, status=ApplicationStatus.PENDING)
        s.add(app)
        await s.commit()
        app_id = app.id
    data = (await client.get("/api/admin/overview", headers=h(ADMIN_TG))).json()
    assert data["teams"][0]["members"] == 1 and data["tournaments"][0]["slots_left"] == 8
    # Раньше: NameError 'CANCELLED' -> 500 на любое решение по заявке в старой админке.
    r = await client.post(f"/api/admin/applications/{app_id}/decision", headers=h(ADMIN_TG), json={"status": "CANCELLED"})
    assert r.status_code == 200, r.text


async def test_page_loads_do_not_touch_telegram(client, monkeypatch):
    from miniapp_v2 import notify

    calls = []
    monkeypatch.setattr(notify, "fire", lambda job, label: calls.append(label))
    await mk_player(1500, mmr=3000, verified=True)
    await client.post("/api/v2/team", headers=h(1500), json={"name": "Quiet"})
    calls.clear()
    for path in ("/api/v2/me", "/api/v2/profile", "/api/v2/team", "/api/v2/players", "/api/v2/teams", "/api/v2/tournaments",
                 "/api/v2/chat", "/api/v2/events", "/api/v2/notifications", "/api/v2/support"):
        assert (await client.get(path, headers=h(1500))).status_code == 200, path
    for path in ("/api/v2/admin/stats", "/api/v2/admin/users", "/api/v2/admin/teams", "/api/v2/admin/tournaments",
                 "/api/v2/admin/applications", "/api/v2/admin/support", "/api/v2/admin/verification", "/api/v2/admin/admins"):
        assert (await client.get(path, headers=h(ADMIN_TG))).status_code == 200, path
    assert calls == []


async def test_ui_switch_between_v2_and_legacy(client):
    import miniapp_v2.common as common

    r = await client.get("/")
    assert r.status_code == 200 and "/v2/static/app.js" in r.text
    common.UI_FLAG_FILE.write_text("legacy", encoding="utf-8")
    r = await client.get("/")
    assert "/assets/js/app.js" in r.text
    assert "/v2/static/app.js" in (await client.get("/v2/")).text
    assert "/assets/js/app.js" in (await client.get("/legacy/")).text
    assert (await client.get("/v2/static/app.js")).status_code == 200
    assert (await client.get("/api/v2/health")).json()["ok"] is True
