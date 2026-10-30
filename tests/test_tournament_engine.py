"""Тесты турнирного движка 5.2: чистая логика (без БД) и операции сервиса на временной SQLite."""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from database.engine import session_scope
from database.models import (
    ApplicationStatus,
    PlayerProfile,
    Team,
    TeamMember,
    Tournament,
    TournamentApplication,
    TournamentMatch,
    TournamentStatus,
    User,
    VerificationStatus,
)
from services.tournament_engine import seeding, timeutil
from services.tournament_engine.formats import FORMATS, DoubleElimination, SingleElimination
from services.tournament_engine.plan import collapse, order_matches
from services.tournament_engine.series import SeriesError, forfeit_score, validate_score
from services.tournament_engine import service as engine

SE = SingleElimination()
DE = DoubleElimination()
SETTINGS = {"bo_default": 3, "bo_final": 1}


# ---------------------------------------------------------------------------
# Чистая логика
# ---------------------------------------------------------------------------

def test_series_rules():
    assert validate_score(3, 2, 0) == 1
    assert validate_score(3, 1, 2) == 2
    assert validate_score(1, 0, 1) == 2
    assert validate_score(5, 3, 2) == 1
    for bo, s1, s2 in ((3, 1, 1), (3, 3, 0), (3, 2, 2), (1, 1, 1), (5, 2, 1), (3, -1, 2), (1, 2, 0)):
        with pytest.raises(SeriesError):
            validate_score(bo, s1, s2)
    assert forfeit_score(3, 2) == (0, 2)
    assert forfeit_score(1, 1) == (1, 0)


def test_moscow_time_input_and_display():
    utc = timeutil.parse_local("02.10.2026 17:50")
    assert utc == datetime(2026, 10, 2, 14, 50, tzinfo=timezone.utc)
    assert timeutil.parse_local("2026-10-02T17:50") == utc
    assert timeutil.parse_local("2026-10-02T14:50:00Z") == utc
    assert timeutil.parse_local("") is None
    assert timeutil.fmt_local(datetime(2026, 10, 2, 14, 50)) == "02.10.2026 17:50"
    assert timeutil.fmt_local(utc, label=True) == "02.10.2026 17:50 МСК"
    assert timeutil.input_value(utc) == "2026-10-02T17:50"
    with pytest.raises(ValueError):
        timeutil.parse_local("завтра вечером")


def _plan(fmt, n, settings=SETTINGS):
    slots, _ = seeding.standard_slots(list(range(1, n + 1)))
    return order_matches(collapse(fmt.build(slots, fmt.normalize_settings(settings))))


@pytest.mark.parametrize("n", list(range(2, 41)) + [64])
def test_single_elimination_counts_and_byes(n):
    specs = _plan(SE, n)
    assert len(specs) == n - 1
    size = seeding.next_power_of_two(n)
    byes = size - n
    first_round_teams = {s.src1.ref for s in specs if s.src1.kind == "team"} | {s.src2.ref for s in specs if s.src2.kind == "team"}
    # Нет «фиктивных» матчей: у каждого матча два реальных источника.
    assert all(s.src1 is not None and s.src2 is not None for s in specs)
    # BYE получают верхние посевы: они не играют в первом раунде.
    for seed in range(1, byes + 1):
        assert not any(s.round_no == 1 and seed in (s.src1.ref, s.src2.ref) for s in specs if s.src1.kind == s.src2.kind == "team")
    assert len(first_round_teams) == n


@pytest.mark.parametrize("n", list(range(3, 41)) + [64])
def test_double_elimination_counts(n):
    specs = _plan(DE, n)
    assert len(specs) == 2 * n - 2
    assert specs[-1].key == "GF"
    assert specs[-1].best_of == 1 and all(s.best_of == 3 for s in specs[:-1])


def _simulate(specs, rng):
    """Играет план до конца со случайными победителями; возвращает (чемпион, поражения, пары матчей)."""
    result: dict[str, tuple[int, int]] = {}
    losses: dict[int, int] = {}
    pairs: dict[str, tuple[int, int]] = {}

    def resolve(src):
        if src.kind == "team":
            return src.ref
        w, lo = result[src.ref]
        return w if src.kind == "winner" else lo

    for spec in specs:
        a, b = resolve(spec.src1), resolve(spec.src2)
        assert a is not None and b is not None and a != b
        w, lo = (a, b) if rng.random() < 0.5 else (b, a)
        result[spec.key] = (w, lo)
        pairs[spec.key] = (a, b)
        losses[lo] = losses.get(lo, 0) + 1
    last = specs[-1].key
    return result[last][0], losses, pairs


