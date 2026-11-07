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


def _png(w: int = 1200, h: int = 400) -> bytes:
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (w, h), (200, 150, 0)).save(out, format="PNG")
    return out.getvalue()


async def test_background_image(client):
    # Без фона — image нет, картинки по адресу нет
    mvp_banner.set_manual("Витёк", "начальник")
    assert (await mvp_banner.current())["image"] is None
    assert (await client.get("/api/v2/mvp-banner/image")).status_code == 404
    # Не админ — 403; не картинка — 400
    files = {"file": ("bg.png", _png(), "image/png")}
    assert (await client.post("/api/v2/admin/mvp-banner/image", headers=h(USER_TG), files=files)).status_code == 403
    bad = {"file": ("bg.png", b"not an image", "image/png")}
    assert (await client.post("/api/v2/admin/mvp-banner/image", headers=h(ADMIN_TG), files=bad)).status_code == 400
    # Админ загружает: в /me появляется адрес, по нему отдаётся JPEG не шире 1600
    r = await client.post("/api/v2/admin/mvp-banner/image", headers=h(ADMIN_TG), files={"file": ("bg.png", _png(2400, 800), "image/png")})
    assert r.status_code == 200, r.text
    url = (await client.get("/api/v2/me", headers=h(USER_TG))).json()["mvp"]["image"]
    assert url.startswith("/api/v2/mvp-banner/image?v=")
    img = await client.get(url)
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    from io import BytesIO

    from PIL import Image

    assert Image.open(BytesIO(img.content)).size == (1600, 533)
    # Убрать фон
    assert (await client.delete("/api/v2/admin/mvp-banner/image", headers=h(ADMIN_TG))).status_code == 200
    assert (await mvp_banner.current())["image"] is None
