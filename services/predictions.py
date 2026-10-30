"""5.13: прогнозы на матчи — кто победит. До начала матча; игроки матча за свой матч не голосуют."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Prediction, TeamMember, TournamentMatch, User
from services.tournament_engine import timeutil


def is_open(m: TournamentMatch) -> bool:
    if not (m.team1_id and m.team2_id) or m.status in ("LIVE", "FINISHED"):
        return False
    at = timeutil.as_utc(m.scheduled_at)
    return at is None or timeutil.now_utc() < at


async def _plays_in(session: AsyncSession, m: TournamentMatch, user_id: int) -> bool:
    tid = await session.scalar(select(TeamMember.team_id).where(TeamMember.user_id == int(user_id)).limit(1))
    return bool(tid and int(tid) in (m.team1_id, m.team2_id))


async def state(session: AsyncSession, m: TournamentMatch, user_id: int | None) -> dict[str, Any]:
    rows = dict((await session.execute(select(Prediction.team_id, func.count(Prediction.id)).where(Prediction.match_id == m.id)
                                       .group_by(Prediction.team_id))).all())
    mine = await session.scalar(select(Prediction.team_id).where(Prediction.match_id == m.id, Prediction.user_id == int(user_id))) if user_id else None
    c1, c2 = int(rows.get(m.team1_id, 0) or 0), int(rows.get(m.team2_id, 0) or 0)
    plays = bool(user_id) and await _plays_in(session, m, int(user_id))
    return {"open": is_open(m) and not plays, "closed_reason": "Вы играете в этом матче." if plays else (None if is_open(m) else "Прогнозы закрыты — матч начался."),
            "team1": c1, "team2": c2, "total": c1 + c2, "mine": int(mine) if mine else None,
            "correct": (int(mine) == int(m.winner_team_id)) if (mine and m.status == "FINISHED" and m.winner_team_id) else None}


async def predict(session: AsyncSession, match_id: int, user_id: int, team_id: int) -> dict[str, Any]:
    m = await session.get(TournamentMatch, int(match_id))
    if m is None:
        raise ValueError("Матч не найден.")
    if int(team_id) not in (m.team1_id, m.team2_id):
        raise ValueError("Выберите одну из команд матча.")
    if not is_open(m):
        raise ValueError("Прогнозы на этот матч закрыты.")
    if await _plays_in(session, m, user_id):
        raise ValueError("Игроки матча не делают прогноз на свой матч.")
    row = await session.scalar(select(Prediction).where(Prediction.match_id == m.id, Prediction.user_id == int(user_id)))
    if row is None:
        session.add(Prediction(match_id=m.id, tournament_id=int(m.tournament_id), user_id=int(user_id), team_id=int(team_id)))
    else:
        row.team_id = int(team_id)
    await session.flush()
    return await state(session, m, user_id)


async def leaderboard(session: AsyncSession, tournament_id: int, user_id: int | None = None, limit: int = 20) -> dict[str, Any]:
    """Лучшие предсказатели: угаданные исходы среди сыгранных матчей."""
    rows = (await session.execute(
        select(Prediction.user_id, Prediction.team_id, TournamentMatch.winner_team_id)
        .join(TournamentMatch, TournamentMatch.id == Prediction.match_id)
        .where(Prediction.tournament_id == int(tournament_id), TournamentMatch.status == "FINISHED", TournamentMatch.winner_team_id.is_not(None))
    )).all()
    stats: dict[int, list[int]] = {}
    for uid, team, winner in rows:
        s = stats.setdefault(int(uid), [0, 0])
        s[1] += 1
        s[0] += int(int(team) == int(winner))
    order = sorted(stats.items(), key=lambda kv: (-kv[1][0], -(kv[1][0] / kv[1][1]), kv[0]))
    users = {int(u.id): u for u in (await session.execute(select(User).where(User.id.in_([k for k, _ in order[:limit]])))).scalars().all()} if order else {}

    def name(u: User | None, uid: int) -> str:
        if u is None:
            return f"#{uid}"
        return "@" + u.username if u.username else (u.first_name or f"игрок {uid}")

    top = [{"place": i + 1, "user_id": uid, "name": name(users.get(uid), uid), "correct": c, "total": t, "accuracy": round(c * 100 / t)}
           for i, (uid, (c, t)) in enumerate(order[:limit])]
    me = None
    if user_id and int(user_id) in stats:
        c, t = stats[int(user_id)]
        me = {"place": [k for k, _ in order].index(int(user_id)) + 1, "correct": c, "total": t}
    pending = int(await session.scalar(select(func.count(Prediction.id)).join(TournamentMatch, TournamentMatch.id == Prediction.match_id)
                                       .where(Prediction.tournament_id == int(tournament_id), TournamentMatch.status != "FINISHED")) or 0)
    return {"items": top, "me": me, "pending": pending}