@pytest.mark.parametrize("n", [2, 3, 5, 6, 7, 8, 9, 12, 16, 17, 31, 32])
def test_single_elimination_simulation_one_champion(n):
    rng = random.Random(n)
    for _ in range(30):
        champion, losses, _ = _simulate(_plan(SE, n), rng)
        assert champion not in losses
        assert sorted(losses) == sorted(t for t in range(1, n + 1) if t != champion)
        assert all(v == 1 for v in losses.values())


@pytest.mark.parametrize("n", [3, 4, 5, 6, 7, 8, 9, 12, 16, 17, 24, 32])
def test_double_elimination_simulation(n):
    rng = random.Random(100 + n)
    for _ in range(30):
        specs = _plan(DE, n)
        champion, losses, _ = _simulate(specs, rng)
        # Все, кроме чемпиона, выбыли; выбывание — после двух поражений
        # (исключение — финалист верхней сетки, проигравший гранд-финал без перезапуска).
        assert set(losses) | {champion} == set(range(1, n + 1))
        assert losses.get(champion, 0) <= 1
        assert all(v in (1, 2) for v in losses.values())
        assert sum(1 for t, v in losses.items() if v == 1 and t != champion) <= 1


def test_double_elimination_8_no_rematch_after_first_drop():
    """Выбывший из 1/2 верхней сетки не встречает сразу команду, которую сам выбил в 1/4."""
    specs = _plan(DE, 8)
    rng = random.Random(7)
    for _ in range(200):
        _, _, pairs = _simulate(specs, rng)
        upper_first = {frozenset(pairs[k]) for k in pairs if k.startswith("U1-")}
        for key in ("L2-1", "L2-2"):
            assert frozenset(pairs[key]) not in upper_first


def test_opening_slots_reproduce_production_pairs():
    # Прод: открывающий матч 1–5, остальные по MMR: 23, 4, 16, 9, 25, 19.
    slots, seeds = seeding.opening_slots(1, 5, [23, 4, 16, 9, 25, 19])
    assert [slots[i:i + 2] for i in range(0, 8, 2)] == [[1, 5], [23, 19], [4, 25], [16, 9]]
    assert seeds[1] == 1 and seeds[5] == 2 and seeds[19] == 8


def test_opening_slots_byes_do_not_meet_each_other():
    slots, _ = seeding.opening_slots(1, 2, [3, 4, 5, 6])  # 6 команд: BYE у 3 и 4
    specs = order_matches(collapse(SE.build(slots, SE.normalize_settings(SETTINGS))))
    assert len(specs) == 5
    second = [s for s in specs if s.round_no == 2]
    assert not any({s.src1.ref, s.src2.ref} == {3, 4} for s in second if s.src1.kind == s.src2.kind == "team")


def test_format_registry_has_no_unknown():
    assert set(FORMATS) == {"single_elim", "double_elim", "round_robin", "groups_rr", "groups_gsl", "swiss", "manual"}
    with pytest.raises(ValueError):
        engine.get_format("swiss_unknown")


# ---------------------------------------------------------------------------
# Сервис на БД
# ---------------------------------------------------------------------------

_SETUP_SEQ = [0]


