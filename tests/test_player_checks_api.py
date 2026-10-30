"""API 5.1: проверка игрока админом, источники в профиле, очередь MMR, аватарки игроков, расход AI."""
from __future__ import annotations

import pytest

pytest.importorskip("httpx")

from services import ai_budget  # noqa: E402
from services import player_checks as pc  # noqa: E402
from services.player_checks import Reading  # noqa: E402
from tests.test_miniapp_v2 import ADMIN_TG, client, h, mk_player  # noqa: E402,F401


@pytest.fixture()
def fake_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_budget, "PATH", tmp_path / "ai_usage.json")

    async def fake_collect(config, account_id, steam_id64):
        return [Reading("opendota", True, None, 52, None, {}), Reading("stratz", True, None, 52, None, {"match_count": 300, "win_count": 200}),
                Reading("dotabuff", False, "HTTP 403")]

    monkeypatch.setattr(pc, "collect", fake_collect)


async def test_admin_check_and_views(client, fake_sources):
    uid = await mk_player(8101, mmr=2500)
    assert (await client.post(f"/api/v2/admin/players/{uid}/check", headers=h(8101), json={})).status_code == 403
    r = await client.post(f"/api/v2/admin/players/{uid}/check", headers=h(ADMIN_TG), json={"use_ai": False})
    assert r.status_code == 200, r.text
    check = r.json()["check"]
    assert check["medal"]["tier"] == 52 and check["risk"]["level"] in ("medium", "high")
    assert any("занижение" in x["text"] for x in check["risk"]["reasons"])
    # Очередь проверки MMR и карточка пользователя показывают последнюю проверку.
    queue = (await client.get("/api/v2/admin/verification", headers=h(ADMIN_TG))).json()["items"]
    assert queue[0]["check"]["id"] == check["id"]
    detail = (await client.get(f"/api/v2/admin/users/{uid}", headers=h(ADMIN_TG))).json()
    assert detail["check"]["risk"]["score"] == check["risk"]["score"]
    hist = (await client.get(f"/api/v2/admin/players/{uid}/checks", headers=h(ADMIN_TG))).json()["items"]
    assert len(hist) == 1
    # Игрок видит источники медали, но не риск.
    prof = (await client.get("/api/v2/profile", headers=h(8101))).json()
    assert prof["medal_check"]["medal"]["name"] and "risk" not in prof["medal_check"]
    # Игрок без Steam — понятная ошибка.
    no_steam = await mk_player(8102, steam=False)
    r = await client.post(f"/api/v2/admin/players/{no_steam}/check", headers=h(ADMIN_TG), json={"use_ai": False})
    assert r.status_code == 400 and "Steam" in r.json()["detail"]
    status = (await client.get("/api/v2/admin/ai/status", headers=h(ADMIN_TG))).json()
    assert status["usage"]["check_limit"] >= 0


async def test_profile_sources_refresh_rate_limited(client, fake_sources):
    await mk_player(8201, mmr=3000)
    r = await client.post("/api/v2/profile/sources/refresh", headers=h(8201))
    assert r.status_code == 200 and r.json()["medal_check"]["medal"]["tier"] == 52
    assert (await client.post("/api/v2/profile/sources/refresh", headers=h(8201))).status_code == 429


async def test_player_avatar_served_only_from_local_file(client, monkeypatch, tmp_path):
    import miniapp_v2.common as common
    import miniapp_v2.routes_user as routes_user

    monkeypatch.setattr(common, "PLAYER_AVATAR_DIR", tmp_path)
    monkeypatch.setattr(routes_user, "PLAYER_AVATAR_DIR", tmp_path)
    uid = await mk_player(8301)
    assert (await client.get(f"/api/v2/players/{uid}/avatar")).status_code == 404
    (tmp_path / f"user_{uid}.jpg").write_bytes(b"\xff\xd8\xff\xe0jpegdata")
    r = await client.get(f"/api/v2/players/{uid}/avatar")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    me = (await client.get("/api/v2/me", headers=h(8301))).json()
    assert me["user"]["avatar_url"].startswith(f"/api/v2/players/{uid}/avatar")


async def test_sweep_checks_unchecked_players_and_suspicious_tab(client, fake_sources, monkeypatch):
    import asyncio

    import services.player_checks as pcm

    await mk_player(8401, mmr=2000)
    await mk_player(8402, mmr=5000)
    monkeypatch.setattr(pcm, "SWEEP_INTERVAL", 0.0)
    assert len(await pcm.users_to_check()) == 2
    r = await client.post("/api/v2/admin/players/check-all", headers=h(ADMIN_TG), json={})
    assert r.status_code == 200 and r.json()["sweep"]["total"] == 2
    for _ in range(100):
        if not pcm.SWEEP["running"]:
            break
        await asyncio.sleep(0.05)
    assert pcm.SWEEP["done"] == 2 and await pcm.users_to_check() == []
    data = (await client.get("/api/v2/admin/players/suspicious?level=all", headers=h(ADMIN_TG))).json()
    assert data["counts"]["checked"] == 2 and data["counts"]["unchecked"] == 0 and len(data["items"]) == 2
    risky = (await client.get("/api/v2/admin/players/suspicious", headers=h(ADMIN_TG))).json()["items"]
    assert all(it["check"]["risk"]["level"] in ("medium", "high") for it in risky)
    assert (await client.get("/api/v2/admin/players/suspicious", headers=h(8401))).status_code == 403


async def test_ban_blocks_bot_and_miniapp_until_unban(client):
    from types import SimpleNamespace

    from aiogram.types import Message

    from handlers.ban_middleware import BanMiddleware

    uid = await mk_player(8501)
    assert (await client.get("/api/v2/me", headers=h(8501))).status_code == 200
    r = await client.post(f"/api/v2/admin/users/{uid}/ban", headers=h(ADMIN_TG), json={"reason": "смурф"})
    assert r.status_code == 200
    r = await client.get("/api/v2/me", headers=h(8501))
    assert r.status_code == 403 and "заблокирован" in r.json()["detail"]
    assert (await client.get("/api/me", headers=h(8501))).status_code == 403  # старая Mini App тоже
    detail = (await client.get(f"/api/v2/admin/users/{uid}", headers=h(ADMIN_TG))).json()
    assert detail["ban"]["reason"] == "смурф"
    assert len((await client.get("/api/v2/admin/bans", headers=h(ADMIN_TG))).json()["items"]) == 1
    # Администратора заблокировать нельзя.
    from database.engine import session_scope
    from database.models import User
    from sqlalchemy import select

    async with session_scope() as s:
        admin_uid = (await s.scalar(select(User).where(User.telegram_id == ADMIN_TG))).id
    assert (await client.post(f"/api/v2/admin/users/{admin_uid}/ban", headers=h(ADMIN_TG), json={})).status_code == 400
    # Бот: обработчик не вызывается для заблокированного.
    called = []

    async def handler(event, data):
        called.append(1)

    msg = Message.model_construct(message_id=1, date=0, chat=SimpleNamespace(id=8501), text="/start")
    object.__setattr__(msg, "answer", lambda *a, **k: _noop())
    await BanMiddleware()(handler, msg, {"event_from_user": SimpleNamespace(id=8501)})
    assert called == []
    assert (await client.delete(f"/api/v2/admin/users/{uid}/ban", headers=h(ADMIN_TG))).status_code == 200
    assert (await client.get("/api/v2/me", headers=h(8501))).status_code == 200
    await BanMiddleware()(handler, msg, {"event_from_user": SimpleNamespace(id=8501)})
    assert called == [1]


async def _noop():
    return None
