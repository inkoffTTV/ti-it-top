"""5.12: после регистрации все без соперника встают друг против друга, BYE — только лишней команде."""
from __future__ import annotations

import random

import pytest
from sqlalchemy import select

from database.engine import session_scope
from database.models import Tournament, TournamentMatch, TournamentStatus
from services.tournament_engine import seeding
from services.tournament_engine import service as engine
from services.tournament_engine.formats import DoubleElimination, SingleElimination
from services.tournament_engine.plan import collapse, order_matches
from tests.test_tournament_engine import _setup, _simulate

pytest.importorskip("httpx")

from tests.test_miniapp_v2 import ADMIN_TG, client, h  # noqa: E402,F401

SE, DE = SingleElimination(), DoubleElimination()


def _everyone_plan(fmt, n):
    real, bye = seeding.everyone_pairs(list(range(1, n + 1)))
    slots, _ = seeding.pairs_slots(seeding.layout_pairs(real, bye), allow_empty=True)
    return order_matches(collapse(fmt.build(slots, fmt.normalize_settings({"bo_default": 1, "bo_final": 1})))), bye


@pytest.mark.parametrize("n", list(range(2, 41)))
def test_everyone_plays_first_round_and_bracket_completes(n):
    specs, bye = _everyone_plan(SE, n)
    assert len(bye) == n % 2
    first = [s for s in specs if s.src1.kind == "team" and s.src2.kind == "team"]
    assert len(first) == n // 2  # в первом раунде играют все, кроме одной лишней команды
    assert len(specs) == n - 1
    rng = random.Random(n)
    for _ in range(10):
        champion, losses, _ = _simulate(specs, rng)
        assert champion not in losses and sorted(losses) == sorted(t for t in range(1, n + 1) if t != champion)
    if n >= 3:
        de_specs, _ = _everyone_plan(DE, n)
        champion, losses, _ = _simulate(de_specs, random.Random(n))
        assert set(losses) | {champion} == set(range(1, n + 1))


def test_layout_never_gives_double_bye():
    for r in range(1, 20):
        for b in (0, 1):
            pairs = seeding.layout_pairs([[i, 100 + i] for i in range(r)], [999] if b else [])
            assert len(pairs) & (len(pairs) - 1) == 0
            for i in range(0, len(pairs), 2):
                group = pairs[i:i + 2]
                assert sum(1 for p in group if p[1] is None) <= 1  # BYE/пустая пара всегда рядом с реальной


async def _r1_teams(tid):
    async with session_scope() as s:
        rows = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid))).scalars().all()
    rev = engine._reverse_links(rows)
    r1 = [m for m in rows if (m.id, 1) not in rev and (m.id, 2) not in rev]
    half = [m for m in rows if ((m.id, 1) in rev) != ((m.id, 2) in rev)]
    byes = [m.team1_id if (m.id, 1) not in rev else m.team2_id for m in half]
    return r1, [b for b in byes if b], rows


async def test_close_registration_pairs_everyone_keeps_time(client):
    tid, teams = await _setup(6)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "double_elim", "bo_default": 3, "bo_final": 1, "seeding": "standard"})).status_code == 200
    r1, byes, _ = await _r1_teams(tid)
    assert len(r1) == 2 and len(byes) == 2  # стандартный посев: 2 BYE сильнейшим
    kept = r1[0]
    kept_pair = {kept.team1_id, kept.team2_id}
    async with session_scope() as s:
        await engine.set_schedule(s, kept.id, "2026-10-03T18:30", force=True)
        await s.commit()
    # Админ убрал команду из второго матча: там осталась одна, ещё одна — вне сетки.
    lone, gone = r1[1].team1_id, r1[1].team2_id
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/pairs", headers=h(ADMIN_TG),
                              json={"pairs": [{"match_id": r1[1].id, "team2_id": None}]})).status_code == 200
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/status", headers=h(ADMIN_TG), json={"status": "ACTIVE"})
    assert r.status_code == 200, r.text
    ap = r.json()["autopair"]
    assert ap["action"] == "rebuilt" and len(ap["pairs"]) == 2 and ap["byes"] == [], ap
    r1, byes, rows = await _r1_teams(tid)
    played = sorted(t for m in r1 for t in (m.team1_id, m.team2_id) if t)
    assert played == sorted(teams) and byes == []  # 6 команд — 3 пары, автопроходов в 1-м раунде нет
    again = next(m for m in rows if {m.team1_id, m.team2_id} == kept_pair)
    assert again.scheduled_at is not None and again.status == "SCHEDULED" and again.best_of == 3
    assert all(m.team1_id and m.team2_id for m in r1) and {lone, gone} <= set(played)
    # Повторный добор ничего не меняет.
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/engine/autopair", headers=h(ADMIN_TG), json={})
    assert r.json()["action"] == "none"


