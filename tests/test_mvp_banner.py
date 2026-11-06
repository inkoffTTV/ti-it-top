"""Плашка «MVP по выбору игроков» на главной: ручная от админа, иначе победитель голосования; /me и админ-API."""
from __future__ import annotations

import pytest

pytest.importorskip("httpx")

from services import mvp_banner  # noqa: E402
from tests.test_miniapp_v2 import ADMIN_TG, client, h  # noqa: E402,F401

USER_TG = 700_456


@pytest.fixture(autouse=True)
def banner_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mvp_banner, "PATH", tmp_path / "mvp_banner.json")


async def test_manual_banner_and_welcome_line():
    assert mvp_banner.manual() is None and await mvp_banner.current() is None
    assert mvp_banner.welcome_line(None) == ""
    b = mvp_banner.set_manual("  Витёк ", "босс, легенда, начальник")
    assert b == {"name": "Витёк", "caption": "босс, легенда, начальник"}
    cur = await mvp_banner.current()
    assert cur["source"] == "manual" and cur["name"] == "Витёк"
    line = mvp_banner.welcome_line(cur)
    assert "MVP матчей по выбору игроков" in line and "Витёк" in line and "начальник" in line
    # HTML экранируется
    assert "&lt;b&gt;" in mvp_banner.welcome_line({"name": "<b>x</b>", "caption": ""})
    with pytest.raises(ValueError):
        mvp_banner.set_manual("   ")
    with pytest.raises(ValueError):
        mvp_banner.set_manual("x" * 41)
    mvp_banner.clear()
    assert mvp_banner.manual() is None


async def test_admin_api_and_me(client):
    # Не админ — 403
    assert (await client.post("/api/v2/admin/mvp-banner", headers=h(USER_TG), json={"name": "Я"})).status_code == 403
    r = await client.post("/api/v2/admin/mvp-banner", headers=h(ADMIN_TG), json={"name": "Витёк", "caption": "босс, легенда, начальник"})
    assert r.status_code == 200, r.text
    assert r.json()["current"]["name"] == "Витёк"
    me = (await client.get("/api/v2/me", headers=h(USER_TG))).json()
    assert me["mvp"]["name"] == "Витёк" and me["mvp"]["caption"] == "босс, легенда, начальник"
    assert (await client.post("/api/v2/admin/mvp-banner", headers=h(ADMIN_TG), json={"name": ""})).status_code == 400
    r = await client.delete("/api/v2/admin/mvp-banner", headers=h(ADMIN_TG))
    assert r.status_code == 200 and r.json()["manual"] is None
    assert (await client.get("/api/v2/me", headers=h(USER_TG))).json()["mvp"] is None
