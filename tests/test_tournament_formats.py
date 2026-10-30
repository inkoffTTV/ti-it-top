"""5.4: круговая система, группы (круговые и GSL) → плей-офф, швейцарка."""
from __future__ import annotations

import itertools
import random

import pytest
from sqlalchemy import select

from database.engine import session_scope
from database.models import Tournament, TournamentMatch
from services.tournament_engine import service as engine
from services.tournament_engine.formats import GSLGroups, Swiss, circle_rounds, snake_groups
from services.tournament_engine.plan import collapse, order_matches
from services.tournament_engine.series import SeriesError, validate_score
from tests.test_tournament_engine import _setup


# ---------------------------------------------------------------------------
# Чистая логика
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", range(3, 13))
def test_circle_method_every_pair_once(n):
    rounds = circle_rounds(list(range(1, n + 1)))
    assert len(rounds) == (n - 1 if n % 2 == 0 else n)
    pairs = [frozenset(p) for r in rounds for p in r]
    assert len(pairs) == len(set(pairs)) == n * (n - 1) // 2
    for r in rounds:
        flat = [t for p in r for t in p]
        assert len(flat) == len(set(flat))  # никто не играет дважды за тур


def test_snake_groups_balanced():
    g = snake_groups(list(range(1, 9)), 2)
    assert g == {"A": [1, 4, 5, 8], "B": [2, 3, 6, 7]}
    assert sorted(len(v) for v in snake_groups(list(range(1, 11)), 3).values()) == [3, 3, 4]


def test_bo2_draw_only_where_allowed():
    assert validate_score(2, 1, 1, allow_draw=True) == 0
    assert validate_score(2, 2, 0, allow_draw=True) == 1
    with pytest.raises(SeriesError):
        validate_score(2, 1, 1, allow_draw=False)
    with pytest.raises(SeriesError):
        validate_score(2, 2, 1, allow_draw=True)


def test_gsl_group_of_four_and_three():
    fmt = GSLGroups()
    s = fmt.normalize_settings({"bo_default": 1, "playoff_format": "single_elim"})
    specs, meta, extra = fmt.build_table([1, 2, 3, 4, 5, 6, 7], s)
    real = order_matches(collapse(specs))
    assert extra["groups"] == 2 and len(real) == 5 + 3  # группа из 3 — без фиктивного стартового матча
    assert all(sp.src1 and sp.src2 for sp in real)


# ---------------------------------------------------------------------------
# На БД
# ---------------------------------------------------------------------------

async def _gen(tid, fmt, **settings):
    async with session_scope() as s:
        t = await s.get(Tournament, tid)
        await engine.generate(s, t, fmt_key=fmt, settings=settings)
        await s.commit()


async def _open(tid):
    async with session_scope() as s:
        return list((await s.execute(
            select(TournamentMatch).where(TournamentMatch.tournament_id == tid, TournamentMatch.status != "FINISHED",
                                          TournamentMatch.team1_id.is_not(None), TournamentMatch.team2_id.is_not(None))
            .order_by(TournamentMatch.match_no)
        )).scalars().all())


async def _play_all(tid, rng, draw_ok=False, limit=500):
    for _ in range(limit):
        ready = await _open(tid)
        if not ready:
            return
        m = ready[0]
        need = m.best_of // 2 + 1
        if m.best_of == 2 and draw_ok and rng.random() < 0.3:
            s1, s2 = 1, 1
        elif rng.random() < 0.5:
            s1, s2 = need, rng.randint(0, need - 1) if m.best_of != 2 else 0
        else:
            s1, s2 = (rng.randint(0, need - 1) if m.best_of != 2 else 0), need
        async with session_scope() as s:
            await engine.set_result(s, m.id, score1=s1, score2=s2)
            await s.commit()
    raise AssertionError("турнир не завершился")


async def _view(tid):
    async with session_scope() as s:
        return await engine.stage_view(s, await s.get(Tournament, tid))


async def test_round_robin_bo2_draws_and_champion(db):
    tid, teams = await _setup(5)
    await _gen(tid, "round_robin", bo_default=2)
    v = await _view(tid)
    assert v["kind"] == "table" and v["total"] == 10 and v["allow_draw"]
    await _play_all(tid, random.Random(5), draw_ok=True)
    v = await _view(tid)
    rows = v["standings"][0]["rows"]
    assert v["status"] == "FINISHED" and v["champion"]["id"] == rows[0]["team_id"]
    assert all(r["played"] == 4 for r in rows)
    assert sum(r["wins"] for r in rows) == sum(r["losses"] for r in rows)
    pts = [r["points"] for r in rows]
    assert pts == sorted(pts, reverse=True)


async def test_head_to_head_breaks_equal_points(db):
    tid, teams = await _setup(4)
    await _gen(tid, "round_robin", bo_default=1)
    ms = {frozenset((m.team1_id, m.team2_id)): m for m in await _open(tid)}
    # teams[3] — лучший посев (выше MMR), teams[0] — худший. A и B набирают по 6 очков,
    # разница карт равна; A выиграл личную встречу — он выше, несмотря на худший посев.
    a, c, d, b = teams
    for (w, lo) in ((a, b), (a, c), (b, c), (b, d), (c, d), (d, a)):
        m = ms[frozenset((w, lo))]
        async with session_scope() as s:
            await engine.set_result(s, m.id, score1=1 if m.team1_id == w else 0, score2=1 if m.team2_id == w else 0)
            await s.commit()
    rows = (await _view(tid))["standings"][0]["rows"]
    assert [r["team_id"] for r in rows] == [a, b, c, d]
    assert [r["points"] for r in rows] == [6, 6, 3, 3]


