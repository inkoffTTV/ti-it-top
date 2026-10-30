"""Агрегированные SQL-запросы Mini App 5.0 (без N+1)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    ApplicationStatus,
    PlayerProfile,
    Team,
    TeamInvite,
    TeamInviteStatus,
    TeamMember,
    TeamRole,
    Tournament,
    TournamentApplication,
    User,
    utcnow,
)
from miniapp_v2.common import (
    avatar_url,
    display_name,
    iso,
    medal_facts,
    mmr_facts,
    parse_positions,
    player_avatar_url,
)
from services.teams import MAX_ROSTER_SIZE


def confirmed_mmr_expr():
    """SQL-выражение подтверждённого MMR: approved_mmr, иначе exact_mmr при mmr_verified."""
    return func.coalesce(
        PlayerProfile.approved_mmr,
        case((PlayerProfile.mmr_verified.is_(True), PlayerProfile.exact_mmr), else_=None),
    )


def member_stats_subquery():
    """team_id -> размер состава, число подтверждённых MMR, средний подтверждённый MMR."""
    confirmed = confirmed_mmr_expr()
    return (
        select(
            TeamMember.team_id.label("team_id"),
            func.count(TeamMember.id).label("members"),
            func.count(confirmed).label("mmr_count"),
            func.avg(confirmed).label("mmr_avg"),
        )
        .select_from(TeamMember)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == TeamMember.user_id)
        .where(TeamMember.role != TeamRole.COACH)  # тренер не игрок
        .group_by(TeamMember.team_id)
        .subquery()
    )


def team_list_statement():
    stats = member_stats_subquery()
    stmt = (
        select(
            Team,
            User,
            func.coalesce(stats.c.members, 0),
            func.coalesce(stats.c.mmr_count, 0),
            stats.c.mmr_avg,
        )
        .outerjoin(User, User.id == Team.captain_user_id)
        .outerjoin(stats, stats.c.team_id == Team.id)
    )
    return stmt


def team_row_json(team: Team, captain: User | None, members: Any, mmr_count: Any, mmr_avg: Any) -> dict[str, Any]:
    members = int(members or 0)
    return {
        "id": int(team.id),
        "name": team.name,
        "captain": {"user_id": int(captain.id), "name": display_name(captain), "username": captain.username} if captain else None,
        "members": members,
        "max_members": MAX_ROSTER_SIZE,
        "over_limit": members > MAX_ROSTER_SIZE,
        "open_slots": max(0, MAX_ROSTER_SIZE - members),
        "avg_mmr": round(float(mmr_avg)) if mmr_avg is not None else None,
        "mmr_count": int(mmr_count or 0),
        "avatar_url": avatar_url(team.id),
        "role_mode": team.role_mode,
    }


async def load_roster(session: AsyncSession, team: Team, viewer_user_id: int | None = None) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(TeamMember, User, PlayerProfile)
        .join(User, User.id == TeamMember.user_id)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == TeamMember.user_id)
        .where(TeamMember.team_id == team.id)
        .order_by(TeamMember.id)
    )).all()
    roster = []
    for member, user, profile in rows:
        roster.append({
            "user_id": int(user.id),
            "name": display_name(user),
            "username": user.username,
            "avatar_url": player_avatar_url(user.id),
            "role": "CAPTAIN" if int(user.id) == int(team.captain_user_id) else ("COACH" if member.role == TeamRole.COACH else "PLAYER"),
            "position": member.position,
            "positions": parse_positions(profile.preferred_positions if profile else None),
            "mmr": mmr_facts(profile),
            "medal": medal_facts(profile),
            "is_me": viewer_user_id is not None and int(user.id) == int(viewer_user_id),
            "joined_at": iso(member.joined_at),
        })
    roster.sort(key=lambda m: (m["role"] != "CAPTAIN", m["role"] == "COACH", m["position"] or 9, m["name"].casefold()))
    return roster


def roster_summary(roster: list[dict[str, Any]]) -> dict[str, Any]:
    players = [m for m in roster if m["role"] != "COACH"]
    confirmed = [m["mmr"]["confirmed"] for m in players if m["mmr"]["confirmed"] is not None]
    return {
        "roster_count": len(players),
        "coach": next((m["name"] for m in roster if m["role"] == "COACH"), None),
        "max_roster": MAX_ROSTER_SIZE,
        "over_limit": len(players) > MAX_ROSTER_SIZE,
        "avg_mmr": round(sum(confirmed) / len(confirmed)) if confirmed else None,
        "mmr_count": len(confirmed),
    }


async def read_team_description(team_id: int) -> str | None:
    """Описание читается отдельно: колонка отложенная (deferred)."""
    from database.engine import session_scope

    try:
        async with session_scope() as session:
            return await session.scalar(select(Team.description).where(Team.id == int(team_id)))
    except Exception:  # noqa: BLE001 — колонка могла не добавиться: описание просто пустое
        return None


async def pending_invites_for_user(session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    now = utcnow()
    captain = User
    rows = (await session.execute(
        select(TeamInvite, Team, captain)
        .join(Team, Team.id == TeamInvite.team_id)
        .outerjoin(captain, captain.id == Team.captain_user_id)
        .where(
            TeamInvite.invitee_user_id == int(user_id),
            TeamInvite.status == TeamInviteStatus.PENDING,
            TeamInvite.expires_at > now,
        )
        .order_by(TeamInvite.id.desc())
        .limit(20)
    )).all()
    return [
        {
            "id": int(inv.id),
            "team_id": int(team.id),
            "team_name": team.name,
            "captain": display_name(cap) if cap else None,
            "avatar_url": avatar_url(team.id),
            "expires_at": iso(inv.expires_at),
        }
        for inv, team, cap in rows
    ]


async def outgoing_invites_for_team(session: AsyncSession, team_id: int) -> list[dict[str, Any]]:
    now = utcnow()
    rows = (await session.execute(
        select(TeamInvite, User)
        .join(User, User.id == TeamInvite.invitee_user_id)
        .where(
            TeamInvite.team_id == int(team_id),
            TeamInvite.status == TeamInviteStatus.PENDING,
            TeamInvite.expires_at > now,
        )
        .order_by(TeamInvite.id.desc())
        .limit(30)
    )).all()
    return [
        {
            "id": int(inv.id),
            "user_id": int(user.id),
            "name": display_name(user),
            "username": user.username,
            "expires_at": iso(inv.expires_at),
        }
        for inv, user in rows
    ]


async def team_applications(session: AsyncSession, team_id: int) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(TournamentApplication, Tournament)
        .join(Tournament, Tournament.id == TournamentApplication.tournament_id)
        .where(TournamentApplication.team_id == int(team_id))
        .order_by(TournamentApplication.id.desc())
        .limit(20)
    )).all()
    return [
        {
            "id": int(app.id),
            "tournament_id": int(t.id),
            "tournament_name": t.name,
            "tournament_status": t.status.value,
            "status": app.status.value,
            "comment": app.admin_comment,
            "created_at": iso(app.created_at),
            "decided_at": iso(app.decided_at),
        }
        for app, t in rows
    ]


async def application_counts_by_tournament(session: AsyncSession) -> dict[int, dict[str, int]]:
    rows = (await session.execute(
        select(TournamentApplication.tournament_id, TournamentApplication.status, func.count(TournamentApplication.id))
        .group_by(TournamentApplication.tournament_id, TournamentApplication.status)
    )).all()
    out: dict[int, dict[str, int]] = {}
    for tid, status, n in rows:
        key = status.value if hasattr(status, "value") else str(status)
        out.setdefault(int(tid), {})[key] = int(n)
    return out


def tournament_json(t: Tournament, counts: dict[str, int] | None = None) -> dict[str, Any]:
    counts = counts or {}
    approved = int(counts.get(ApplicationStatus.APPROVED.value, 0))
    return {
        "id": int(t.id),
        "name": t.name,
        "description": t.description,
        "status": t.status.value,
        "registration_open": bool(t.registration_open),
        "max_teams": int(t.max_teams),
        "start_date": iso(t.start_date),
        "approved_count": approved,
        "pending_count": int(counts.get(ApplicationStatus.PENDING.value, 0)),
        "slots_left": max(0, int(t.max_teams) - approved),
    }
