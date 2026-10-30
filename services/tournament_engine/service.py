"""Сервис турнирного движка: генерация, фиксация, результаты, расписание, выдача.

Функции работают в переданной сессии и НЕ делают commit — коммитит вызывающий
роут (одна транзакция на действие админа). Ошибки для пользователя — EngineError
(400) и NeedConfirm (409: нужно подтверждение, в data — предупреждения или
список затронутых матчей).
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    ApplicationStatus,
    PlayerProfile,
    StageParticipant,
    Team,
    TeamMember,
    TeamRole,
    Tournament,
    TournamentApplication,
    TournamentMatch,
    TournamentMatchLog,
    TournamentStage,
)
from services.tournament_engine import seeding, timeutil
from services.tournament_engine.formats import FORMATS, StageFormat, get_format
from services.tournament_engine.plan import MatchSpec, collapse, order_matches
from services.tournament_engine.series import (
    SeriesError,
    allowed_scores,
    forfeit_score,
    validate_bo,
    validate_score,
    wins_needed,
)

logger = logging.getLogger("TOURNAMENT_ENGINE")

STATUS_TEXT = {
    "WAITING": "Ожидается",
    "SCHEDULED": "Запланирован",
    "LIVE": "Идёт",
    "FINISHED": "Завершён",
    "POSTPONED": "Перенесён",
}
GAME_MINUTES = 60  # оценка длительности одной карты для проверок расписания
MIN_CONFIRMED_MMR = 5
SEEDING_MODES = ("standard", "opening", "pairs", "everyone")


class EngineError(ValueError):
    def __init__(self, message: str, *, status: int = 400, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


class NeedConfirm(EngineError):
    def __init__(self, message: str, data: dict[str, Any]):
        super().__init__(message, status=409, data={"need_confirm": True, **data})


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------

def _confirmed_mmr_expr():
    """Как в прежней сетке: учитывается только MMR с флагом mmr_verified."""
    return case(
        (PlayerProfile.mmr_verified.is_(True), func.coalesce(PlayerProfile.approved_mmr, PlayerProfile.exact_mmr)),
        else_=None,
    )


async def approved_teams(session: AsyncSession, tournament_id: int) -> list[dict[str, Any]]:
    """Одобренные команды турнира с агрегатами MMR (один запрос, без N+1)."""
    expr = _confirmed_mmr_expr()
    stats = (
        select(
            TeamMember.team_id.label("team_id"),
            func.count(TeamMember.id).label("members"),
            func.count(expr).label("mmr_count"),
            func.avg(expr).label("mmr_avg"),
        )
        .select_from(TeamMember)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == TeamMember.user_id)
        .where(TeamMember.role != TeamRole.COACH)
        .group_by(TeamMember.team_id)
        .subquery()
    )
    rows = (await session.execute(
        select(Team.id, Team.name, Team.avatar_file_id, stats.c.members, stats.c.mmr_count, stats.c.mmr_avg)
        .join(TournamentApplication, TournamentApplication.team_id == Team.id)
        .outerjoin(stats, stats.c.team_id == Team.id)
        .where(
            TournamentApplication.tournament_id == int(tournament_id),
            TournamentApplication.status == ApplicationStatus.APPROVED,
        )
        .order_by(Team.id)
    )).all()
    out = []
    for tid, name, avatar_file_id, members, mmr_count, mmr_avg in rows:
        count = int(mmr_count or 0)
        out.append({
            "id": int(tid),
            "name": name,
            "avg_mmr": round(float(mmr_avg)) if mmr_avg is not None else None,
            "mmr_count": count,
            "mmr_complete": count >= MIN_CONFIRMED_MMR,
            "roster_count": int(members or 0),
            "avatar_file": bool(avatar_file_id),
        })
    return out


async def get_stage(session: AsyncSession, tournament_id: int) -> TournamentStage | None:
    return await session.scalar(
        select(TournamentStage)
        .where(TournamentStage.tournament_id == int(tournament_id))
        .order_by(TournamentStage.order_no, TournamentStage.id)
        .limit(1)
    )


async def _stage_matches(session: AsyncSession, stage_id: int) -> list[TournamentMatch]:
    return list((await session.execute(
        select(TournamentMatch).where(TournamentMatch.stage_id == int(stage_id)).order_by(TournamentMatch.match_no)
    )).scalars().all())


def _settings(stage: TournamentStage) -> dict[str, Any]:
    try:
        data = json.loads(stage.settings_json or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


async def _load_match(session: AsyncSession, match_id: int) -> tuple[TournamentMatch, TournamentStage, StageFormat, dict[str, Any]]:
    m = await session.get(TournamentMatch, int(match_id))
    if m is None:
        raise EngineError("Матч не найден.", status=404)
    stage = await session.get(TournamentStage, m.stage_id)
    if stage is None:
        raise EngineError("Этап турнира не найден.", status=404)
    fmt = FORMATS.get(stage.format)
    if fmt is None:
        raise EngineError("Формат этапа не поддерживается этой версией.")
    return m, stage, fmt, _settings(stage)


async def _team_names(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    ids = {int(x) for x in ids if x is not None}
    if not ids:
        return {}
    rows = (await session.execute(select(Team.id, Team.name).where(Team.id.in_(ids)))).all()
    out = {int(i): n for i, n in rows}
    gone = ids - set(out)
    if gone:  # 5.13: расформированные команды — из архива, история матчей остаётся читаемой
        from database.models import TeamArchive

        for i, n in (await session.execute(select(TeamArchive.team_id, TeamArchive.name).where(TeamArchive.team_id.in_(gone)))).all():
            out[int(i)] = f"{n} (расформирована)"
    return out


# ---------------------------------------------------------------------------
# Служебное
# ---------------------------------------------------------------------------

def _snapshot(m: TournamentMatch) -> dict[str, Any]:
    at = timeutil.as_utc(m.scheduled_at)
    return {
        "team1": m.team1_id, "team2": m.team2_id, "score1": m.score1, "score2": m.score2,
        "winner": m.winner_team_id, "result_type": m.result_type, "status": m.status,
        "best_of": m.best_of, "scheduled_at": at.isoformat() if at else None,
    }


def _log(session: AsyncSession, *, stage_id: int, match_id: int, action: str, old: Any, new: Any, admin_user_id: int | None) -> None:
    session.add(TournamentMatchLog(
        match_id=int(match_id), stage_id=int(stage_id), action=action[:32],
        old_json=json.dumps(old, ensure_ascii=False) if old is not None else None,
        new_json=json.dumps(new, ensure_ascii=False) if new is not None else None,
        admin_user_id=admin_user_id,
    ))


def _derive(m: TournamentMatch) -> None:
    """Автостатус для неначатых матчей: обе команды + время = «Запланирован»."""
    if m.status in ("LIVE", "FINISHED", "POSTPONED"):
        return
    m.status = "SCHEDULED" if (m.team1_id and m.team2_id and m.scheduled_at) else "WAITING"


def _clear_result(m: TournamentMatch) -> None:
    m.score1 = m.score2 = None
    m.winner_team_id = None
    m.result_type = None
    m.finished_at = None
    m.started_at = None
    m.status = "WAITING"
    _derive(m)


def _bump(m: TournamentMatch, admin_user_id: int | None) -> None:
    m.version = int(m.version or 1) + 1
    m.updated_by = admin_user_id
    m.updated_at = timeutil.now_utc()


def _short(m: TournamentMatch) -> dict[str, Any]:
    return {"id": int(m.id), "no": int(m.match_no), "round_name": m.round_name, "status": m.status,
            "status_text": STATUS_TEXT.get(m.status, m.status)}


async def _update_stage_status(session: AsyncSession, stage: TournamentStage) -> None:
    pending = int(await session.scalar(
        select(func.count(TournamentMatch.id)).where(TournamentMatch.stage_id == stage.id, TournamentMatch.status != "FINISHED")
    ) or 0)
    stage.status = "FINISHED" if pending == 0 else "ACTIVE"


def _check_mmr_complete(teams: list[dict[str, Any]]) -> None:
    incomplete = [t for t in teams if not t["mmr_complete"]]
    if incomplete:
        names = ", ".join(t["name"] for t in incomplete[:5])
        extra = "…" if len(incomplete) > 5 else ""
        raise EngineError(
            "Нельзя зафиксировать сетку: у каждой команды нужно минимум 5 игроков "
            f"с подтверждённым MMR. Проблемные команды: {names}{extra}."
        )


async def _ensure_regenerable(session: AsyncSession, stage: TournamentStage) -> None:
    if stage.locked:
        raise EngineError("Сетка зафиксирована. Чтобы пересобрать её, сначала нажмите «Сбросить сетку».")
    played = int(await session.scalar(
        select(func.count(TournamentMatch.id)).where(
            TournamentMatch.stage_id == stage.id, TournamentMatch.status.in_(("LIVE", "FINISHED"))
        )
    ) or 0)
    if played:
        raise EngineError("В сетке уже есть начатые или сыгранные матчи — пересборка запрещена.")


# ---------------------------------------------------------------------------
# Генерация, фиксация, сброс, перенос старой сетки
# ---------------------------------------------------------------------------

async def generate(
    session: AsyncSession,
    tournament: Tournament,
    *,
    fmt_key: str,
    seeding_mode: str = "standard",
    settings: dict[str, Any] | None = None,
    opening: tuple[int, int] | None = None,
    pairs: list[list[int | None]] | None = None,
    seeds: dict[int, int] | None = None,
    admin_user_id: int | None = None,
    lock: bool = False,
    check_mmr_on_lock: bool = True,
    extra_settings: dict[str, Any] | None = None,
    allow_empty_pairs: bool = False,
) -> TournamentStage:
    try:
        fmt = get_format(fmt_key)
        norm = fmt.normalize_settings(settings)
    except (ValueError, SeriesError) as exc:
        raise EngineError(str(exc)) from None
    if seeding_mode not in SEEDING_MODES:
        raise EngineError("Неизвестный способ посева.")
    stages = await get_stages(session, tournament.id)
    existing = stages[0] if stages else None
    if existing is not None:
        await _ensure_regenerable(session, existing)
        if len(stages) > 1:
            raise EngineError("Плей-офф уже сформирован. Чтобы пересобрать всё, нажмите «Сбросить сетку».")

    teams = await approved_teams(session, tournament.id)
    by_id = {t["id"]: t for t in teams}
    groups: dict[int, str | None] = {}
    stage_extra: dict[str, Any] = {}
    try:
        if not fmt.uses_slots:
            if seeding_mode == "pairs":
                raise EngineError("Перенос пар возможен только в сетку (single/double elimination).")
            if len(teams) > int(tournament.max_teams):
                raise EngineError("Количество одобренных команд превышает лимит турнира.")
            if len(teams) < fmt.min_teams:
                raise EngineError(f"Для формата «{fmt.title}» нужно минимум {fmt.min_teams} одобренные команды (сейчас {len(teams)}).")
            entrants = [t["id"] for t in seeding.sort_by_skill(teams)]
            virtual, meta, stage_extra = fmt.build_table(entrants, norm)
            specs = order_matches(collapse(virtual))
            seeds_map = {tid: i + 1 for i, tid in enumerate(entrants)}
            groups = {int(tid): m.get("group") for tid, m in meta.items()}
            seeding_mode = "standard"
        elif seeding_mode == "pairs":
            slots, seeds_map = seeding.pairs_slots(pairs or [], seeds, allow_empty=allow_empty_pairs)
            unknown = [t for t in slots if t is not None and t not in by_id]
            if unknown:
                raise EngineError("В сетке есть команды без одобренной заявки: " + ", ".join(map(str, unknown)))
        else:
            if len(teams) > int(tournament.max_teams):
                raise EngineError("Количество одобренных команд превышает лимит турнира.")
            if len(teams) < fmt.min_teams:
                raise EngineError(f"Для формата «{fmt.title}» нужно минимум {fmt.min_teams} одобренные команды (сейчас {len(teams)}).")
            ordered = seeding.sort_by_skill(teams)
            if seeding_mode == "opening":
                if not opening or opening[0] in (None, "") or opening[1] in (None, ""):
                    raise EngineError("Выберите две команды для первого матча.")
                a, b = int(opening[0]), int(opening[1])
                if a == b:
                    raise EngineError("Команды первого матча должны быть разными.")
                if a not in by_id or b not in by_id:
                    raise EngineError("Для первого матча выберите две допущенные команды.")
                rest = [t["id"] for t in ordered if t["id"] not in (a, b)]
                slots, seeds_map = seeding.opening_slots(a, b, rest)
            elif seeding_mode == "everyone":
                ids = [t["id"] for t in ordered]
                real, bye = seeding.everyone_pairs(ids)
                slots, seeds_map = seeding.pairs_slots(seeding.layout_pairs(real, bye),
                                                       {tid: i + 1 for i, tid in enumerate(ids)}, allow_empty=True)
            else:
                slots, seeds_map = seeding.standard_slots([t["id"] for t in ordered])
        if fmt.uses_slots:
            entrants = [t for t in slots if t is not None]
            if len(entrants) < fmt.min_teams:
                raise EngineError(f"Для формата «{fmt.title}» нужно минимум {fmt.min_teams} команды.")
            specs = order_matches(collapse(fmt.build(slots, norm)))
    except EngineError:
        raise
    except ValueError as exc:
        raise EngineError(str(exc)) from None

    if lock and check_mmr_on_lock:
        _check_mmr_complete([by_id[t] for t in entrants])

    return await _create_stage(
        session, tournament, fmt, norm, specs, entrants, seeds_map, groups, order_no=1, existing=existing,
        seeding_mode=seeding_mode, extra={**stage_extra, **(extra_settings or {})}, lock=lock, admin_user_id=admin_user_id,
    )


async def get_stages(session: AsyncSession, tournament_id: int) -> list[TournamentStage]:
    return list((await session.execute(
        select(TournamentStage).where(TournamentStage.tournament_id == int(tournament_id))
        .order_by(TournamentStage.order_no, TournamentStage.id)
    )).scalars().all())


async def _create_stage(
    session: AsyncSession,
    tournament: Tournament,
    fmt: StageFormat,
    norm: dict[str, Any],
    specs: list[MatchSpec],
    entrants: list[int],
    seeds_map: dict[int, int],
    groups: dict[int, str | None],
    *,
    order_no: int,
    existing: TournamentStage | None,
    seeding_mode: str,
    extra: dict[str, Any],
    lock: bool,
    admin_user_id: int | None,
) -> TournamentStage:
    now = timeutil.now_utc()
    if existing is not None:
        await session.execute(delete(TournamentMatch).where(TournamentMatch.stage_id == existing.id))
        await session.execute(delete(StageParticipant).where(StageParticipant.stage_id == existing.id))
        stage = existing
    else:
        stage = TournamentStage(tournament_id=int(tournament.id), order_no=order_no)
        session.add(stage)
    settings_all = {**norm, "seeding": seeding_mode, "generated_at": now.isoformat(), **extra}
    stage.format = fmt.key
    stage.name = fmt.describe(settings_all)
    stage.status = "ACTIVE"
    stage.settings_json = json.dumps(settings_all, ensure_ascii=False)
    stage.locked = bool(lock)
    stage.created_by = admin_user_id
    stage.updated_at = now
    await session.flush()

    for tid in entrants:
        session.add(StageParticipant(stage_id=stage.id, team_id=int(tid), seed=seeds_map.get(int(tid)),
                                     group_label=groups.get(int(tid))))

    rows: dict[str, TournamentMatch] = {}
    for no, spec in enumerate(specs, start=1):
        m = TournamentMatch(
            tournament_id=int(tournament.id), stage_id=stage.id, plan_key=spec.key, bracket=spec.bracket,
            round_no=spec.round_no, position=spec.position, match_no=no, round_name=spec.round_name,
            best_of=spec.best_of, status="WAITING", version=1, updated_by=admin_user_id,
        )
        if spec.src1 and spec.src1.kind == "team":
            m.team1_id = int(spec.src1.ref)
        if spec.src2 and spec.src2.kind == "team":
            m.team2_id = int(spec.src2.ref)
        session.add(m)
        rows[spec.key] = m
    await session.flush()
    for spec in specs:
        target = rows[spec.key]
        for slot, src in ((1, spec.src1), (2, spec.src2)):
            if src is None or src.kind == "team":
                continue
            source = rows[src.ref]
            if src.kind == "winner":
                source.winner_to_match_id, source.winner_to_slot = target.id, slot
            else:
                source.loser_to_match_id, source.loser_to_slot = target.id, slot
    for m in rows.values():
        _derive(m)
    _log(session, stage_id=stage.id, match_id=0, action="generate", old=None,
         new={"format": fmt.key, "seeding": seeding_mode, "teams": len(entrants), "matches": len(specs), "locked": bool(lock)},
         admin_user_id=admin_user_id)
    logger.info("[ENGINE] tournament=%s generated %s: teams=%s matches=%s locked=%s",
                tournament.id, fmt.key, len(entrants), len(specs), lock)
    return stage


async def lock_stage(session: AsyncSession, tournament: Tournament, *, admin_user_id: int | None = None) -> TournamentStage:
    stage = await get_stage(session, tournament.id)
    if stage is None:
        raise EngineError("Сначала сгенерируйте сетку.")
    if stage.locked:
        return stage
    parts = set((await session.execute(
        select(StageParticipant.team_id).where(StageParticipant.stage_id == stage.id)
    )).scalars().all())
    teams = [t for t in await approved_teams(session, tournament.id) if t["id"] in parts]
    missing = parts - {t["id"] for t in teams}
    if missing:
        raise EngineError("В сетке есть команды, чья заявка больше не одобрена. Пересоберите сетку.")
    _check_mmr_complete(teams)
    stage.locked = True
    stage.updated_at = timeutil.now_utc()
    _log(session, stage_id=stage.id, match_id=0, action="lock", old=None, new={"locked": True}, admin_user_id=admin_user_id)
    return stage


async def reset_stage(session: AsyncSession, tournament: Tournament, *, admin_user_id: int | None = None, force: bool = False) -> None:
    """Удаляет все этапы турнира (группы и плей-офф) вместе с расписанием и результатами."""
    stages = await get_stages(session, tournament.id)
    if not stages:
        raise EngineError("Сетки нет.")
    ids = [s.id for s in stages]
    played = int(await session.scalar(
        select(func.count(TournamentMatch.id)).where(
            TournamentMatch.stage_id.in_(ids), TournamentMatch.status.in_(("LIVE", "FINISHED"))
        )
    ) or 0)
    locked = any(s.locked for s in stages)
    if (played or locked) and not force:
        raise NeedConfirm(
            "Сетка будет удалена вместе с расписанием" + (f" и результатами ({played})" if played else "") + ".",
            {"played": played, "locked": bool(locked)},
        )
    for stage in stages:
        _log(session, stage_id=stage.id, match_id=0, action="reset_stage", old={"format": stage.format, "played": played},
             new=None, admin_user_id=admin_user_id)
        await session.execute(delete(TournamentMatch).where(TournamentMatch.stage_id == stage.id))
        await session.execute(delete(StageParticipant).where(StageParticipant.stage_id == stage.id))
        await session.delete(stage)


async def create_playoffs(session: AsyncSession, tournament: Tournament, *, admin_user_id: int | None = None) -> TournamentStage:
    """Плей-офф из итогов групп/швейцарки: лучшие проходят, посев «победители групп, затем вторые места…»."""
    stages = await get_stages(session, tournament.id)
    if not stages:
        raise EngineError("Сначала сгенерируйте групповой этап.")
    if len(stages) > 1:
        raise EngineError("Плей-офф уже сформирован.")
    first = stages[0]
    fmt = FORMATS.get(first.format)
    settings = _settings(first)
    if fmt is None or not fmt.feeds_playoffs(settings):
        raise EngineError("В этом формате плей-офф не предусмотрен.")
    matches = await _stage_matches(session, first.id)
    if not matches or any(m.status != "FINISHED" for m in matches):
        raise EngineError("Групповой этап ещё не завершён: сыграны не все матчи.")
    standings = await _standings_for(session, first, fmt, settings, matches)
    qualified = fmt.qualifiers(standings, settings)
    try:
        playoff = get_format(settings.get("playoff_format") or "single_elim")
        norm = playoff.normalize_settings({
            "bo_default": settings.get("playoff_bo_default", 3), "bo_final": settings.get("playoff_bo_final", 3),
            "grand_final_reset": settings.get("grand_final_reset"), "third_place": settings.get("third_place"),
        })
        if len(qualified) < playoff.min_teams:
            raise EngineError(f"В плей-офф выходит {len(qualified)} команд — для «{playoff.title}» нужно минимум {playoff.min_teams}.")
        slots, seeds_map = seeding.standard_slots(qualified)
        specs = order_matches(collapse(playoff.build(slots, norm)))
    except EngineError:
        raise
    except (ValueError, SeriesError) as exc:
        raise EngineError(str(exc)) from None
    stage2 = await _create_stage(
        session, tournament, playoff, norm, specs, qualified, seeds_map, {}, order_no=2, existing=None,
        seeding_mode="standings", extra={"source_stage": int(first.id)}, lock=True, admin_user_id=admin_user_id,
    )
    first.status = "FINISHED"
    return stage2


async def import_legacy(
    session: AsyncSession,
    tournament: Tournament,
    *,
    fmt_key: str,
    settings: dict[str, Any] | None = None,
    admin_user_id: int | None = None,
) -> TournamentStage:
    """Переносит опубликованную JSON-сетку (runtime/brackets) с теми же парами первого раунда."""
    from services.bracket import _read_state, build_bracket  # ленивый импорт: bracket.py сам импортирует движок

    if await get_stage(session, tournament.id) is not None:
        raise EngineError("Сетка уже в новом движке.")
    state = _read_state(tournament.id)
    seed_map = {str(k): int(v) for k, v in ((state or {}).get("seed_map") or {}).items()}
    if not seed_map:
        raise EngineError("Старой сетки для переноса нет — сгенерируйте новую.")
    teams = await approved_teams(session, tournament.id)
    team_map = {t["id"]: {**t, "avatar": False} for t in teams}
    try:
        legacy = build_bracket(seed_map, team_map)
    except (ValueError, KeyError):
        raise EngineError("Старую сетку не перенести: состав одобренных команд изменился. Сгенерируйте новую.") from None
    first = legacy["rounds"][0]["matches"]
    pairs = [[(m.get("team_a") or {}).get("id"), (m.get("team_b") or {}).get("id")] for m in first]
    seeds = {int(v): int(k) for k, v in seed_map.items()}
    return await generate(
        session, tournament, fmt_key=fmt_key, seeding_mode="pairs", settings=settings, pairs=pairs, seeds=seeds,
        admin_user_id=admin_user_id, lock=bool(state.get("locked")), check_mmr_on_lock=False,
        extra_settings={"imported_from": "runtime/brackets json", "legacy_generated_at": state.get("generated_at")},
    )


# ---------------------------------------------------------------------------
# Продвижение и пересчёт
# ---------------------------------------------------------------------------

def _loser_of(m: TournamentMatch) -> int | None:
    if m.winner_team_id is None:
        return None
    return m.team2_id if m.winner_team_id == m.team1_id else m.team1_id


async def _followup_rows(session: AsyncSession, m: TournamentMatch, fmt: StageFormat) -> list[TournamentMatch]:
    keys = fmt.followup_keys(m.plan_key)
    if not keys:
        return []
    return list((await session.execute(
        select(TournamentMatch).where(TournamentMatch.stage_id == m.stage_id, TournamentMatch.plan_key.in_(keys))
    )).scalars().all())


async def _advance(session: AsyncSession, m: TournamentMatch, fmt: StageFormat, settings: dict[str, Any]) -> None:
    for target_id, slot, team_id in (
        (m.winner_to_match_id, m.winner_to_slot, m.winner_team_id),
        (m.loser_to_match_id, m.loser_to_slot, _loser_of(m)),
    ):
        if not target_id:
            continue
        target = await session.get(TournamentMatch, target_id)
        if target is None:
            continue
        setattr(target, f"team{int(slot)}_id", team_id)
        _derive(target)
    winner_slot = 1 if m.winner_team_id == m.team1_id else 2
    specs: list[MatchSpec] = fmt.followups(m.plan_key, winner_slot, settings)
    if specs:
        existing = {x.plan_key for x in await _followup_rows(session, m, fmt)}
        top = int(await session.scalar(select(func.max(TournamentMatch.match_no)).where(TournamentMatch.stage_id == m.stage_id)) or 0)
        for spec in specs:
            if spec.key in existing:
                continue
            top += 1
            teams = []
            for src in (spec.src1, spec.src2):
                _, n = src.ref  # ("slot", (key, n)) — команда из слота текущего матча
                teams.append(m.team1_id if int(n) == 1 else m.team2_id)
            row = TournamentMatch(
                tournament_id=m.tournament_id, stage_id=m.stage_id, plan_key=spec.key, bracket=spec.bracket,
                round_no=spec.round_no, position=spec.position, match_no=top, round_name=spec.round_name,
                best_of=spec.best_of, status="WAITING", team1_id=teams[0], team2_id=teams[1], version=1,
            )
            _derive(row)
            session.add(row)


async def _collect_affected(session: AsyncSession, m: TournamentMatch, fmt: StageFormat, out: list[TournamentMatch]) -> None:
    targets = [await session.get(TournamentMatch, tid) for tid in (m.winner_to_match_id, m.loser_to_match_id) if tid]
    targets += await _followup_rows(session, m, fmt)
    for t in targets:
        if t is None or any(x.id == t.id for x in out):
            continue
        if t.status in ("LIVE", "FINISHED"):
            out.append(t)
            if t.status == "FINISHED":
                await _collect_affected(session, t, fmt, out)


async def _retract(session: AsyncSession, m: TournamentMatch, fmt: StageFormat, admin_user_id: int | None) -> None:
    """Отменяет последствия результата матча m во всех зависимых матчах (каскадно)."""
    for target_id, slot in ((m.winner_to_match_id, m.winner_to_slot), (m.loser_to_match_id, m.loser_to_slot)):
        if not target_id:
            continue
        target = await session.get(TournamentMatch, target_id)
        if target is None:
            continue
        if target.status in ("FINISHED", "LIVE"):
            old = _snapshot(target)
            if target.status == "FINISHED":
                await _retract(session, target, fmt, admin_user_id)
            _clear_result(target)
            _bump(target, admin_user_id)
            _log(session, stage_id=target.stage_id, match_id=target.id, action="cascade_reset", old=old,
                 new=_snapshot(target), admin_user_id=admin_user_id)
        setattr(target, f"team{int(slot)}_id", None)
        if target.status == "POSTPONED" and not (target.team1_id and target.team2_id):
            target.status = "WAITING"
        _derive(target)
    for row in await _followup_rows(session, m, fmt):
        if row.status == "FINISHED":
            await _retract(session, row, fmt, admin_user_id)
        _log(session, stage_id=row.stage_id, match_id=row.id, action="followup_removed", old=_snapshot(row), new=None,
             admin_user_id=admin_user_id)
        await session.delete(row)


# ---------------------------------------------------------------------------
# Таблицы (круговая система, группы, швейцарка) и динамические туры
# ---------------------------------------------------------------------------

POINTS_WIN, POINTS_DRAW = 3, 1


async def _standings_for(
    session: AsyncSession,
    stage: TournamentStage,
    fmt: StageFormat | None,
    settings: dict[str, Any],
    matches: list[TournamentMatch] | None = None,
    names: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Турнирная таблица по группам с тай-брейками.

    rr:    очки (3/1/0) → личные встречи среди равных → разница карт → выигранные карты → посев;
    gsl:   победы → меньше поражений → разница карт → посев (в GSL место однозначно по счёту 2-0/2-1/1-2/0-2);
    swiss: победы → меньше поражений → Buchholz (сумма побед соперников) → разница карт → посев.
    """
    if fmt is None or not fmt.table:
        return []
    matches = matches if matches is not None else await _stage_matches(session, stage.id)
    parts = (await session.execute(
        select(StageParticipant.team_id, StageParticipant.seed, StageParticipant.group_label)
        .where(StageParticipant.stage_id == stage.id)
    )).all()
    names = names if names is not None else await _team_names(session, {int(t) for t, _, _ in parts})
    rows: dict[int, dict[str, Any]] = {}
    for tid, seed, grp in parts:
        rows[int(tid)] = {"team_id": int(tid), "name": names.get(int(tid), "Команда удалена"), "seed": seed, "group": grp,
                          "played": 0, "wins": 0, "draws": 0, "losses": 0, "maps_won": 0, "maps_lost": 0,
                          "points": 0, "byes": 0, "opponents": []}
    h2h: dict[tuple[int, int], int] = {}
    for m in matches:
        if m.status != "FINISHED" or not (m.team1_id and m.team2_id):
            continue
        a, b = rows.get(int(m.team1_id)), rows.get(int(m.team2_id))
        if a is None or b is None:
            continue
        s1, s2 = int(m.score1 or 0), int(m.score2 or 0)
        for me, other, won, lost in ((a, b, s1, s2), (b, a, s2, s1)):
            me["played"] += 1
            me["maps_won"] += won
            me["maps_lost"] += lost
            me["opponents"].append(other["team_id"])
        if m.winner_team_id is None:
            a["draws"] += 1
            b["draws"] += 1
            a["points"] += POINTS_DRAW
            b["points"] += POINTS_DRAW
            h2h[(a["team_id"], b["team_id"])] = h2h.get((a["team_id"], b["team_id"]), 0) + POINTS_DRAW
            h2h[(b["team_id"], a["team_id"])] = h2h.get((b["team_id"], a["team_id"]), 0) + POINTS_DRAW
        else:
            w, lo = (a, b) if int(m.winner_team_id) == a["team_id"] else (b, a)
            w["wins"] += 1
            lo["losses"] += 1
            w["points"] += POINTS_WIN
            h2h[(w["team_id"], lo["team_id"])] = h2h.get((w["team_id"], lo["team_id"]), 0) + POINTS_WIN
    for tid, rounds in (settings.get("swiss_byes") or {}).items():
        row = rows.get(int(tid))
        if row is not None:
            row["byes"] = len(rounds or [])
            row["wins"] += row["byes"]
            row["points"] += POINTS_WIN * row["byes"]
    for r in rows.values():
        r["map_diff"] = r["maps_won"] - r["maps_lost"]
        r["buchholz"] = sum(rows[o]["wins"] for o in r["opponents"] if o in rows)
    mode = fmt.standings_mode
    wins_need = int(settings.get("wins_to_advance") or 0)
    loss_need = int(settings.get("losses_to_eliminate") or 0)
    advance = int(settings.get("advance") or 0) if fmt.feeds_playoffs(settings) else 0
    seed = lambda r: r["seed"] if r["seed"] is not None else 999  # noqa: E731

    def order(group_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if mode == "gsl":
            return sorted(group_rows, key=lambda r: (-r["wins"], r["losses"], -r["map_diff"], seed(r)))
        if mode == "swiss":
            return sorted(group_rows, key=lambda r: (-r["wins"], r["losses"], -r["buchholz"], -r["map_diff"], seed(r)))
        ranked = sorted(group_rows, key=lambda r: (-r["points"], -r["map_diff"], -r["maps_won"], seed(r)))
        out: list[dict[str, Any]] = []
        i = 0
        while i < len(ranked):  # блоки с равными очками — личные встречи внутри блока
            j = i
            while j < len(ranked) and ranked[j]["points"] == ranked[i]["points"]:
                j += 1
            block = ranked[i:j]
            if len(block) > 1:
                ids = {r["team_id"] for r in block}
                for r in block:
                    r["h2h"] = sum(h2h.get((r["team_id"], o), 0) for o in ids if o != r["team_id"])
                block.sort(key=lambda r: (-r["h2h"], -r["map_diff"], -r["maps_won"], seed(r)))
            out.extend(block)
            i = j
        return out

    by_group: dict[str | None, list[dict[str, Any]]] = {}
    for r in rows.values():
        by_group.setdefault(r["group"], []).append(r)
    result = []
    for grp in sorted(by_group, key=lambda g: (g is None, g or "")):
        ordered = order(by_group[grp])
        bracket_key = ("gsl-" if mode == "gsl" else "group-") + str(grp) if grp else "swiss"
        group_done = all(m.status == "FINISHED" for m in matches if m.bracket == bracket_key) if mode != "swiss" else False
        for place, r in enumerate(ordered, start=1):
            r["place"] = place
            r.pop("opponents", None)
            if mode == "swiss":
                r["status"] = "advanced" if wins_need and r["wins"] >= wins_need else (
                    "eliminated" if loss_need and r["losses"] >= loss_need else "active")
                r["advancing"] = r["status"] == "advanced"
            else:
                r["advancing"] = bool(advance and place <= advance)
                r["status"] = ("advanced" if r["advancing"] else "eliminated") if (group_done and advance) else "active"
        result.append({"group": grp, "title": fmt.bracket_title(bracket_key), "bracket": bracket_key, "rows": ordered})
    return result


def _max_round(matches: list[TournamentMatch], settings: dict[str, Any]) -> int:
    top = max((int(m.round_no) for m in matches), default=0)
    for rounds in (settings.get("swiss_byes") or {}).values():
        if rounds:
            top = max(top, max(int(x) for x in rounds))
    return top


def _save_settings(stage: TournamentStage, settings: dict[str, Any]) -> None:
    stage.settings_json = json.dumps(settings, ensure_ascii=False)


async def _after_result(session: AsyncSession, stage: TournamentStage, fmt: StageFormat, settings: dict[str, Any]) -> None:
    """Швейцарка: когда тур сыгран целиком — создаётся следующий (пары по счёту без повторов)."""
    if not fmt.dynamic_rounds:
        return
    for _ in range(32):
        matches = await _stage_matches(session, stage.id)
        if any(m.status != "FINISHED" for m in matches):
            return
        standings = await _standings_for(session, stage, fmt, settings, matches)
        rnd = _max_round(matches, settings)
        state = {
            "rows": standings[0]["rows"] if standings else [], "round": rnd, "bo": settings.get("bo_default", 1),
            "played": {frozenset((int(m.team1_id), int(m.team2_id))) for m in matches if m.team1_id and m.team2_id},
            "byes": settings.get("swiss_byes") or {},
            "wins_to_advance": settings.get("wins_to_advance") or 3, "losses_to_eliminate": settings.get("losses_to_eliminate") or 3,
        }
        nxt = fmt.next_round(state)
        if nxt is None:
            return
        specs, bye_teams = nxt
        byes = dict(settings.get("swiss_byes") or {})
        for tid in bye_teams:
            byes.setdefault(str(tid), []).append(rnd + 1)
        settings["swiss_byes"] = byes
        _save_settings(stage, settings)
        if specs:
            top = max((int(m.match_no) for m in matches), default=0)
            for i, spec in enumerate(specs, start=1):
                row = TournamentMatch(
                    tournament_id=stage.tournament_id, stage_id=stage.id, plan_key=spec.key, bracket=spec.bracket,
                    round_no=spec.round_no, position=spec.position, match_no=top + i, round_name=spec.round_name,
                    best_of=spec.best_of, status="WAITING", team1_id=int(spec.src1.ref), team2_id=int(spec.src2.ref), version=1,
                )
                _derive(row)
                session.add(row)
            await session.flush()
            _log(session, stage_id=stage.id, match_id=0, action="swiss_round", old=None,
                 new={"round": rnd + 1, "matches": len(specs), "byes": bye_teams}, admin_user_id=None)
            return
        await session.flush()  # тур из одного BYE — считаем дальше


async def _dynamic_guard(session: AsyncSession, m: TournamentMatch, stage: TournamentStage, fmt: StageFormat,
                         settings: dict[str, Any], cascade: bool) -> None:
    """Швейцарка: правка прошлого тура пересобирает следующие туры, если в них ещё нет результатов."""
    if not fmt.dynamic_rounds:
        return
    matches = await _stage_matches(session, stage.id)
    later = [x for x in matches if int(x.round_no) > int(m.round_no)]
    later_byes = any(int(r) > int(m.round_no) for rounds in (settings.get("swiss_byes") or {}).values() for r in (rounds or []))
    if not later and not later_byes:
        return
    if any(x.status in ("LIVE", "FINISHED") for x in later):
        raise EngineError("В следующих турах уже есть результаты — сначала сбросьте их.")
    if not cascade:
        raise NeedConfirm("Следующие туры будут пересобраны по новому результату.", {"affected": [_short(x) for x in later]})
    for x in later:
        await session.delete(x)
    settings["swiss_byes"] = {t: [r for r in rounds if int(r) <= int(m.round_no)]
                              for t, rounds in (settings.get("swiss_byes") or {}).items()}
    settings["swiss_byes"] = {t: r for t, r in settings["swiss_byes"].items() if r}
    _save_settings(stage, settings)
    await session.flush()


# ---------------------------------------------------------------------------
# Ручная расстановка (5.5): кто против кого, добавление и удаление матчей
# ---------------------------------------------------------------------------

def _team_arg(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise EngineError("Команда указана неверно.") from None


async def _allowed_team_ids(session: AsyncSession, tournament_id: int, stage_id: int) -> set[int]:
    approved = set((await session.execute(
        select(TournamentApplication.team_id).where(
            TournamentApplication.tournament_id == int(tournament_id), TournamentApplication.status == ApplicationStatus.APPROVED)
    )).scalars().all())
    parts = set((await session.execute(select(StageParticipant.team_id).where(StageParticipant.stage_id == int(stage_id)))).scalars().all())
    return {int(x) for x in approved | parts}


async def _ensure_participants(session: AsyncSession, stage: TournamentStage, fmt: StageFormat, ids: list[int | None]) -> None:
    have = set((await session.execute(select(StageParticipant.team_id).where(StageParticipant.stage_id == stage.id))).scalars().all())
    for tid in ids:
        if tid and tid not in have:
            session.add(StageParticipant(stage_id=stage.id, team_id=int(tid), group_label="A" if fmt.key == "manual" else None))
            have.add(tid)


async def team_options(session: AsyncSession, m: TournamentMatch) -> list[dict[str, Any]]:
    ids = await _allowed_team_ids(session, m.tournament_id, m.stage_id)
    names = await _team_names(session, ids)
    return sorted(({"id": i, "name": names.get(i, str(i))} for i in ids), key=lambda x: x["name"].casefold())


async def set_teams(session: AsyncSession, match_id: int, team1_id: Any, team2_id: Any, *,
                    force: bool = False, admin_user_id: int | None = None) -> TournamentMatch:
    """Админ сам ставит, кто играет в матче. Предупреждает, если слот заполняется результатом
    другого матча (ручная замена будет перезаписана) или команда уже стоит в этом раунде."""
    m, stage, fmt, _ = await _load_match(session, match_id)
    if m.status in ("LIVE", "FINISHED"):
        raise EngineError("Матч уже идёт или сыгран — сначала сбросьте результат.")
    t1, t2 = _team_arg(team1_id), _team_arg(team2_id)
    if t1 and t1 == t2:
        raise EngineError("Команда не может играть сама с собой.")
    allowed = await _allowed_team_ids(session, m.tournament_id, stage.id)
    for t in (t1, t2):
        if t and t not in allowed:
            raise EngineError("Эта команда не допущена к турниру.")
    matches = await _stage_matches(session, stage.id)
    # Команда не может стоять в двух несыгранных матчах одновременно (иначе она «проходит» без победы,
    # а другая команда выпадает из сетки). В сетке это всегда ошибка, не предупреждение.
    names = await _team_names(session, {t1, t2} - {None})
    for x in matches:
        if x.id == m.id or x.status == "FINISHED":
            continue
        if fmt.table and (x.bracket, x.round_no) != (m.bracket, m.round_no):
            continue  # в таблице команда играет во многих турах — дубль ищем только в этом туре
        for t in (t1, t2):
            if t and t in (x.team1_id, x.team2_id) and t not in (m.team1_id, m.team2_id):
                raise EngineError(f"«{names.get(t, t)}» уже стоит в матче {x.match_no} — сначала уберите её оттуда.")
    warnings: list[str] = []
    for x in matches:
        for slot, cur in ((1, t1), (2, t2)):
            fed = (x.winner_to_match_id == m.id and x.winner_to_slot == slot) or (x.loser_to_match_id == m.id and x.loser_to_slot == slot)
            if fed and cur != getattr(m, f"team{slot}_id") and x.status != "FINISHED":
                warnings.append(f"Место {slot} заполнит результат матча {x.match_no} — ручная замена будет перезаписана.")
        if x.id != m.id and x.status != "FINISHED" and x.bracket == m.bracket and x.round_no == m.round_no:
            both = ({t1, t2} - {None}) & ({x.team1_id, x.team2_id} - {None})
            if both:
                warnings.append(f"Команда уже стоит в матче {x.match_no} этого раунда.")
    if warnings and not force:
        raise NeedConfirm("Проверьте расстановку.", {"warnings": warnings})
    old = _snapshot(m)
    m.team1_id, m.team2_id = t1, t2
    if m.status == "POSTPONED" and not (t1 and t2):
        m.status = "WAITING"
    _derive(m)
    await _ensure_participants(session, stage, fmt, [t1, t2])
    _bump(m, admin_user_id)
    _log(session, stage_id=stage.id, match_id=m.id, action="teams", old=old, new=_snapshot(m), admin_user_id=admin_user_id)
    return m


async def add_manual_match(session: AsyncSession, tournament: Tournament, *, team1_id: Any, team2_id: Any, best_of: Any = 1,
                           when: Any = None, round_no: Any = None, admin_user_id: int | None = None) -> TournamentMatch:
    stages = await get_stages(session, tournament.id)
    stage = stages[0] if stages else None
    if stage is None or stage.format != "manual":
        raise EngineError("Добавлять матчи можно в формате «Ручные матчи» — выберите его и нажмите «Сгенерировать».")
    fmt = FORMATS["manual"]
    t1, t2 = _team_arg(team1_id), _team_arg(team2_id)
    if not t1 or not t2:
        raise EngineError("Выберите обе команды.")
    if t1 == t2:
        raise EngineError("Команда не может играть сама с собой.")
    allowed = await _allowed_team_ids(session, tournament.id, stage.id)
    if t1 not in allowed or t2 not in allowed:
        raise EngineError("Обе команды должны быть допущены к турниру.")
    try:
        bo = validate_bo(best_of, allow_even=True)
        at = timeutil.parse_local(when) if when else None
    except (SeriesError, ValueError) as exc:
        raise EngineError(str(exc)) from None
    matches = await _stage_matches(session, stage.id)
    try:
        rnd = int(round_no) if round_no not in (None, "", 0, "0") else max((int(x.round_no) for x in matches), default=1)
    except (TypeError, ValueError):
        raise EngineError("Номер тура — целое число.") from None
    top = max((int(x.match_no) for x in matches), default=0)
    row = TournamentMatch(
        tournament_id=int(tournament.id), stage_id=stage.id, plan_key=f"M{top + 1}", bracket="group-A", round_no=max(1, rnd),
        position=sum(1 for x in matches if int(x.round_no) == rnd) + 1, match_no=top + 1, round_name=f"Тур {max(1, rnd)}",
        best_of=bo, team1_id=t1, team2_id=t2, scheduled_at=at, status="WAITING", version=1, updated_by=admin_user_id,
    )
    _derive(row)
    session.add(row)
    await _ensure_participants(session, stage, fmt, [t1, t2])
    stage.status = "ACTIVE"
    await session.flush()
    _log(session, stage_id=stage.id, match_id=row.id, action="add_match", old=None, new=_snapshot(row), admin_user_id=admin_user_id)
    return row


async def delete_match(session: AsyncSession, match_id: int, *, admin_user_id: int | None = None) -> None:
    m, stage, fmt, _ = await _load_match(session, match_id)
    if fmt.key != "manual":
        raise EngineError("Удалять можно только матчи формата «Ручные матчи». В сетке меняйте команды матча.")
    if m.status in ("LIVE", "FINISHED"):
        raise EngineError("Матч идёт или сыгран — сначала сбросьте результат.")
    _log(session, stage_id=stage.id, match_id=m.id, action="delete_match", old=_snapshot(m), new=None, admin_user_id=admin_user_id)
    await session.delete(m)
    await session.flush()


# ---------------------------------------------------------------------------
# Расстановка пар (5.11): все «ручные» места этапа на одном экране, сохранение разом
# ---------------------------------------------------------------------------

def _pair_scope(x: TournamentMatch, table: bool) -> Any:
    """Где команда может стоять только один раз: в сетке — во всём этапе, в таблице — в одном туре."""
    return (x.bracket, int(x.round_no)) if table else "stage"


async def pairs_view(session: AsyncSession, tournament: Tournament) -> dict[str, Any]:
    """Матчи текущего этапа, где место заполняется вручную (а не победителем/проигравшим другого матча)."""
    stages = await get_stages(session, tournament.id)
    if not stages:
        raise EngineError("Сначала сгенерируйте сетку.")
    stage = stages[-1]
    fmt = FORMATS.get(stage.format)
    table = bool(fmt and fmt.table)
    matches, seeds, names = await _view_context(session, stage)
    rev = _reverse_links(matches)
    allowed = await _allowed_team_ids(session, tournament.id, stage.id)
    names.update(await _team_names(session, allowed - set(names)))
    rows, fixed = [], []
    for x in sorted(matches, key=lambda y: y.match_no):
        open_match = x.status not in ("LIVE", "FINISHED")
        slots = []
        for slot in (1, 2):
            tid = getattr(x, f"team{slot}_id")
            editable = open_match and (int(x.id), slot) not in rev
            slots.append({"slot": slot, "team_id": int(tid) if tid else None, "editable": editable,
                          "source": rev.get((int(x.id), slot))})
            if tid and not editable:
                fixed.append({"team_id": int(tid), "scope": _pair_scope(x, table)})
        if any(s["editable"] for s in slots):
            rows.append({"id": int(x.id), "no": int(x.match_no), "round_name": x.round_name, "best_of": int(x.best_of),
                         "bracket_title": fmt.bracket_title(x.bracket) if fmt else x.bracket,
                         "scope": "%s:%s" % _pair_scope(x, table) if table else "stage", "slots": slots})
    teams = sorted(({"id": i, "name": names.get(i, str(i)), "seed": seeds.get(i)} for i in allowed),
                   key=lambda t: (t["seed"] is None, t["seed"] or 0, t["name"].casefold()))
    busy = sorted({f["team_id"] for f in fixed}) if not table else []
    return {"stage_id": int(stage.id), "format_title": fmt.title if fmt else stage.format, "table": table,
            "matches": rows, "teams": teams, "busy": busy}


async def set_pairs(session: AsyncSession, tournament: Tournament, pairs: Any, *, admin_user_id: int | None = None) -> int:
    """Сохраняет расстановку разом. Проверяется итог целиком, поэтому обмен команд местами — одно действие."""
    stages = await get_stages(session, tournament.id)
    if not stages:
        raise EngineError("Сначала сгенерируйте сетку.")
    if not isinstance(pairs, list) or not pairs:
        raise EngineError("Нет изменений.")
    stage = stages[-1]
    fmt = FORMATS.get(stage.format)
    table = bool(fmt and fmt.table)
    matches = await _stage_matches(session, stage.id)
    by_id = {int(x.id): x for x in matches}
    rev = _reverse_links(matches)
    allowed = await _allowed_team_ids(session, tournament.id, stage.id)
    new = {int(x.id): [x.team1_id, x.team2_id] for x in matches}
    for p in pairs:
        try:
            x = by_id.get(int(p.get("match_id")))
        except (TypeError, ValueError, AttributeError):
            raise EngineError("Матч указан неверно.") from None
        if x is None:
            raise EngineError("Матч не из текущего этапа турнира.")
        for slot in (1, 2):
            key = f"team{slot}_id"
            if key not in p:
                continue
            val = _team_arg(p[key])
            if val == getattr(x, key):
                continue
            if x.status in ("LIVE", "FINISHED"):
                raise EngineError(f"Матч {x.match_no} уже идёт или сыгран — сначала сбросьте результат.")
            if (int(x.id), slot) in rev:
                raise EngineError(f"Матч {x.match_no}: это место займёт {rev[(int(x.id), slot)].lower()}.")
            if val and val not in allowed:
                raise EngineError("Команда не допущена к турниру.")
            new[int(x.id)][slot - 1] = val
    changed = [x for x in matches if new[int(x.id)] != [x.team1_id, x.team2_id]]
    if not changed:
        return 0
    names = await _team_names(session, allowed)
    where: dict[tuple[Any, int], list[int]] = {}
    for x in matches:
        a, b = new[int(x.id)]
        if a and a == b:
            raise EngineError(f"Матч {x.match_no}: команда не может играть сама с собой.")
        for t in (a, b):
            if t:
                where.setdefault((_pair_scope(x, table), int(t)), []).append(int(x.match_no))
    for x in changed:
        for slot, t in ((1, new[int(x.id)][0]), (2, new[int(x.id)][1])):
            if (int(x.id), slot) in rev:
                continue  # место заполнено результатом другого матча — команда законно стоит и там, и там
            nos = where.get((_pair_scope(x, table), int(t)), []) if t else []
            if len(nos) > 1:
                raise EngineError(f"«{names.get(int(t), t)}» стоит сразу в матчах {', '.join(map(str, sorted(nos)))}.")
    for x in changed:
        old = _snapshot(x)
        x.team1_id, x.team2_id = new[int(x.id)]
        if x.status == "POSTPONED" and not (x.team1_id and x.team2_id):
            x.status = "WAITING"
        _derive(x)
        _bump(x, admin_user_id)
        _log(session, stage_id=stage.id, match_id=x.id, action="teams", old=old, new=_snapshot(x), admin_user_id=admin_user_id)
    await _ensure_participants(session, stage, fmt, [t for x in changed for t in new[int(x.id)]])
    await session.flush()
    return len(changed)


# ---------------------------------------------------------------------------
# Автодобор пар (5.12): после регистрации все без соперника встают друг против друга,
# BYE — только тем, кому соперника не хватило
# ---------------------------------------------------------------------------

def _esc(text: Any) -> str:
    from html import escape

    return escape(str(text if text is not None else ""), quote=False)


async def _captain_tg(session: AsyncSession, team_ids: set[int]) -> dict[int, int]:
    from database.models import User

    if not team_ids:
        return {}
    rows = (await session.execute(
        select(Team.id, User.telegram_id).join(User, User.id == Team.captain_user_id).where(Team.id.in_(team_ids))
    )).all()
    return {int(t): int(tg) for t, tg in rows if tg}


def _side_text(m: TournamentMatch, slot: int, names: dict[int, str], rev: dict[tuple[int, int], str]) -> str:
    tid = getattr(m, f"team{slot}_id")
    return names.get(int(tid), "—") if tid else rev.get((int(m.id), slot), "Ожидается")


async def _autopair_report(session: AsyncSession, tournament: Tournament, stage: TournamentStage, out: dict[str, Any],
                           pairs: list[list[int]], byes: list[int]) -> None:
    """Номера матчей новых пар и BYE, тексты для админов и капитанов."""
    matches = await _stage_matches(session, stage.id)
    rev = _reverse_links(matches)
    names = await _team_names(session, {t for x in matches for t in (x.team1_id, x.team2_id) if t} | set(byes))
    where = {int(t): x for x in matches for t in (x.team1_id, x.team2_id) if t and x.status not in ("FINISHED",)}
    caps = await _captain_tg(session, {t for p in pairs for t in p} | set(byes))
    lines, msgs = [], []
    for a, b in pairs:
        m = where.get(int(a))
        no = f"матч {m.match_no}, {m.round_name}" if m else "матч в сетке"
        out["pairs"].append({"team1": names.get(int(a)), "team2": names.get(int(b)), "match_no": m.match_no if m else None})
        lines.append(f"• {_esc(names.get(int(a)))} — {_esc(names.get(int(b)))} ({no})")
        for me, opp in ((a, b), (b, a)):
            if caps.get(int(me)):
                msgs.append({"telegram_id": caps[int(me)], "text": (
                    f"Регистрация на «{_esc(tournament.name)}» закрыта. Ваш соперник: <b>{_esc(names.get(int(opp)))}</b> ({no}).\n"
                    "Время матча назначит организатор — следите за уведомлениями и сеткой в Mini App.")})
    for b in byes:
        m = where.get(int(b))
        if m is not None:
            slot = 1 if m.team1_id == b else 2
            vs = _side_text(m, 2 if slot == 1 else 1, names, rev)
            nxt = f"следующий матч — {m.round_name} (матч {m.match_no}), соперник: {vs}"
        else:
            nxt = "следующий матч появится в сетке"
        out["byes"].append({"team": names.get(int(b)), "match_no": m.match_no if m else None})
        lines.append(f"• BYE: {_esc(names.get(int(b)))} — {_esc(nxt)}")
        if caps.get(int(b)):
            msgs.append({"telegram_id": caps[int(b)], "text": (
                f"Регистрация на «{_esc(tournament.name)}» закрыта. Соперника для вашей команды не хватило — "
                f"у вас <b>автопроход (BYE)</b> первого раунда, {_esc(nxt)}.")})
    out["messages"] = msgs
    head = out.get("text") or "Пары добраны."
    out["admin_text"] = f"<b>{_esc(tournament.name)}</b>: {_esc(head)}" + ("\n" + "\n".join(lines) if lines else "")


async def _autopair_fill(session: AsyncSession, tournament: Tournament, stage: TournamentStage, matches: list[TournamentMatch],
                         unplaced: list[int], out: dict[str, Any], *, dry_run: bool, admin_user_id: int | None) -> dict[str, Any]:
    """Сетку пересобрать нельзя (зафиксирована или игры начались): ставим новые команды в свободные места,
    сначала против одиноких команд, затем парами в пустые матчи, лишних — на автопроход (свободное место BYE)."""
    rev = _reverse_links(matches)
    fmt = FORMATS.get(stage.format)
    open_ = [x for x in sorted(matches, key=lambda y: (y.round_no, y.position, y.match_no)) if x.status not in ("LIVE", "FINISHED")]
    entry = lambda x, s: (int(x.id), s) not in rev  # noqa: E731
    lone = [(x, s) for x in open_ if entry(x, 1) and entry(x, 2) for s in (1, 2)
            if not getattr(x, f"team{s}_id") and getattr(x, f"team{3 - s}_id")]
    empty = [x for x in open_ if entry(x, 1) and entry(x, 2) and not x.team1_id and not x.team2_id]
    bye_slots = [(x, s) for x in open_ for s in (1, 2) if entry(x, s) and not entry(x, 3 - s) and not getattr(x, f"team{s}_id")]
    queue = list(unplaced)
    plan: list[tuple[TournamentMatch, int, int]] = []
    pairs, byes = [], []
    for x, s in lone:
        if queue:
            t = queue.pop(0)
            plan.append((x, s, t))
            pairs.append([int(getattr(x, f"team{3 - s}_id")), t])
    for x in empty:
        if len(queue) >= 2:
            a, b = queue.pop(0), queue.pop(0)
            plan += [(x, 1, a), (x, 2, b)]
            pairs.append([a, b])
    for x, s in bye_slots:
        if queue:
            t = queue.pop(0)
            plan.append((x, s, t))
            byes.append(t)
    names = await _team_names(session, set(unplaced))
    out["left"] = [names.get(t, str(t)) for t in queue]
    if not plan:
        out.update(action="none", text="Свободных мест в сетке нет — " + ("не поместились: " + ", ".join(out["left"]) if queue else "добирать некого") + ".")
        return out
    out.update(action="filled", text=f"Новые команды поставлены в свободные места сетки ({out['why']}).")
    if dry_run:
        return out
    for x, s, t in plan:
        old = _snapshot(x)
        setattr(x, f"team{s}_id", t)
        _derive(x)
        _bump(x, admin_user_id)
        _log(session, stage_id=stage.id, match_id=x.id, action="autopair", old=old, new=_snapshot(x), admin_user_id=admin_user_id)
    await _ensure_participants(session, stage, fmt, [t for _, _, t in plan])
    await session.flush()
    await _autopair_report(session, tournament, stage, out, pairs, byes)
    return out


async def autopair_leftovers(session: AsyncSession, tournament: Tournament, *, dry_run: bool = False,
                             admin_user_id: int | None = None) -> dict[str, Any]:
    """Все команды без соперника (на автопроходе, одни в матче, не попавшие в сетку) встают друг против друга —
    сильный против слабого; при нечётном числе сильнейшая получает BYE. Готовые пары, их время и BO сохраняются."""
    out: dict[str, Any] = {"action": "none", "text": "", "pairs": [], "byes": [], "left": [], "messages": [], "admin_text": ""}
    stages = await get_stages(session, tournament.id)
    if not stages:
        out.update(action="skipped", text="Сетки ещё нет. Сгенерируйте её с посевом «Все играют в 1-м раунде».")
        return out
    if len(stages) > 1:
        out.update(action="skipped", text="Плей-офф уже сформирован — добирать пары некуда.")
        return out
    stage = stages[0]
    fmt = FORMATS.get(stage.format)
    if fmt is None:
        out.update(action="skipped", text="Неизвестный формат сетки.")
        return out
    teams = await approved_teams(session, tournament.id)
    by_id = {int(t["id"]): t for t in teams}
    matches = await _stage_matches(session, stage.id)
    placed = {int(t) for x in matches for t in (x.team1_id, x.team2_id) if t}
    unplaced = [int(t["id"]) for t in seeding.sort_by_skill(teams) if int(t["id"]) not in placed]
    try:
        await _ensure_regenerable(session, stage)
        rebuild_ok, why = True, ""
    except EngineError as exc:
        rebuild_ok, why = False, str(exc).rstrip(".")
    out["why"] = why

    if fmt.key == "manual":
        names = await _team_names(session, set(unplaced))
        pairs = [[unplaced[i], unplaced[i + 1]] for i in range(0, len(unplaced) - 1, 2)]
        out["left"] = [names[t] for t in unplaced[len(pairs) * 2:]]
        if not pairs:
            out["text"] = "Добирать некого." if not unplaced else f"Без пары осталась одна команда: {out['left'][0]} — поставьте её вручную."
            return out
        out.update(action="filled", text="Добавлены матчи для команд без соперника.")
        if not dry_run:
            for a, b in pairs:
                await add_manual_match(session, tournament, team1_id=a, team2_id=b, best_of=_settings(stage).get("bo_default", 1),
                                       admin_user_id=admin_user_id)
            await _autopair_report(session, tournament, stage, out, pairs, [])
        return out

    if not fmt.uses_slots:
        if not unplaced:
            out["text"] = "Все команды уже в таблице."
            return out
        if not rebuild_ok:
            names = await _team_names(session, set(unplaced))
            out.update(left=[names[t] for t in unplaced], text=f"Таблицу пересобрать нельзя ({why}). Не попали: " + ", ".join(names[t] for t in unplaced) + ".")
            return out
        out.update(action="rebuilt", text="Таблица пересобрана — новые команды добавлены в расписание туров.")
        if not dry_run:
            await generate(session, tournament, fmt_key=stage.format, settings=_settings(stage), admin_user_id=admin_user_id)
            await session.flush()
            out["admin_text"] = f"<b>{_esc(tournament.name)}</b>: {_esc(out['text'])}"
        return out

    # Сетка (single/double elimination).
    if not rebuild_ok:
        return await _autopair_fill(session, tournament, stage, matches, unplaced, out, dry_run=dry_run, admin_user_id=admin_user_id)
    rev = _reverse_links(matches)
    kept: list[list[int]] = []
    pool: list[int] = []
    on_bye: set[int] = set()
    seen: set[int] = set()  # команда, по ошибке стоящая в двух местах, учитывается один раз
    for x in sorted(matches, key=lambda y: (y.round_no, y.position, y.match_no)):
        entry = [(int(x.id), s) not in rev for s in (1, 2)]
        ids = [int(t) for t in (x.team1_id, x.team2_id) if t and int(t) in by_id and int(t) not in seen]
        if all(entry):
            if len(ids) == 2:
                kept.append(ids)
            else:
                pool += ids
            seen.update(ids)
        elif any(entry):
            t = x.team1_id if entry[0] else x.team2_id
            if t and int(t) in by_id and int(t) not in seen:
                pool.append(int(t))
                on_bye.add(int(t))
                seen.add(int(t))
    pool += [t for t in unplaced if t not in seen]
    if not pool or (len(pool) == 1 and pool[0] in on_bye):
        out["text"] = "У всех команд уже есть соперник." if not pool else "Все в парах, одна команда на автопроходе — так и должно быть."
        return out
    if 2 * len(kept) + len(pool) < 2:
        out.update(action="skipped", text="Для сетки нужно минимум 2 команды.")
        return out
    new_pairs, bye = seeding.everyone_pairs([int(t["id"]) for t in seeding.sort_by_skill([by_id[t] for t in pool])])
    layout = seeding.layout_pairs(kept + new_pairs, bye)
    out.update(action="rebuilt", text=(f"Без соперника было {len(pool)} — "
                                       + (f"составлено пар: {len(new_pairs)}" if new_pairs else "новых пар нет")
                                       + (", одна команда на автопроходе (BYE)" if bye else "") + ". Готовые пары, их время и BO сохранены."))
    if dry_run:
        names = await _team_names(session, set(pool))
        out["pairs"] = [{"team1": names.get(a), "team2": names.get(b), "match_no": None} for a, b in new_pairs]
        out["byes"] = [{"team": names.get(b), "match_no": None} for b in bye]
        return out
    keep = {frozenset((int(x.team1_id), int(x.team2_id))): (x.scheduled_at, x.best_of) for x in matches if x.team1_id and x.team2_id}
    old_seeds = {int(t): int(s) for t, s in (await session.execute(
        select(StageParticipant.team_id, StageParticipant.seed).where(StageParticipant.stage_id == stage.id)
    )).all() if s is not None}
    seeds: dict[int, int] = {}
    entrants = [int(t) for p in layout for t in p if t]
    for t in entrants:
        if t in old_seeds and old_seeds[t] not in seeds.values():
            seeds[t] = old_seeds[t]
    top = max(seeds.values(), default=0)
    for t in entrants:
        if t not in seeds:
            top += 1
            seeds[t] = top
    stage = await generate(session, tournament, fmt_key=stage.format, seeding_mode="pairs", settings=_settings(stage), pairs=layout,
                           seeds=seeds, admin_user_id=admin_user_id, allow_empty_pairs=True,
                           extra_settings={"autopair_at": timeutil.now_utc().isoformat()})
    await session.flush()
    for x in await _stage_matches(session, stage.id):
        saved = keep.get(frozenset((int(x.team1_id), int(x.team2_id)))) if x.team1_id and x.team2_id else None
        if saved:
            x.scheduled_at, x.best_of = saved[0], int(saved[1] or x.best_of)
            _derive(x)
    await session.flush()
    await _autopair_report(session, tournament, stage, out, new_pairs, bye)
    return out


# ---------------------------------------------------------------------------
# Действия с матчем
# ---------------------------------------------------------------------------

async def set_result(
    session: AsyncSession,
    match_id: int,
    *,
    score1: Any = None,
    score2: Any = None,
    forfeit_winner: int | None = None,
    cascade: bool = False,
    expected_version: int | None = None,
    admin_user_id: int | None = None,
) -> dict[str, Any]:
    m, stage, fmt, settings = await _load_match(session, match_id)
    if expected_version is not None and int(expected_version) != int(m.version):
        raise EngineError("Матч уже изменил другой администратор — обновите экран.", status=409)
    if not (m.team1_id and m.team2_id):
        raise EngineError("Команды этого матча ещё не определены.")
    try:
        if forfeit_winner in (1, 2, "1", "2"):
            slot = int(forfeit_winner)
            s1, s2 = forfeit_score(m.best_of, slot)
            result_type = "forfeit"
        else:
            slot = validate_score(m.best_of, score1, score2, allow_draw=fmt.allow_draw)
            s1, s2 = int(score1), int(score2)
            result_type = "normal" if slot else "draw"
    except SeriesError as exc:
        raise EngineError(str(exc)) from None
    new_winner = None if slot == 0 else (m.team1_id if slot == 1 else m.team2_id)
    old = _snapshot(m)
    was_finished = m.status == "FINISHED"
    winner_changed = (not was_finished) or m.winner_team_id != new_winner
    affected: list[TournamentMatch] = []
    if was_finished:
        await _dynamic_guard(session, m, stage, fmt, settings, cascade)
    if was_finished and m.winner_team_id is not None and m.winner_team_id != new_winner:
        await _collect_affected(session, m, fmt, affected)
        if affected and not cascade:
            raise NeedConfirm(
                "Смена победителя сбросит результаты или составы следующих матчей.",
                {"affected": [_short(x) for x in affected]},
            )
        await _retract(session, m, fmt, admin_user_id)
    now = timeutil.now_utc()
    m.score1, m.score2 = s1, s2
    m.winner_team_id = new_winner
    m.result_type = result_type
    m.status = "FINISHED"
    m.finished_at = now
    m.started_at = m.started_at or now
    if winner_changed and new_winner is not None:
        await _advance(session, m, fmt, settings)
    _bump(m, admin_user_id)
    _log(session, stage_id=stage.id, match_id=m.id, action="result_correct" if was_finished else "result",
         old=old, new=_snapshot(m), admin_user_id=admin_user_id)
    await session.flush()
    await _after_result(session, stage, fmt, settings)
    await _update_stage_status(session, stage)
    await _auto_forfeits(session, stage, admin_user_id)
    return {"match": m, "old": old, "winner_changed": winner_changed, "affected": [_short(x) for x in affected],
            "first_result": not was_finished}


async def _auto_forfeits(session: AsyncSession, stage: TournamentStage, admin_user_id: int | None) -> list[TournamentMatch]:
    """5.13: снявшаяся команда проигрывает технически каждый свой матч, как только у неё появляется соперник."""
    withdrawn = {int(x) for x in (_settings(stage).get("withdrawn") or [])}
    done: list[TournamentMatch] = []
    if not withdrawn:
        return done
    for _ in range(256):
        nxt = next((x for x in await _stage_matches(session, stage.id)
                    if x.status != "FINISHED" and x.team1_id and x.team2_id
                    and (int(x.team1_id) in withdrawn or int(x.team2_id) in withdrawn)), None)
        if nxt is None:
            break
        loser_first = int(nxt.team1_id) in withdrawn
        if loser_first and int(nxt.team2_id) in withdrawn:
            loser_first = True  # обе снялись — проходит любая, дальше она проиграет следующему сопернику
        m, _, fmt, settings = await _load_match(session, nxt.id)
        try:
            slot = 2 if loser_first else 1
            s1, s2 = forfeit_score(m.best_of, slot)
        except SeriesError:
            break
        old = _snapshot(m)
        now = timeutil.now_utc()
        m.score1, m.score2 = s1, s2
        m.winner_team_id = m.team2_id if slot == 2 else m.team1_id
        m.result_type = "forfeit"
        m.status = "FINISHED"
        m.finished_at = now
        m.started_at = m.started_at or now
        await _advance(session, m, fmt, settings)
        _bump(m, admin_user_id)
        _log(session, stage_id=stage.id, match_id=m.id, action="withdraw_forfeit", old=old, new=_snapshot(m), admin_user_id=admin_user_id)
        await session.flush()
        await _after_result(session, stage, fmt, settings)
        done.append(m)
    await _update_stage_status(session, stage)
    return done


async def withdraw_team(session: AsyncSession, tournament: Tournament, team_id: int, *,
                        admin_user_id: int | None = None) -> dict[str, Any]:
    """5.13: команда снимается с турнира (сама или её снимает/расформировывает организатор).

    До начала игр — команда просто убирается из сетки (после закрытия регистрации сразу добираются пары).
    Если игры уже идут — каждый её несыгранный матч засчитывается сопернику техническим поражением,
    в том числе матчи, где соперник определится позже (нижняя сетка, следующий тур)."""
    team_id = int(team_id)
    out: dict[str, Any] = {"forfeits": [], "removed": 0, "waiting": [], "autopair": None}
    for stage in await get_stages(session, tournament.id):
        fmt = FORMATS.get(stage.format)
        matches = await _stage_matches(session, stage.id)
        mine = [x for x in matches if team_id in (x.team1_id, x.team2_id) and x.status != "FINISHED"]
        started = any(x.status in ("LIVE", "FINISHED") for x in matches)
        if not started:
            if fmt is not None and fmt.key == "manual":
                for x in mine:
                    _log(session, stage_id=stage.id, match_id=x.id, action="delete_match", old=_snapshot(x), new=None, admin_user_id=admin_user_id)
                    await session.delete(x)
                    out["removed"] += 1
            elif fmt is not None and not fmt.uses_slots and mine:
                try:
                    await _ensure_regenerable(session, stage)
                    await session.execute(delete(StageParticipant).where(StageParticipant.stage_id == stage.id, StageParticipant.team_id == team_id))
                    await session.flush()
                    await generate(session, tournament, fmt_key=stage.format, settings=_settings(stage), admin_user_id=admin_user_id)
                    out["removed"] += len(mine)
                except EngineError:
                    pass  # зафиксированная таблица — ниже засчитаем технические поражения
                else:
                    continue
            else:
                for x in mine:
                    old = _snapshot(x)
                    if x.team1_id == team_id:
                        x.team1_id = None
                    if x.team2_id == team_id:
                        x.team2_id = None
                    if x.status == "POSTPONED":
                        x.status = "WAITING"
                    _derive(x)
                    _bump(x, admin_user_id)
                    _log(session, stage_id=stage.id, match_id=x.id, action="withdraw", old=old, new=_snapshot(x), admin_user_id=admin_user_id)
                    out["removed"] += 1
            if fmt is None or fmt.key == "manual" or fmt.uses_slots:
                await session.execute(delete(StageParticipant).where(StageParticipant.stage_id == stage.id, StageParticipant.team_id == team_id))
                await session.flush()
                continue
        settings = _settings(stage)
        withdrawn = sorted({*(int(x) for x in settings.get("withdrawn") or []), team_id})
        settings["withdrawn"] = withdrawn
        _save_settings(stage, settings)
        await session.flush()
        names = await _team_names(session, {t for x in mine for t in (x.team1_id, x.team2_id) if t})
        for x in await _auto_forfeits(session, stage, admin_user_id):
            opp = x.team1_id if x.team2_id == team_id else x.team2_id
            if team_id in (x.team1_id, x.team2_id) and opp:
                out["forfeits"].append({"match_no": x.match_no, "round_name": x.round_name, "opponent_id": int(opp),
                                        "opponent": names.get(int(opp)) or (await _team_names(session, {int(opp)})).get(int(opp))})
        out["waiting"] = [x.match_no for x in await _stage_matches(session, stage.id)
                          if x.status != "FINISHED" and team_id in (x.team1_id, x.team2_id)]
    await session.flush()
    if out["removed"] and tournament.status.value != "REGISTRATION":
        out["autopair"] = await autopair_leftovers(session, tournament, admin_user_id=admin_user_id)
    return out


async def reset_result(session: AsyncSession, match_id: int, *, cascade: bool = False, admin_user_id: int | None = None) -> dict[str, Any]:
    m, stage, fmt, settings = await _load_match(session, match_id)
    if m.status != "FINISHED":
        raise EngineError("У матча нет результата.")
    await _dynamic_guard(session, m, stage, fmt, settings, cascade)
    affected: list[TournamentMatch] = []
    await _collect_affected(session, m, fmt, affected)
    if affected and not cascade:
        raise NeedConfirm("Сброс затронет следующие матчи.", {"affected": [_short(x) for x in affected]})
    old = _snapshot(m)
    await _retract(session, m, fmt, admin_user_id)
    _clear_result(m)
    _bump(m, admin_user_id)
    _log(session, stage_id=stage.id, match_id=m.id, action="result_reset", old=old, new=_snapshot(m), admin_user_id=admin_user_id)
    await session.flush()
    await _update_stage_status(session, stage)
    return {"match": m, "affected": [_short(x) for x in affected]}


async def set_status(session: AsyncSession, match_id: int, status: str, *, admin_user_id: int | None = None) -> TournamentMatch:
    m, stage, _, _ = await _load_match(session, match_id)
    status = str(status or "").strip().upper()
    if status == "FINISHED":
        raise EngineError("Статус «Завершён» ставится вводом результата.")
    if status not in ("WAITING", "SCHEDULED", "LIVE", "POSTPONED"):
        raise EngineError("Неизвестный статус матча.")
    if m.status == "FINISHED":
        raise EngineError("Матч завершён. Чтобы изменить его, сбросьте результат.")
    if status in ("LIVE", "SCHEDULED") and not (m.team1_id and m.team2_id):
        raise EngineError("Команды этого матча ещё не определены.")
    if status == "SCHEDULED" and not m.scheduled_at:
        raise EngineError("Сначала назначьте время матча.")
    old = _snapshot(m)
    if status == "WAITING":
        m.status = "WAITING"
        m.started_at = None
        _derive(m)
    else:
        m.status = status
        if status == "LIVE":
            m.started_at = timeutil.now_utc()
    _bump(m, admin_user_id)
    _log(session, stage_id=stage.id, match_id=m.id, action="status", old=old, new=_snapshot(m), admin_user_id=admin_user_id)
    return m


async def set_best_of(session: AsyncSession, match_id: int, best_of: Any, *, admin_user_id: int | None = None) -> TournamentMatch:
    m, stage, fmt, _ = await _load_match(session, match_id)
    if m.status == "FINISHED":
        raise EngineError("Матч завершён: формат серии меняется только после сброса результата.")
    try:
        bo = validate_bo(best_of, allow_even=fmt.allow_draw)
    except SeriesError as exc:
        raise EngineError(str(exc)) from None
    old = _snapshot(m)
    m.best_of = bo
    _bump(m, admin_user_id)
    _log(session, stage_id=stage.id, match_id=m.id, action="best_of", old=old, new=_snapshot(m), admin_user_id=admin_user_id)
    return m


def _duration(m: TournamentMatch) -> timedelta:
    return timedelta(minutes=GAME_MINUTES * int(m.best_of or 1))


async def schedule_warnings(session: AsyncSession, m: TournamentMatch, at) -> list[str]:
    """Мягкие проверки расписания: сохраняются только после подтверждения админа."""
    warnings: list[str] = []
    now = timeutil.now_utc()
    if at < now - timedelta(minutes=5):
        warnings.append("Это время уже прошло.")
    tournament = await session.get(Tournament, m.tournament_id)
    start = timeutil.as_utc(tournament.start_date) if tournament else None
    if start and at < start - timedelta(minutes=1):
        warnings.append(f"Раньше старта турнира ({timeutil.fmt_local(start, label=True)}).")
    matches = await _stage_matches(session, m.stage_id)
    names = await _team_names(session, {m.team1_id, m.team2_id})
    for src in matches:
        if src.id == m.id or src.status == "FINISHED":
            continue
        if src.winner_to_match_id == m.id or src.loser_to_match_id == m.id:
            src_at = timeutil.as_utc(src.scheduled_at)
            if src_at and at < src_at + _duration(src):
                warnings.append(
                    f"Матч {src.match_no}, откуда придёт команда, назначен на {timeutil.fmt_local(src_at)} "
                    f"(BO{src.best_of}) и может ещё не закончиться."
                )
    for target_id in (m.winner_to_match_id, m.loser_to_match_id):
        target = next((x for x in matches if x.id == target_id), None)
        t_at = timeutil.as_utc(target.scheduled_at) if target else None
        if target and t_at and target.status != "FINISHED" and t_at < at + _duration(m):
            warnings.append(f"Следующий матч {target.match_no} назначен на {timeutil.fmt_local(t_at)} — раньше, чем закончится этот.")
    mine = {m.team1_id, m.team2_id} - {None}
    for other in matches:
        if other.id == m.id or other.status == "FINISHED" or not other.scheduled_at:
            continue
        common = mine & ({other.team1_id, other.team2_id} - {None})
        if not common:
            continue
        o_at = timeutil.as_utc(other.scheduled_at)
        if at < o_at + _duration(other) and o_at < at + _duration(m):
            for tid in common:
                warnings.append(f"У команды «{names.get(tid, tid)}» пересечение с матчем {other.match_no} ({timeutil.fmt_local(o_at)}).")
    return warnings


async def set_schedule(
    session: AsyncSession,
    match_id: int,
    when: Any,
    *,
    postpone: bool = False,
    force: bool = False,
    admin_user_id: int | None = None,
) -> dict[str, Any]:
    m, stage, _, _ = await _load_match(session, match_id)
    if m.status == "FINISHED":
        raise EngineError("Матч завершён — время менять нельзя.")
    try:
        new_at = timeutil.parse_local(when)
    except ValueError as exc:
        raise EngineError(str(exc)) from None
    old = _snapshot(m)
    prev_at = timeutil.as_utc(m.scheduled_at)
    if new_at is None:
        if postpone:
            m.scheduled_at = None
            m.status = "POSTPONED"
        else:
            m.scheduled_at = None
            if m.status in ("SCHEDULED", "POSTPONED"):
                m.status = "WAITING"
            _derive(m)
    else:
        warnings = await schedule_warnings(session, m, new_at)
        if warnings and not force:
            raise NeedConfirm("Проверьте расписание.", {"warnings": warnings})
        m.scheduled_at = new_at
        if postpone and m.status != "LIVE":
            m.status = "POSTPONED"
        else:
            _derive(m)
    _bump(m, admin_user_id)
    action = "postpone" if m.status == "POSTPONED" else ("reschedule" if prev_at else "schedule")
    _log(session, stage_id=stage.id, match_id=m.id, action=action, old=old, new=_snapshot(m), admin_user_id=admin_user_id)
    return {"match": m, "previous": prev_at, "current": new_at, "postponed": m.status == "POSTPONED"}


# ---------------------------------------------------------------------------
# Выдача
# ---------------------------------------------------------------------------

AvatarFn = Callable[[int], "str | None"]


def _reverse_links(matches: list[TournamentMatch]) -> dict[tuple[int, int], str]:
    rev: dict[tuple[int, int], str] = {}
    for x in matches:
        if x.winner_to_match_id:
            rev[(x.winner_to_match_id, int(x.winner_to_slot or 1))] = f"Победитель матча {x.match_no}"
        if x.loser_to_match_id:
            rev[(x.loser_to_match_id, int(x.loser_to_slot or 1))] = f"Проигравший матча {x.match_no}"
    return rev


def match_json(
    m: TournamentMatch,
    *,
    names: dict[int, str],
    seeds: dict[int, int],
    rev: dict[tuple[int, int], str],
    viewer_team_id: int | None = None,
    avatar_fn: AvatarFn | None = None,
) -> dict[str, Any]:
    def brief(tid: int | None) -> dict[str, Any] | None:
        if tid is None:
            return None
        return {"id": int(tid), "name": names.get(int(tid), "Команда удалена"), "seed": seeds.get(int(tid)),
                "avatar_url": avatar_fn(int(tid)) if avatar_fn else None}

    at = timeutil.as_utc(m.scheduled_at)
    return {
        "id": int(m.id),
        "no": int(m.match_no),
        "key": m.plan_key,
        "bracket": m.bracket,
        "round_no": int(m.round_no),
        "round_name": m.round_name,
        "position": int(m.position),
        "label": f"Матч {m.match_no}",
        "best_of": int(m.best_of),
        "wins_needed": wins_needed(int(m.best_of)),
        "status": m.status,
        "status_text": STATUS_TEXT.get(m.status, m.status),
        "team1": brief(m.team1_id),
        "team2": brief(m.team2_id),
        "team1_placeholder": None if m.team1_id else rev.get((int(m.id), 1), "Ожидается"),
        "team2_placeholder": None if m.team2_id else rev.get((int(m.id), 2), "Ожидается"),
        # BYE: команда стоит во 2-м раунде сетки сразу по посеву (в сетке мест больше, чем команд).
        "team1_bye": bool(m.team1_id and int(m.round_no) > 1 and m.bracket in ("upper", "main") and (int(m.id), 1) not in rev),
        "team2_bye": bool(m.team2_id and int(m.round_no) > 1 and m.bracket in ("upper", "main") and (int(m.id), 2) not in rev),
        "score1": m.score1,
        "score2": m.score2,
        "winner_team_id": m.winner_team_id,
        "result_type": m.result_type,
        "scheduled_at": at.isoformat().replace("+00:00", "Z") if at else None,
        "scheduled_text": timeutil.fmt_local(at),
        "scheduled_input": timeutil.input_value(at),
        "mine": bool(viewer_team_id and viewer_team_id in (m.team1_id, m.team2_id)),
        "version": int(m.version or 1),
    }


async def _view_context(session: AsyncSession, stage: TournamentStage) -> tuple[list[TournamentMatch], dict[int, int], dict[int, str]]:
    matches = await _stage_matches(session, stage.id)
    parts = (await session.execute(
        select(StageParticipant.team_id, StageParticipant.seed).where(StageParticipant.stage_id == stage.id)
    )).all()
    seeds = {int(t): int(s) for t, s in parts if s is not None}
    ids = {int(t) for t, _ in parts}
    for x in matches:
        ids.update(i for i in (x.team1_id, x.team2_id) if i)
    return matches, seeds, await _team_names(session, ids)


PUBLIC_SETTINGS = ("bo_default", "bo_final", "third_place", "grand_final_reset", "seeding", "groups", "advance",
                   "playoff_format", "playoff_bo_default", "playoff_bo_final", "wins_to_advance", "losses_to_eliminate")


async def _single_stage_view(
    session: AsyncSession,
    tournament: Tournament,
    stage: TournamentStage,
    *,
    has_next_stage: bool,
    viewer_team_id: int | None,
    avatar_fn: AvatarFn | None,
) -> dict[str, Any]:
    fmt = FORMATS.get(stage.format)
    settings = _settings(stage)
    matches, seeds, names = await _view_context(session, stage)
    rev = _reverse_links(matches)
    participants = set(seeds) | {int(t) for t in (await session.execute(
        select(StageParticipant.team_id).where(StageParticipant.stage_id == stage.id)
    )).scalars().all()}
    stale = False
    if int(stage.order_no or 1) == 1:
        approved = set((await session.execute(
            select(TournamentApplication.team_id).where(
                TournamentApplication.tournament_id == tournament.id, TournamentApplication.status == ApplicationStatus.APPROVED
            )
        )).scalars().all())
        stale = participants != approved
    known = list(fmt.brackets) if fmt else []
    groups: dict[str, dict[int, dict[str, Any]]] = {}
    for x in matches:
        rounds = groups.setdefault(x.bracket, {})
        rnd = rounds.setdefault(int(x.round_no), {"round_no": int(x.round_no), "name": x.round_name, "matches": []})
        rnd["matches"].append(match_json(x, names=names, seeds=seeds, rev=rev, viewer_team_id=viewer_team_id, avatar_fn=avatar_fn))
    order = [k for k in known if k in groups] + sorted(b for b in groups if b not in known)
    brackets = []
    for key in order:
        rounds = [groups[key][r] for r in sorted(groups[key])]
        for r in rounds:
            r["matches"].sort(key=lambda mm: mm["position"])
        brackets.append({"key": key, "title": fmt.bracket_title(key) if fmt else key, "rounds": rounds})
    standings = await _standings_for(session, stage, fmt, settings, matches, names) if fmt and fmt.table else []
    for grp in standings:
        for row in grp["rows"]:
            row["avatar_url"] = avatar_fn(int(row["team_id"])) if avatar_fn else None
    champion = None
    if stage.status == "FINISHED" and not (fmt and fmt.feeds_playoffs(settings)):
        tid = None
        if fmt and fmt.table and standings and standings[0]["rows"]:
            tid = int(standings[0]["rows"][0]["team_id"])
        else:
            finals = [x for x in matches if x.bracket in ("main", "final") and x.winner_team_id]
            if finals:
                tid = int(max(finals, key=lambda x: x.match_no).winner_team_id)
        if tid:
            champion = {"id": tid, "name": names.get(tid, "Команда удалена"), "avatar_url": avatar_fn(tid) if avatar_fn else None}
    counts = {s: 0 for s in STATUS_TEXT}
    for x in matches:
        counts[x.status] = counts.get(x.status, 0) + 1
    feeds = bool(fmt and fmt.feeds_playoffs(settings))
    all_done = bool(matches) and all(x.status == "FINISHED" for x in matches)
    return {
        "exists": True,
        "stage_id": int(stage.id),
        "order_no": int(stage.order_no or 1),
        "format": stage.format,
        "format_title": fmt.title if fmt else stage.format,
        "kind": "table" if fmt and fmt.table else "bracket",
        "description": stage.name,
        "locked": bool(stage.locked),
        "status": stage.status,
        "settings": {k: v for k, v in settings.items() if k in PUBLIC_SETTINGS},
        "generated_at": settings.get("generated_at"),
        "tz": timeutil.tz_info(),
        "brackets": brackets,
        "standings": standings,
        "allow_draw": bool(fmt and fmt.allow_draw),
        "feeds_playoffs": feeds,
        "playoffs_ready": feeds and all_done and not has_next_stage,
        "playoffs_created": has_next_stage,
        "champion": champion,
        "stale": stale,
        "team_count": len(participants),
        "counts": counts,
        "total": len(matches),
    }


async def stage_view(
    session: AsyncSession,
    tournament: Tournament,
    *,
    viewer_team_id: int | None = None,
    avatar_fn: AvatarFn | None = None,
) -> dict[str, Any] | None:
    """Выдача сетки турнира. Основной ключ — последний этап (плей-офф, если он уже есть);
    при нескольких этапах все они — в all_stages (сначала группы, затем плей-офф)."""
    stages = await get_stages(session, tournament.id)
    if not stages:
        return None
    views = [
        await _single_stage_view(session, tournament, s, has_next_stage=i < len(stages) - 1,
                                 viewer_team_id=viewer_team_id, avatar_fn=avatar_fn)
        for i, s in enumerate(stages)
    ]
    primary = dict(views[-1])
    if len(views) > 1:
        primary["all_stages"] = views
        # Фиксация/пересборка относятся к первому этапу — показываем его флаги.
        primary["stale"] = views[0]["stale"]
    return primary


async def schedule_view(
    session: AsyncSession,
    tournament: Tournament,
    *,
    viewer_team_id: int | None = None,
    avatar_fn: AvatarFn | None = None,
) -> dict[str, Any]:
    stages = await get_stages(session, tournament.id)
    if not stages:
        return {"exists": False, "tz": timeutil.tz_info(), "items": []}
    rows_all = []
    for stage in stages:
        matches, seeds, names = await _view_context(session, stage)
        rev = _reverse_links(matches)
        fmt = FORMATS.get(stage.format)
        for x in matches:
            row = match_json(x, names=names, seeds=seeds, rev=rev, viewer_team_id=viewer_team_id, avatar_fn=avatar_fn)
            local = timeutil.to_local(x.scheduled_at)
            row["day"] = local.strftime("%d.%m.%Y") if local else None
            row["time"] = local.strftime("%H:%M") if local else None
            row["bracket_title"] = fmt.bracket_title(x.bracket) if fmt else x.bracket
            row["stage_order"] = int(stage.order_no or 1)
            rows_all.append((x, row))
    rows_all.sort(key=lambda p: (p[0].scheduled_at is None, timeutil.as_utc(p[0].scheduled_at) or timeutil.now_utc(),
                                 p[1]["stage_order"], p[0].match_no))
    return {"exists": True, "tz": timeutil.tz_info(), "format": stages[0].format, "items": [r for _, r in rows_all]}


async def match_detail(
    session: AsyncSession,
    match_id: int,
    *,
    admin: bool = False,
    viewer_team_id: int | None = None,
    avatar_fn: AvatarFn | None = None,
) -> dict[str, Any]:
    m, stage, fmt, _ = await _load_match(session, match_id)
    matches, seeds, names = await _view_context(session, stage)
    rev = _reverse_links(matches)
    data = match_json(m, names=names, seeds=seeds, rev=rev, viewer_team_id=viewer_team_id, avatar_fn=avatar_fn)
    tournament = await session.get(Tournament, m.tournament_id)
    data.update({
        "tournament_id": int(m.tournament_id),
        "tournament_name": tournament.name if tournament else None,
        "tournament_status": tournament.status.value if tournament else None,
        "bracket_title": fmt.bracket_title(m.bracket),
        "stage_locked": bool(stage.locked),
        "allow_draw": bool(fmt.allow_draw),
        "best_of_options": [1, 2, 3, 5] if fmt.allow_draw else [1, 3, 5],
        "allowed_scores": allowed_scores(int(m.best_of)),
        "tz": timeutil.tz_info(),
        "next_winner": next((f"Матч {x.match_no}" for x in matches if x.id == m.winner_to_match_id), None),
        "next_loser": next((f"Матч {x.match_no}" for x in matches if x.id == m.loser_to_match_id), None),
    })
    data["manual"] = fmt.key == "manual"
    if admin:
        data["team_options"] = await team_options(session, m)
        rows = (await session.execute(
            select(TournamentMatchLog).where(TournamentMatchLog.match_id == m.id).order_by(TournamentMatchLog.id.desc()).limit(20)
        )).scalars().all()
        data["log"] = [{
            "action": r.action,
            "at": timeutil.fmt_local(r.created_at, label=True),
            "admin_user_id": r.admin_user_id,
            "old": json.loads(r.old_json) if r.old_json else None,
            "new": json.loads(r.new_json) if r.new_json else None,
        } for r in rows]
    return data


async def match_team_members(session: AsyncSession, m: TournamentMatch) -> list[tuple[int, int]]:
    """(user_id, telegram_id) игроков обеих команд — для уведомлений."""
    from database.models import User

    ids = [t for t in (m.team1_id, m.team2_id) if t]
    if not ids:
        return []
    rows = (await session.execute(
        select(User.id, User.telegram_id).join(TeamMember, TeamMember.user_id == User.id).where(TeamMember.team_id.in_(ids))
    )).all()
    return [(int(u), int(t)) for u, t in rows]