async def test_groups_rr_to_single_elim_playoffs(db):
    tid, teams = await _setup(8)
    await _gen(tid, "groups_rr", bo_default=1, groups=2, advance=2, playoff_format="single_elim", playoff_bo_default=3, playoff_bo_final=3)
    v = await _view(tid)
    assert v["total"] == 12 and [g["title"] for g in v["standings"]] == ["Группа A", "Группа B"]
    async with session_scope() as s:
        with pytest.raises(engine.EngineError):
            await engine.create_playoffs(s, await s.get(Tournament, tid))  # группы не сыграны
    await _play_all(tid, random.Random(8))
    v = await _view(tid)
    assert v["playoffs_ready"]
    async with session_scope() as s:
        await engine.create_playoffs(s, await s.get(Tournament, tid))
        await s.commit()
    v = await _view(tid)
    assert len(v["all_stages"]) == 2 and v["format"] == "single_elim" and v["all_stages"][0]["playoffs_created"]
    groups = {r["team_id"]: g["group"] for g in v["all_stages"][0]["standings"] for r in g["rows"]}
    semis = [m for b in v["brackets"] for r in b["rounds"] if r["round_no"] == 1 for m in r["matches"]]
    assert len(semis) == 2 and all(groups[m["team1"]["id"]] != groups[m["team2"]["id"]] for m in semis)  # A1–B2, B1–A2
    assert all(m["best_of"] == 3 for m in semis)
    await _play_all(tid, random.Random(9))
    v = await _view(tid)
    assert v["status"] == "FINISHED" and v["champion"]
    async with session_scope() as s:
        with pytest.raises(engine.EngineError):
            await engine.generate(s, await s.get(Tournament, tid), fmt_key="groups_rr", settings={})
        await engine.reset_stage(s, await s.get(Tournament, tid), force=True)
        await s.commit()
    assert await _view(tid) is None


async def test_gsl_groups_places_by_record_then_double_elim(db):
    tid, teams = await _setup(8)
    await _gen(tid, "groups_gsl", bo_default=1, playoff_format="double_elim")
    assert (await _view(tid))["total"] == 10
    await _play_all(tid, random.Random(3))
    v = await _view(tid)
    for grp in v["standings"]:
        records = [(r["wins"], r["losses"]) for r in grp["rows"]]
        assert records == [(2, 0), (2, 1), (1, 2), (0, 2)]
    async with session_scope() as s:
        await engine.create_playoffs(s, await s.get(Tournament, tid))
        await s.commit()
    v = await _view(tid)
    assert v["format"] == "double_elim" and v["total"] == 6  # 4 команды: 2n-2


@pytest.mark.parametrize("n,seed", [(8, 1), (16, 2), (7, 3), (12, 4)])
async def test_swiss_runs_to_completion_without_rematches(db, n, seed):
    tid, teams = await _setup(n)
    await _gen(tid, "swiss", bo_default=1, playoff_format="single_elim" if n in (8, 16) else "none")
    await _play_all(tid, random.Random(seed))
    v = await _view(tid)
    rows = v["standings"][0]["rows"]
    need = Swiss.default_threshold(n)
    assert v["status"] == "FINISHED"
    assert all(r["status"] in ("advanced", "eliminated") for r in rows)
    assert all(r["wins"] == need or r["losses"] == need for r in rows)
    async with session_scope() as s:
        ms = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid))).scalars().all()
    pairs = [frozenset((m.team1_id, m.team2_id)) for m in ms]
    assert len(pairs) == len(set(pairs)), "повторная встреча в швейцарке"
    if n in (8, 16):
        advanced = [r for r in rows if r["status"] == "advanced"]
        assert len(advanced) == n // 2
        assert v["playoffs_ready"]


async def test_swiss_correction_rebuilds_next_round(db):
    tid, teams = await _setup(8)
    await _gen(tid, "swiss", bo_default=1)
    first = await _open(tid)
    for m in first:
        async with session_scope() as s:
            await engine.set_result(s, m.id, score1=1, score2=0)
            await s.commit()
    round2 = await _open(tid)
    assert len(round2) == 4 and all(m.round_no == 2 for m in round2)
    async with session_scope() as s:
        with pytest.raises(engine.NeedConfirm):
            await engine.set_result(s, first[0].id, score1=0, score2=1)
    async with session_scope() as s:
        await engine.set_result(s, first[0].id, score1=0, score2=1, cascade=True)
        await s.commit()
    round2b = await _open(tid)
    assert len(round2b) == 4 and all(m.round_no == 2 for m in round2b)
    from database.models import TournamentMatchLog

    async with session_scope() as s:
        rebuilt = (await s.execute(select(TournamentMatchLog).where(TournamentMatchLog.action == "swiss_round"))).scalars().all()
    assert len(rebuilt) == 2  # второй тур собран заново по исправленному результату
    # Если в следующем туре уже есть результат — правка запрещена.
    async with session_scope() as s:
        await engine.set_result(s, round2b[0].id, score1=1, score2=0)
        await s.commit()
    async with session_scope() as s:
        with pytest.raises(engine.EngineError):
            await engine.set_result(s, first[1].id, score1=0, score2=1, cascade=True)


async def test_all_formats_generate_for_many_team_counts(db):
    for n, fmt, settings in itertools.chain(
        ((n, "round_robin", {"bo_default": 1}) for n in (3, 4, 7)),
        ((n, "groups_rr", {"groups": 2, "advance": 1}) for n in (4, 6, 9)),
        ((n, "groups_gsl", {}) for n in (3, 4, 6, 8, 12)),
        ((n, "swiss", {}) for n in (4, 5, 9)),
    ):
        tid, _ = await _setup(n)
        await _gen(tid, fmt, **settings)
        v = await _view(tid)
        assert v["total"] > 0, (n, fmt)
