"""5.11: расстановка пар вручную — все «ручные» места этапа на одном экране, сохранение разом."""
from __future__ import annotations

import pytest

pytest.importorskip("httpx")

from tests.test_miniapp_v2 import ADMIN_TG, client, h  # noqa: E402,F401
from tests.test_tournament_engine import _setup  # noqa: E402


def _url(tid):
    return f"/api/v2/admin/tournaments/{tid}/engine/pairs"


async def _pairs(client, tid):
    r = await client.get(_url(tid), headers=h(ADMIN_TG))
    assert r.status_code == 200, r.text
    return r.json()


def _slot(m, editable=True):
    return next(s for s in m["slots"] if s["editable"] is editable)


async def test_pairs_swap_duplicates_and_fed_slots(client):
    tid, teams = await _setup(6)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "double_elim", "bo_default": 1, "bo_final": 1, "seeding": "standard"})).status_code == 200
    assert (await client.get(_url(tid), headers=h(9501))).status_code in (401, 403)
    d = await _pairs(client, tid)
    assert d["table"] is False and d["busy"] == []
    full = [m for m in d["matches"] if all(s["editable"] for s in m["slots"])]
    half = [m for m in d["matches"] if sum(s["editable"] for s in m["slots"]) == 1]
    assert len(full) == 2 and len(half) == 2  # 1-й раунд + две команды со «свободным проходом» во 2-м
    placed = [s["team_id"] for m in d["matches"] for s in m["slots"] if s["editable"]]
    assert sorted(placed) == sorted(teams)
    assert _slot(half[0], False)["source"].startswith("Победитель матча")

    # Обмен местами одним сохранением: команда из 1-го раунда <-> команда со свободным проходом.
    a, hs = full[0]["slots"][0]["team_id"], _slot(half[0])
    r = await client.post(_url(tid), headers=h(ADMIN_TG), json={"pairs": [
        {"match_id": full[0]["id"], "team1_id": hs["team_id"]},
        {"match_id": half[0]["id"], f"team{hs['slot']}_id": a},
    ]})
    assert r.status_code == 200 and r.json()["changed"] == 2, r.text
    d2 = await _pairs(client, tid)
    m1 = next(m for m in d2["matches"] if m["id"] == full[0]["id"])
    mh = next(m for m in d2["matches"] if m["id"] == half[0]["id"])
    assert m1["slots"][0]["team_id"] == hs["team_id"] and _slot(mh)["team_id"] == a

    # Одна команда в двух матчах — отказ, ничего не меняется.
    dup = full[0]["slots"][1]["team_id"]
    r = await client.post(_url(tid), headers=h(ADMIN_TG), json={"pairs": [{"match_id": full[1]["id"], "team1_id": dup}]})
    assert r.status_code == 400 and "сразу в матчах" in r.text
    # Место «Победитель матча N» вручную не задаётся.
    fed = _slot(half[0], False)
    r = await client.post(_url(tid), headers=h(ADMIN_TG), json={"pairs": [{"match_id": half[0]["id"], f"team{fed['slot']}_id": dup}]})
    assert r.status_code == 400 and "займёт" in r.text
    # Пустое сохранение и чужой матч.
    assert (await client.post(_url(tid), headers=h(ADMIN_TG), json={"pairs": []})).status_code == 400
    assert (await client.post(_url(tid), headers=h(ADMIN_TG), json={"pairs": [{"match_id": 999999, "team1_id": dup}]})).status_code == 400

    # Сыграли матч 1-го раунда: он пропадает из списка, его команды заняты.
    assert (await client.post(f"/api/v2/admin/matches/{full[0]['id']}/result", headers=h(ADMIN_TG),
                              json={"score1": 1, "score2": 0})).status_code == 200
    d3 = await _pairs(client, tid)
    assert full[0]["id"] not in [m["id"] for m in d3["matches"]]
    loser = full[0]["slots"][1]["team_id"]
    assert loser in d3["busy"]
    r = await client.post(_url(tid), headers=h(ADMIN_TG), json={"pairs": [{"match_id": full[1]["id"], "team1_id": loser}]})
    assert r.status_code == 400
    # Обмен двух команд со свободным проходом — победитель уже стоит в соседнем месте, ложной ошибки нет.
    x, y = (next(m for m in d3["matches"] if m["id"] == hm["id"]) for hm in half)
    sx, sy = _slot(x), _slot(y)
    r = await client.post(_url(tid), headers=h(ADMIN_TG), json={"pairs": [
        {"match_id": x["id"], f"team{sx['slot']}_id": sy["team_id"]},
        {"match_id": y["id"], f"team{sy['slot']}_id": sx["team_id"]},
    ]})
    assert r.status_code == 200 and r.json()["changed"] == 2, r.text


async def test_pairs_round_robin_scope_is_one_round(client):
    tid, teams = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "round_robin", "bo_default": 1})).status_code == 200
    d = await _pairs(client, tid)
    assert d["table"] is True and len(d["matches"]) == 6
    by_scope = {}
    for m in d["matches"]:
        by_scope.setdefault(m["scope"], []).append(m)
    m1, m2 = next(v for v in by_scope.values() if len(v) == 2)
    b, c = m1["slots"][1]["team_id"], m2["slots"][0]["team_id"]
    r = await client.post(_url(tid), headers=h(ADMIN_TG), json={"pairs": [
        {"match_id": m1["id"], "team2_id": c}, {"match_id": m2["id"], "team1_id": b}]})
    assert r.status_code == 200 and r.json()["changed"] == 2, r.text
    # В одном туре команда не может стоять дважды.
    r = await client.post(_url(tid), headers=h(ADMIN_TG), json={"pairs": [{"match_id": m1["id"], "team1_id": b}]})
    assert r.status_code == 400
    # Одиночная замена в матче больше не упирается в то, что команда играет в других турах.
    assert (await client.post(f"/api/v2/admin/matches/{m2['id']}/teams", headers=h(ADMIN_TG),
                              json={"team1_id": None, "team2_id": m2["slots"][1]["team_id"]})).status_code == 200
    r = await client.post(f"/api/v2/admin/matches/{m1['id']}/teams", headers=h(ADMIN_TG),
                          json={"team1_id": m1["slots"][0]["team_id"], "team2_id": b, "force": True})
    assert r.status_code == 200, r.text
