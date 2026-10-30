"""5.9: игрок/зритель, болельщики, чат матча, матчи сегодня."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

pytest.importorskip("httpx")

from database.engine import session_scope  # noqa: E402
from database.models import Team, Tournament, TournamentMatch, User  # noqa: E402
from services import audience  # noqa: E402
from services.tournament_engine import service as engine  # noqa: E402
from services.tournament_engine import timeutil  # noqa: E402
from tests.test_miniapp_v2 import ADMIN_TG, client, h  # noqa: E402,F401
from tests.test_tournament_engine import _setup  # noqa: E402


async def test_viewer_choice_and_restrictions(client):
    me = (await client.get("/api/v2/me", headers=h(9101))).json()
    assert me["kind_chosen"] is False and me["stream"]["url"].endswith("inkoff_ttv")
    assert (await client.post("/api/v2/me/kind", headers=h(9101), json={"kind": "VIEWER"})).status_code == 200
    me = (await client.get("/api/v2/me", headers=h(9101))).json()
    assert me["kind"] == "VIEWER" and me["kind_chosen"] is True
    r = await client.post("/api/v2/team", headers=h(9101), json={"name": "Viewers United"})
    assert r.status_code == 403 and "зритель" in r.json()["detail"]
    tid, teams = await _setup(2)
    assert (await client.post(f"/api/v2/teams/{teams[0]}/follow", headers=h(9101), json={"follow": True})).status_code == 200
    assert (await client.get("/api/v2/me", headers=h(9101))).json()["follows"] == [teams[0]]
    assert (await client.post("/api/v2/me/kind", headers=h(9101), json={"kind": "PLAYER"})).status_code == 200
    assert (await client.post("/api/v2/team", headers=h(9101), json={"name": "Now Players"})).status_code != 403


async def test_match_chat_access_and_open_with_lobby(client):
    tid, teams = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "single_elim", "bo_default": 1, "bo_final": 1})).status_code == 200
    async with session_scope() as s:
        m = await s.scalar(select(TournamentMatch).where(TournamentMatch.tournament_id == tid, TournamentMatch.plan_key == "R1-1"))
        cap = (await s.get(User, (await s.get(Team, m.team1_id)).captain_user_id)).telegram_id
        await engine.set_schedule(s, m.id, (timeutil.now_utc() + timedelta(minutes=40)).isoformat(), force=True)
        await s.commit()
    outsider = 9202
    await client.get("/api/v2/me", headers=h(outsider))
    assert (await client.get(f"/api/v2/matches/{m.id}/chat", headers=h(outsider))).json()["open"] is False
    opened = await audience.open_due_chats()
    assert len(opened) == 1 and len(opened[0]["telegram_ids"]) == 10
    assert await audience.open_due_chats() == []  # один раз
    d = (await client.get(f"/api/v2/matches/{m.id}/chat", headers=h(cap))).json()
    assert d["open"] and d["can_write"] and "Пароль" in d["messages"][0]["text"]
    assert (await client.post(f"/api/v2/matches/{m.id}/chat", headers=h(cap), json={"text": "GL HF"})).status_code == 200
    assert (await client.post(f"/api/v2/matches/{m.id}/chat", headers=h(outsider), json={"text": "spam"})).status_code == 403
    live = (await client.get("/api/v2/live", headers=h(outsider))).json()
    assert any(x["id"] == m.id for x in live["items"]) or live["items"] == []  # сегодня по МСК, если матч не перевалил за полночь
