"""5.13: утренняя сводка организаторам и поиск дублей (один Steam на несколько аккаунтов, игрок в двух командах)."""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import session_scope
from database.models import (
    ApplicationStatus,
    MatchReport,
    PlayerCheck,
    PlayerProfile,
    RosterRequest,
    Team,
    TeamMember,
    Tournament,
    TournamentApplication,
    TournamentMatch,
    TournamentStatus,
    User,
)
from services.tournament_engine import timeutil

T_STATUS = {"DRAFT": "черновик", "REGISTRATION": "регистрация", "ACTIVE": "идёт", "FINISHED": "завершён", "CANCELLED": "отменён"}


def _h(v: Any) -> str:
    return escape(str(v if v is not None else ""), quote=False)


def _uname(u: User) -> str:
    return "@" + u.username if u.username else (" ".join(x for x in (u.first_name, u.last_name) if x) or f"id{u.telegram_id}")


async def find_duplicates(session: AsyncSession) -> dict[str, Any]:
    """Один Steam-аккаунт у нескольких пользователей и игрок, числящийся в нескольких командах."""
    team_of: dict[int, str] = {int(u): n for u, n in (await session.execute(
        select(TeamMember.user_id, Team.name).join(Team, Team.id == TeamMember.team_id))).all()}
    steam_ids = (await session.execute(
        select(PlayerProfile.steam_id64).where(PlayerProfile.steam_id64.is_not(None), PlayerProfile.steam_id64 != "")
        .group_by(PlayerProfile.steam_id64).having(func.count(PlayerProfile.id) > 1))).scalars().all()
    steam = []
    for sid in steam_ids:
        users = (await session.execute(select(User).join(PlayerProfile, PlayerProfile.user_id == User.id)
                                       .where(PlayerProfile.steam_id64 == sid))).scalars().all()
        steam.append({"steam_id64": sid, "users": [{"user_id": int(u.id), "name": _uname(u), "team": team_of.get(int(u.id))} for u in users]})
    multi_ids = (await session.execute(select(TeamMember.user_id).group_by(TeamMember.user_id)
                                       .having(func.count(func.distinct(TeamMember.team_id)) > 1))).scalars().all()
    multi = []
    for uid in multi_ids:
        u = await session.get(User, uid)
        names = (await session.execute(select(Team.name).join(TeamMember, TeamMember.team_id == Team.id)
                                       .where(TeamMember.user_id == uid))).scalars().all()
        multi.append({"user_id": int(uid), "name": _uname(u) if u else f"#{uid}", "teams": list(names)})
    return {"steam": steam, "multi_team": multi, "total": len(steam) + len(multi)}


async def _risky_players(session: AsyncSession, tournament_id: int) -> list[str]:
    """Игроки одобренных команд, у которых последняя проверка дала средний или высокий риск смурфа."""
    rows = (await session.execute(
        select(User, Team.name).join(TeamMember, TeamMember.user_id == User.id).join(Team, Team.id == TeamMember.team_id)
        .join(TournamentApplication, TournamentApplication.team_id == Team.id)
        .where(TournamentApplication.tournament_id == int(tournament_id), TournamentApplication.status == ApplicationStatus.APPROVED)
    )).all()
    out = []
    for u, team in rows:
        chk = await session.scalar(select(PlayerCheck).where(PlayerCheck.user_id == u.id).order_by(PlayerCheck.id.desc()).limit(1))
        level = (chk.risk_level or "").lower() if chk else ""
        if level in ("high", "medium"):
            word = "высокий" if level == "high" else "средний"
            out.append(f"{_uname(u)} ({team}) — {word} риск" + (f", {chk.risk_score}/100" if chk.risk_score is not None else ""))
    return out