async def _setup(n: int, *, max_teams: int = 16, mmr: bool = True) -> tuple[int, list[int]]:
    _SETUP_SEQ[0] += 1
    base = 10_000 + _SETUP_SEQ[0] * 100_000
    async with session_scope() as s:
        t = Tournament(name="Engine Cup", max_teams=max_teams, status=TournamentStatus.REGISTRATION, registration_open=True,
                       start_date=datetime(2026, 10, 2, 14, 50, tzinfo=timezone.utc))
        s.add(t)
        await s.flush()
        team_ids = []
        for i in range(n):
            captain = User(telegram_id=base + i * 10, username=f"cap{base + i}", is_registered=True)
            s.add(captain)
            await s.flush()
            team = Team(name=f"Team {_SETUP_SEQ[0]}-{i + 1}", captain_user_id=captain.id)
            s.add(team)
            await s.flush()
            for j in range(5):
                u = captain if j == 0 else User(telegram_id=base + i * 10 + j, is_registered=True)
                if j:
                    s.add(u)
                    await s.flush()
                s.add(TeamMember(team_id=team.id, user_id=u.id))
                if mmr:
                    s.add(PlayerProfile(user_id=u.id, verification_status=VerificationStatus.VERIFIED,
                                        exact_mmr=2000 + i * 100, approved_mmr=2000 + i * 100, mmr_verified=True))
            s.add(TournamentApplication(tournament_id=t.id, team_id=team.id, created_by=captain.id, status=ApplicationStatus.APPROVED))
            team_ids.append(int(team.id))
        await s.commit()
        return int(t.id), team_ids


async def _matches(tid: int) -> dict[str, TournamentMatch]:
    async with session_scope() as s:
        rows = (await s.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == tid))).scalars().all()
        return {m.plan_key: m for m in rows}


async def _generate(tid: int, fmt: str = "double_elim", **kw):
    async with session_scope() as s:
        t = await s.get(Tournament, tid)
        stage = await engine.generate(s, t, fmt_key=fmt, settings=kw.pop("settings", SETTINGS), **kw)
        await s.commit()
        return stage


async def _result(match_id: int, s1: int, s2: int, **kw):
    async with session_scope() as s:
        out = await engine.set_result(s, match_id, score1=s1, score2=s2, **kw)
        await s.commit()
        return out


async def test_generate_double_elimination_and_play_to_champion(db):
    tid, teams = await _setup(8)
    await _generate(tid)
    ms = await _matches(tid)
    assert len(ms) == 14
    assert ms["GF"].best_of == 1 and ms["U1-1"].best_of == 3 and ms["L1-1"].best_of == 3
    assert all(ms[f"U1-{i}"].team1_id and ms[f"U1-{i}"].team2_id for i in range(1, 5))
    assert ms["U2-1"].team1_id is None and ms["U2-1"].status == "WAITING"
    rng = random.Random(3)
    for _ in range(20):
        ms = await _matches(tid)
        ready = [m for m in ms.values() if m.status != "FINISHED" and m.team1_id and m.team2_id]
        if not ready:
            break
        m = min(ready, key=lambda x: x.match_no)
        await _result(m.id, *((2, rng.randint(0, 1)) if m.best_of == 3 else (1, 0)))
    ms = await _matches(tid)
    assert all(m.status == "FINISHED" for m in ms.values())
    async with session_scope() as s:
        view = await engine.stage_view(s, await s.get(Tournament, tid))
    assert view["status"] == "FINISHED" and view["champion"]["id"] == ms["GF"].winner_team_id
    assert [b["key"] for b in view["brackets"]] == ["upper", "lower", "final"]


async def test_result_validation_and_advancement(db):
    tid, _ = await _setup(8)
    await _generate(tid)
    ms = await _matches(tid)
    with pytest.raises(engine.EngineError):
        await _result(ms["U1-1"].id, 1, 0)  # BO3 требует 2 победы
    with pytest.raises(engine.EngineError):
        await _result(ms["U2-1"].id, 2, 0)  # команды ещё не определены
    await _result(ms["U1-1"].id, 2, 1)
    ms2 = await _matches(tid)
    assert ms2["U2-1"].team1_id == ms["U1-1"].team1_id
    assert ms2["L1-1"].team1_id == ms["U1-1"].team2_id


