"""5.15: записи трансляций — список, главы по матчам, правка и удаление админом."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("httpx")

from sqlalchemy import select  # noqa: E402

from database.engine import session_scope  # noqa: E402
from database.models import TournamentMatch  # noqa: E402
from tests.test_miniapp_v2 import ADMIN_TG, client, h  # noqa: E402,F401
from tests.test_tournament_engine import _setup  # noqa: E402

USER_TG = 700_123
START = datetime(2026, 10, 23, 14, 49, 45, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def rec_dir(tmp_path, monkeypatch):
    d = tmp_path / "rec"
    d.mkdir()
    monkeypatch.setenv("RECORDINGS_DIR", str(d))
    return d


def _put(d, rid: str, **meta):
    (d / f"{rid}.mp4").write_bytes(b"\x00" * 4096)
    (d / f"{rid}.jpg").write_bytes(b"\xff\xd8\xff")
    base = {"title": "Турнир — вечер", "recorded_at": START.isoformat(), "duration": 5520.0, "size": 4096, "height": 1080}
    base.update(meta)
    (d / f"{rid}.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")


async def test_records_list_and_chapters(client, rec_dir):
    tid, _ = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "single_elim", "bo_default": 3, "bo_final": 3})).status_code == 200
    async with session_scope() as s:
        m = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid, TournamentMatch.team1_id.is_not(None))
                             .order_by(TournamentMatch.match_no))).scalars().first()
        m.started_at = START + timedelta(minutes=12, seconds=30)
        m.finished_at = START + timedelta(minutes=50)
        await s.commit()
    _put(rec_dir, "20261023-174945")
    # Без описания запись не видна (загрузка ещё не закончена)
    (rec_dir / "partial.mp4").write_bytes(b"\x00")

    r = await client.get("/api/v2/records", headers=h(USER_TG))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [x["id"] for x in items] == ["20261023-174945"]
    it = items[0]
    assert it["video_url"] == "/rec/20261023-174945.mp4" and it["poster_url"] == "/rec/20261023-174945.jpg"
    assert it["tournament"]["id"] == tid and it["duration_text"] == "1:32:00" and it["chapters_count"] == 1
    assert "disk" not in r.json()

    d = (await client.get("/api/v2/records/20261023-174945", headers=h(USER_TG))).json()
    assert d["chapters"][0]["t"] == 750 and d["chapters"][0]["at"] == "0:12:30" and not d["chapters"][0]["approx"]
    assert "chapters_text" not in d

    me = (await client.get("/api/v2/me", headers=h(USER_TG))).json()
    assert me["stream"]["records"] == 1

    # Файлы отдаются с перемоткой (Range)
    v = await client.get("/rec/20261023-174945.mp4", headers={"Range": "bytes=100-199"})
    assert v.status_code == 206 and len(v.content) == 100
    # Описание и недокачанное наружу не отдаются
    assert (await client.get("/rec/20261023-174945.json")).status_code == 404
    assert (await client.get("/rec/partial.mp4.part")).status_code == 404


async def test_records_admin_edit_hide_delete(client, rec_dir):
    _put(rec_dir, "rec1")
    assert (await client.post("/api/v2/admin/records/rec1", headers=h(USER_TG), json={"title": "x"})).status_code == 403

    r = await client.post("/api/v2/admin/records/rec1", headers=h(ADMIN_TG),
                          json={"title": "Финал", "chapters": "0:05:00 Матч 1\n1:02:03 Финал\n"})
    assert r.status_code == 200, r.text
    d = (await client.get("/api/v2/records/rec1", headers=h(ADMIN_TG))).json()
    assert d["title"] == "Финал" and [c["t"] for c in d["chapters"]] == [300, 3723] and d["chapters_custom"]
    assert (await client.post("/api/v2/admin/records/rec1", headers=h(ADMIN_TG), json={"chapters": "bad line"})).status_code == 400

    assert (await client.post("/api/v2/admin/records/rec1", headers=h(ADMIN_TG), json={"hidden": True})).status_code == 200
    assert (await client.get("/api/v2/records", headers=h(USER_TG))).json()["items"] == []
    assert (await client.get("/api/v2/records/rec1", headers=h(USER_TG))).status_code == 404
    adm = (await client.get("/api/v2/records", headers=h(ADMIN_TG))).json()
    assert adm["items"][0]["hidden"] is True and "disk" in adm

    assert (await client.request("DELETE", "/api/v2/admin/records/rec1", headers=h(ADMIN_TG), json={})).status_code == 400
    assert (await client.request("DELETE", "/api/v2/admin/records/rec1", headers=h(ADMIN_TG), json={"confirm": True})).status_code == 200
    assert not any(rec_dir.iterdir())


async def test_records_reject_bad_ids(client, rec_dir):
    _put(rec_dir, "ok")
    for bad in ("..%2Fok", "a.b", "x" * 80):
        assert (await client.get(f"/api/v2/records/{bad}", headers=h(USER_TG))).status_code == 404
