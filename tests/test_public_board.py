"""5.10: публичное табло для проектора/стрима — без авторизации и без личных данных."""
from __future__ import annotations

import pytest

pytest.importorskip("httpx")

from database.engine import session_scope  # noqa: E402
from database.models import Tournament, TournamentStatus  # noqa: E402
from tests.test_miniapp_v2 import ADMIN_TG, client, h  # noqa: E402,F401
from tests.test_tournament_engine import _setup  # noqa: E402


async def test_public_board_without_auth(client):
    tid, _ = await _setup(4)
    empty = await client.get(f"/api/v2/public/tournaments/{tid}")
    assert empty.status_code == 200 and empty.json()["engine"] is None
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "single_elim", "bo_default": 3, "bo_final": 3})).status_code == 200
    r = await client.get(f"/api/v2/public/tournaments/{tid}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["tournament"]["name"] == "Engine Cup" and d["stream"].startswith("https://www.twitch.tv/")
    assert d["engine"]["brackets"] and len(d["schedule"]) == 3
    body = r.text
    assert "telegram" not in body.lower() and "captain" not in body.lower()


async def test_public_board_hides_drafts_and_missing(client):
    tid, _ = await _setup(2)
    async with session_scope() as s:
        (await s.get(Tournament, tid)).status = TournamentStatus.DRAFT
        await s.commit()
    assert (await client.get(f"/api/v2/public/tournaments/{tid}")).status_code == 404
    assert (await client.get("/api/v2/public/tournaments/999999")).status_code == 404


async def test_board_page_is_served(client):
    r = await client.get("/v2/static/board.html")
    assert r.status_code == 200 and "/api/v2/public/tournaments/" in r.text