async def test_odd_leftover_gets_bye_and_dry_run_changes_nothing(client):
    tid, teams = await _setup(7)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "single_elim", "bo_default": 1, "bo_final": 1, "seeding": "standard"})).status_code == 200
    r1, byes, _ = await _r1_teams(tid)
    assert len(r1) == 3 and len(byes) == 1
    url = f"/api/v2/admin/tournaments/{tid}/engine/autopair"
    assert (await client.post(url, headers=h(ADMIN_TG), json={})).json()["action"] == "none"  # одна лишняя на BYE — норма
    gap = r1[0]
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/pairs", headers=h(ADMIN_TG),
                              json={"pairs": [{"match_id": gap.id, "team2_id": None}]})).status_code == 200
    plan = (await client.post(url, headers=h(ADMIN_TG), json={"dry_run": True})).json()
    assert plan["action"] == "rebuilt" and len(plan["pairs"]) == 1 and len(plan["byes"]) == 1, plan
    async with session_scope() as s:
        assert (await s.get(TournamentMatch, gap.id)).team2_id is None  # предпросмотр ничего не записал
    done = (await client.post(url, headers=h(ADMIN_TG), json={})).json()
    assert done["action"] == "rebuilt"
    r1, byes, _ = await _r1_teams(tid)
    assert len(r1) == 3 and all(m.team1_id and m.team2_id for m in r1) and len(byes) == 1
    assert sorted([t for m in r1 for t in (m.team1_id, m.team2_id)] + byes) == sorted(teams)


async def test_locked_bracket_only_fills_free_slots(client):
    tid, teams = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "single_elim", "bo_default": 1, "bo_final": 1, "seeding": "standard"})).status_code == 200
    r1, _, _ = await _r1_teams(tid)
    m = r1[0]
    out_team = m.team2_id
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/pairs", headers=h(ADMIN_TG),
                              json={"pairs": [{"match_id": m.id, "team2_id": None}]})).status_code == 200
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/lock", headers=h(ADMIN_TG))).status_code == 200
    done = (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/autopair", headers=h(ADMIN_TG), json={})).json()
    assert done["action"] == "filled", done
    async with session_scope() as s:
        again = await s.get(TournamentMatch, m.id)
        assert again.team2_id == out_team


async def test_manual_round_robin_and_no_bracket(client):
    tid, teams = await _setup(4)
    url = f"/api/v2/admin/tournaments/{tid}/engine/autopair"
    assert (await client.post(url, headers=h(ADMIN_TG), json={})).json()["action"] == "skipped"
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "manual", "bo_default": 1})).status_code == 200
    done = (await client.post(url, headers=h(ADMIN_TG), json={})).json()
    assert done["action"] == "filled" and len(done["pairs"]) == 2
    tid2, _ = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid2}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "round_robin", "bo_default": 1})).status_code == 200
    assert (await client.post(f"/api/v2/admin/tournaments/{tid2}/engine/autopair", headers=h(ADMIN_TG), json={})).json()["action"] == "none"


async def test_status_change_outside_registration_does_not_autopair(client):
    tid, _ = await _setup(4)
    async with session_scope() as s:
        (await s.get(Tournament, tid)).status = TournamentStatus.ACTIVE
        await s.commit()
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/status", headers=h(ADMIN_TG), json={"status": "FINISHED"})
    assert r.status_code == 200 and "autopair" not in r.json()


async def test_admins_reminded_to_close_registration_hour_before_start(client):
    from datetime import timedelta

    from services.tournament_engine import reminders, timeutil

    tid, _ = await _setup(4)
    tid_later, _ = await _setup(4)
    now = timeutil.now_utc()
    async with session_scope() as s:
        (await s.get(Tournament, tid)).start_date = now + timedelta(minutes=30)
        (await s.get(Tournament, tid_later)).start_date = now + timedelta(hours=2)
        await s.commit()
    texts = [x for x in await reminders.registration_close_check(now) if "Engine Cup" in x]
    assert len(texts) == 1 and "регистрация ещё открыта" in texts[0] and "Сейчас:" in texts[0]
    assert not [x for x in await reminders.registration_close_check(now) if "Engine Cup" in x]  # один раз


async def test_everyone_seeding_via_api(client):
    tid, teams = await _setup(7)
    r = await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                          json={"format": "double_elim", "bo_default": 1, "bo_final": 1, "seeding": "everyone"})
    assert r.status_code == 200, r.text
    r1, byes, _ = await _r1_teams(tid)
    assert len(r1) == 3 and len(byes) == 1