async def _tournament_digest(session: AsyncSession, t: Tournament, today: str) -> str:
    from services.tournament_engine import service as engine

    approved = int(await session.scalar(select(func.count(TournamentApplication.id)).where(
        TournamentApplication.tournament_id == t.id, TournamentApplication.status == ApplicationStatus.APPROVED)) or 0)
    waiting_apps = int(await session.scalar(select(func.count(TournamentApplication.id)).where(
        TournamentApplication.tournament_id == t.id, TournamentApplication.status == ApplicationStatus.PENDING)) or 0)
    start = timeutil.to_local(t.start_date)
    lines = [f"<b>{_h(t.name)}</b> — {T_STATUS.get(t.status.value, t.status.value)}",
             f"Старт: {_h(start.strftime('%d.%m %H:%M') + ' МСК' if start else 'не назначен')} · одобрено {approved} из {t.max_teams}"
             + (f" · ждут решения: {waiting_apps}" if waiting_apps else "")]
    if t.status == TournamentStatus.REGISTRATION:
        try:
            plan = await engine.autopair_leftovers(session, t, dry_run=True)
            preview = plan.get("text") or ""
        except Exception:  # noqa: BLE001
            preview = ""
        lines.append("Регистрация открыта — закройте её до старта, пары доберутся сами." + (f" Сейчас: {_h(preview)}" if preview else ""))
    matches = (await session.execute(select(TournamentMatch).where(TournamentMatch.tournament_id == t.id)
                                     .order_by(TournamentMatch.match_no))).scalars().all()
    names = await engine._team_names(session, {x for m in matches for x in (m.team1_id, m.team2_id) if x})
    todays = sorted((m for m in matches if m.scheduled_at and (timeutil.fmt_local(m.scheduled_at) or "").startswith(today)
                     and m.status != "FINISHED"), key=lambda m: timeutil.as_utc(m.scheduled_at))
    if todays:
        lines.append(f"Матчи сегодня ({len(todays)}):")
        for m in todays[:15]:
            vs = f"{names.get(m.team1_id, 'ожидается') if m.team1_id else 'ожидается'} — {names.get(m.team2_id, 'ожидается') if m.team2_id else 'ожидается'}"
            lines.append(f"• {timeutil.to_local(m.scheduled_at).strftime('%H:%M')} матч {m.match_no} ({_h(m.round_name)}): {_h(vs)}")
    no_time = [m for m in matches if m.team1_id and m.team2_id and not m.scheduled_at and m.status not in ("FINISHED", "LIVE")]
    if no_time:
        lines.append(f"Без времени: {len(no_time)} — матчи " + ", ".join(str(m.match_no) for m in no_time[:20]) + ".")
    reports = int(await session.scalar(select(func.count(MatchReport.id)).join(TournamentMatch, TournamentMatch.id == MatchReport.match_id).where(
        TournamentMatch.tournament_id == t.id, MatchReport.status.in_(("PENDING", "CONFIRMED", "DISPUTED")))) or 0)
    roster = int(await session.scalar(select(func.count(RosterRequest.id)).where(
        RosterRequest.tournament_id == t.id, RosterRequest.status == "PENDING")) or 0)
    if reports or roster:
        lines.append("Ждут решения: " + ", ".join(x for x in (
            f"результаты капитанов — {reports}" if reports else "", f"замены в составах — {roster}" if roster else "") if x) + ".")
    risky = await _risky_players(session, t.id)
    if risky:
        lines.append(f"Риск смурфа в одобренных командах ({len(risky)}):")
        lines += [f"• {_h(r)}" for r in risky[:10]]
    return "\n".join(lines)


async def build_digest(now: datetime | None = None, *, force: bool = False) -> str | None:
    """Сводка на сегодня (МСК). None — сегодня турнирных дел нет (если не force)."""
    now = now or timeutil.now_utc()
    today = timeutil.to_local(now).strftime("%d.%m.%Y")
    async with session_scope() as session:
        rows = (await session.execute(select(Tournament).where(Tournament.status.in_(
            (TournamentStatus.REGISTRATION, TournamentStatus.DRAFT, TournamentStatus.ACTIVE))).order_by(Tournament.id))).scalars().all()
        picked = []
        for t in rows:
            starts_today = (timeutil.fmt_local(t.start_date) or "").startswith(today)
            has_today = any((timeutil.fmt_local(x) or "").startswith(today) for x in (await session.execute(
                select(TournamentMatch.scheduled_at).where(TournamentMatch.tournament_id == t.id, TournamentMatch.scheduled_at.is_not(None))
            )).scalars().all())
            if starts_today or has_today or t.status == TournamentStatus.ACTIVE or force:
                picked.append(t)
        if not picked and not force:
            return None
        parts = [f"<b>Сводка на {today[:5]}</b>"]
        for t in picked:
            parts.append(await _tournament_digest(session, t, today))
        dup = await find_duplicates(session)
        if dup["total"]:
            parts.append(f"Дубли аккаунтов: {dup['total']} (Mini App → Админ → Подозрительные).")
        if not picked:
            parts.append("Активных турниров нет.")
    return "\n\n".join(parts)


async def digest_due(state_file: Path, now: datetime | None = None, *, hour: int = 9) -> str | None:
    """Раз в день после `hour` по Москве — текст сводки (или None). День отправки хранится в файле вне папки кода,
    поэтому перезапуск и деплой не присылают сводку повторно."""
    now = now or timeutil.now_utc()
    local = timeutil.to_local(now)
    day = local.strftime("%Y-%m-%d")
    try:
        last = state_file.read_text(encoding="utf-8").strip()
    except OSError:
        last = ""
    if local.hour < hour or last == day:
        return None
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(day, encoding="utf-8")
    return await build_digest(now)