async def test_correction_cascade_requires_confirmation(db):
    tid, _ = await _setup(8)
    await _generate(tid)
    ms = await _matches(tid)
    await _result(ms["U1-1"].id, 2, 0)
    await _result(ms["U1-2"].id, 2, 0)
    ms = await _matches(tid)
    await _result(ms["U2-1"].id, 2, 1)
    # Меняем победителя U1-1: U2-1 уже сыгран -> нужен явный каскад.
    with pytest.raises(engine.NeedConfirm) as exc:
        await _result(ms["U1-1"].id, 0, 2)
    assert [a["no"] for a in exc.value.data["affected"]] == [ms["U2-1"].match_no]
    await _result(ms["U1-1"].id, 0, 2, cascade=True)
    after = await _matches(tid)
    assert after["U2-1"].status == "WAITING" and after["U2-1"].winner_team_id is None
    assert after["U2-1"].team1_id == ms["U1-1"].team2_id
    assert after["L1-1"].team1_id == ms["U1-1"].team1_id
    # Тот же победитель — просто правка счёта без каскада.
    await _result(after["U1-1"].id, 1, 2)
    assert (await _matches(tid))["U1-1"].score1 == 1


async def test_schedule_moscow_time_and_warnings(db, monkeypatch):
    # Тест про 02.10.2026: «сейчас» — утро того дня, иначе сработает «время уже прошло»
    monkeypatch.setattr(engine.timeutil, "now_utc", lambda: datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc))
    tid, _ = await _setup(8)
    await _generate(tid)
    ms = await _matches(tid)
    async with session_scope() as s:
        # Раньше старта турнира (17:50 МСК) -> предупреждение.
        with pytest.raises(engine.NeedConfirm) as exc:
            await engine.set_schedule(s, ms["U1-1"].id, "02.10.2026 16:00")
        assert any("старта турнира" in w for w in exc.value.data["warnings"])
        out = await engine.set_schedule(s, ms["U1-1"].id, "02.10.2026 18:00")
        await s.commit()
    assert out["match"].status == "SCHEDULED"
    assert engine.timeutil.fmt_local(out["match"].scheduled_at) == "02.10.2026 18:00"
    async with session_scope() as s:
        # Следующий матч раньше, чем закончится источник (BO3 ≈ 3 ч).
        with pytest.raises(engine.NeedConfirm) as exc:
            await engine.set_schedule(s, ms["U2-1"].id, "02.10.2026 19:00")
        assert any("может ещё не закончиться" in w for w in exc.value.data["warnings"])
        await engine.set_schedule(s, ms["U2-1"].id, "02.10.2026 19:00", force=True)
        # Перенос.
        moved = await engine.set_schedule(s, ms["U1-1"].id, "03.10.2026 18:00", postpone=True, force=True)
        await s.commit()
    assert moved["match"].status == "POSTPONED"
    async with session_scope() as s:
        with pytest.raises(engine.EngineError):
            await engine.set_schedule(s, ms["U1-1"].id, "32.13.2026 99:99")
        sched = await engine.schedule_view(s, await s.get(Tournament, tid))
    assert sched["items"][0]["day"] == "02.10.2026" and sched["items"][0]["time"] == "19:00"


async def test_single_elimination_with_byes_has_no_fake_matches(db):
    tid, teams = await _setup(6)
    await _generate(tid, fmt="single_elim", settings={"bo_default": 1, "bo_final": 3})
    ms = await _matches(tid)
    assert len(ms) == 5
    first = [m for m in ms.values() if m.round_no == 1]
    assert len(first) == 2 and all(m.team1_id and m.team2_id for m in first)
    assert ms["R3-1"].best_of == 3


async def test_grand_final_reset_created_and_removed(db):
    tid, _ = await _setup(4)
    await _generate(tid, settings={"bo_default": 1, "bo_final": 1, "grand_final_reset": True})
    for key in ("U1-1", "U1-2"):
        await _result((await _matches(tid))[key].id, 1, 0)
    for key in ("U2-1", "L1-1", "L2-1"):
        await _result((await _matches(tid))[key].id, 1, 0)
    gf = (await _matches(tid))["GF"]
    await _result(gf.id, 0, 1)  # победил финалист нижней сетки -> перезапуск
    ms = await _matches(tid)
    assert "GF2" in ms and {ms["GF2"].team1_id, ms["GF2"].team2_id} == {gf.team1_id, gf.team2_id}
    await _result(gf.id, 1, 0)  # исправили: победил финалист верхней -> перезапуск не нужен
    assert "GF2" not in await _matches(tid)


