"""5.13: общий журнал действий организаторов — кто, что и когда менял в сетке, результатах и составах."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import RosterRequest, Team, Tournament, TournamentMatch, TournamentMatchLog, User, UserBan
from services.tournament_engine import timeutil

ACTION = {
    "result": "внёс результат", "result_correct": "исправил результат", "result_reset": "сбросил результат",
    "cascade_reset": "сброс по цепочке", "teams": "поменял пары", "schedule": "назначил время", "status": "сменил статус матча",
    "best_of": "сменил формат серии", "generate": "сгенерировал сетку", "lock": "зафиксировал сетку", "reset": "сбросил сетку",
    "add_match": "добавил матч", "delete_match": "удалил матч", "autopair": "добрал пары", "withdraw": "снял команду из матча",
    "withdraw_forfeit": "техпоражение снявшейся команде", "playoffs": "сформировал плей-офф",
}
SKIP = {"reminder", "ready", "noshow_check", "fan_start", "chat_open"}


def _name(u: User | None, fallback: Any = None) -> str:
    if u is None:
        return f"#{fallback}" if fallback else "система"
    return "@" + u.username if u.username else (" ".join(x for x in (u.first_name, u.last_name) if x) or f"id{u.telegram_id}")


async def entries(session: AsyncSession, limit: int = 150) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    users: dict[int, User | None] = {}

    async def user(uid: int | None) -> User | None:
        if not uid:
            return None
        if uid not in users:
            users[uid] = await session.get(User, int(uid))
        return users[uid]

    logs = (await session.execute(select(TournamentMatchLog).where(TournamentMatchLog.admin_user_id.is_not(None))
                                  .order_by(TournamentMatchLog.id.desc()).limit(limit))).scalars().all()
    matches = {int(m.id): m for m in (await session.execute(select(TournamentMatch).where(
        TournamentMatch.id.in_({int(x.match_id) for x in logs if x.match_id})))).scalars().all()}
    tournaments = {int(t.id): t.name for t in (await session.execute(select(Tournament))).scalars().all()}
    for row in logs:
        if row.action in SKIP:
            continue
        m = matches.get(int(row.match_id or 0))
        what = ACTION.get(row.action, row.action)
        detail = ""
        try:
            new = json.loads(row.new_json or "null")
            if isinstance(new, dict) and new.get("score1") is not None and row.action.startswith("result"):
                detail = f" {new['score1']}:{new['score2']}"
            if isinstance(new, dict) and row.action == "schedule" and new.get("scheduled_at"):
                detail = " — " + (timeutil.fmt_local(timeutil.parse_local(new["scheduled_at"])) or "")
        except (ValueError, TypeError):
            pass
        where = f"матч {m.match_no} ({m.round_name}), {tournaments.get(int(m.tournament_id), '')}" if m else "сетка"
        out.append({"at": row.created_at, "who": _name(await user(row.admin_user_id), row.admin_user_id), "what": f"{what}{detail}", "where": where})
    for r in (await session.execute(select(RosterRequest).where(RosterRequest.decided_by.is_not(None))
                                    .order_by(RosterRequest.id.desc()).limit(limit))).scalars().all():
        team = await session.get(Team, r.team_id)
        verdict = "одобрил" if r.status == "APPROVED" else "отклонил"
        out.append({"at": r.decided_at or r.created_at, "who": _name(await user(r.decided_by), r.decided_by),
                    "what": f"{verdict} замену ({r.kind.lower()})", "where": f"{team.name if team else 'команда'} — {_name(await user(r.target_user_id), r.target_user_id)}"})
    for b in (await session.execute(select(UserBan).order_by(UserBan.id.desc()).limit(limit))).scalars().all():
        by = await session.scalar(select(User).where((User.id == b.banned_by) | (User.telegram_id == b.banned_by))) if b.banned_by else None
        out.append({"at": b.created_at, "who": _name(by, b.banned_by), "what": "заблокировал пользователя" + (f" ({b.reason})" if b.reason else ""),
                    "where": _name(await user(b.user_id), b.user_id)})
    out.sort(key=lambda e: timeutil.as_utc(e["at"]) or timeutil.now_utc(), reverse=True)
    for e in out:
        e["at"] = timeutil.fmt_local(e["at"])
    return out[:limit]