async def test_lock_requires_confirmed_mmr_and_blocks_regeneration(db):
    tid, _ = await _setup(4, mmr=False)
    await _generate(tid)
    async with session_scope() as s:
        with pytest.raises(engine.EngineError):
            await engine.lock_stage(s, await s.get(Tournament, tid))
    tid2, _ = await _setup(4)
    await _generate(tid2)
    async with session_scope() as s:
        await engine.lock_stage(s, await s.get(Tournament, tid2))
        await s.commit()
    with pytest.raises(engine.EngineError):
        await _generate(tid2)
    async with session_scope() as s:
        with pytest.raises(engine.NeedConfirm):
            await engine.reset_stage(s, await s.get(Tournament, tid2))
        await engine.reset_stage(s, await s.get(Tournament, tid2), force=True)
        await s.commit()
    assert await _matches(tid2) == {}


async def test_match_reminders_once_per_scheduled_time(db):
    from datetime import timedelta

    from services.tournament_engine import reminders

    tid, _ = await _setup(4)
    await _generate(tid, fmt="single_elim", settings={"bo_default": 1, "bo_final": 1})
    ms = await _matches(tid)
    now = timeutil.now_utc()
    async with session_scope() as s:
        await engine.set_schedule(s, ms["R1-1"].id, (now + timedelta(minutes=20)).isoformat(), force=True)
        await engine.set_schedule(s, ms["R1-2"].id, (now + timedelta(hours=5)).isoformat(), force=True)
        await s.commit()
    sent: list[tuple[int, str]] = []

    async def send(chat_id, text, markup=None):
        sent.append((chat_id, text))

    assert await reminders.run_once(send, now=now) == 1
    assert len(sent) == 10 and "Скоро матч" in sent[0][1]
    assert await reminders.run_once(send, now=now) == 0  # второй раз не шлём
    async with session_scope() as s:  # перенос -> новое напоминание
        await engine.set_schedule(s, ms["R1-1"].id, (now + timedelta(minutes=25)).isoformat(), postpone=True, force=True)
        await s.commit()
    assert await reminders.run_once(send, now=now) == 1


async def test_import_legacy_json_keeps_published_pairs(db, tmp_path, monkeypatch):
    import services.bracket as bracket

    monkeypatch.setattr(bracket, "BRACKET_DIR", tmp_path / "brackets")
    tid, teams = await _setup(8)
    async with session_scope() as s:
        t = await s.get(Tournament, tid)
        state = await bracket.generate(s, t, teams[0], teams[4], lock=False)
        await s.commit()
    legacy_pairs = [[m["team_a"]["id"], m["team_b"]["id"]] for m in state["bracket"]["rounds"][0]["matches"]]
    async with session_scope() as s:
        await engine.import_legacy(s, await s.get(Tournament, tid), fmt_key="double_elim", settings=SETTINGS)
        await s.commit()
    ms = await _matches(tid)
    assert [[ms[f"U1-{i}"].team1_id, ms[f"U1-{i}"].team2_id] for i in range(1, 5)] == legacy_pairs
    # Старый API теперь отдаёт сетку из БД в прежнем формате.
    async with session_scope() as s:
        pub = await bracket.public_state(s, await s.get(Tournament, tid))
    assert pub["exists"] and pub["engine"]["format"] == "double_elim"
    assert pub["bracket"]["rounds"][0]["matches"][0]["team_a"]["id"] == legacy_pairs[0][0]
    stored = json.loads((tmp_path / "brackets" / f"tournament_{tid}.json").read_text(encoding="utf-8"))
    assert stored["seed_map"]  # JSON-файл не тронут — остаётся для отката


async def test_bot_matches_command_text(db):
    from handlers.matches import team_matches_text

    tid, teams = await _setup(4)
    await _generate(tid, fmt="single_elim", settings={"bo_default": 3, "bo_final": 3})
    ms = await _matches(tid)
    async with session_scope() as s:
        await engine.set_schedule(s, ms["R1-1"].id, "02.10.2026 18:00", force=True)
        await s.commit()
        captain = (await s.get(__import__("database.models", fromlist=["Team"]).Team, ms["R1-1"].team1_id)).captain_user_id
        tg = (await s.get(User, captain)).telegram_id
    text = await team_matches_text(tg)
    assert "02.10.2026 18:00" in text and "BO3" in text and "Запланирован" in text
    assert "не в команде" in await team_matches_text(999999999)
