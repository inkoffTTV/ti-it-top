"""5.9: игрок или зритель, «болею за команду», закрытые чаты матчей.

Игрок — участвует: команда, заявки, MMR, лобби и чаты матчей, голосование за MVP.
Зритель — смотрит: турниры, сетка, расписание, результаты, статистика, зал славы,
трансляция, общий чат, поддержка; может «болеть» за команды и получать их уведомления.
Кто уже зарегистрирован, привязал Steam или состоит в команде — всегда игрок.
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select

from database.engine import session_scope
from database.models import (
    MatchChatMessage,
    PlayerProfile,
    TeamFollow,
    TeamMember,
    TournamentMatch,
    TournamentMatchLog,
    User,
    UserKind,
)

PLAYER, VIEWER = "PLAYER", "VIEWER"
TWITCH_CHANNEL = (os.getenv("TWITCH_CHANNEL") or "inkoff_ttv").strip()
TWITCH_URL = f"https://www.twitch.tv/{TWITCH_CHANNEL}"
CHAT_OPENS_BEFORE = timedelta(minutes=60)
VIEWER_BLOCK = "Вы вошли как зритель. Чтобы участвовать, нажмите «Стать игроком» (Профиль в Mini App или кнопка в боте)."


async def kind_of(session, user_id: int) -> tuple[str | None, bool]:
    """(тип, выбран_явно). Зарегистрированные/со Steam/в команде — всегда PLAYER."""
    row = await session.scalar(select(UserKind.kind).where(UserKind.user_id == int(user_id)))
    user = await session.get(User, int(user_id))
    has_steam = await session.scalar(select(PlayerProfile.id).where(PlayerProfile.user_id == int(user_id), PlayerProfile.account_id.is_not(None)))
    in_team = await session.scalar(select(TeamMember.id).where(TeamMember.user_id == int(user_id)))
    if (user and user.is_registered) or has_steam or in_team:
        return PLAYER, True
    return (row, True) if row else (None, False)


async def set_kind(session, user_id: int, kind: str) -> str:
    kind = str(kind or "").upper()
    if kind not in (PLAYER, VIEWER):
        raise ValueError("Тип: игрок или зритель.")
    if kind == VIEWER:
        current, _ = await kind_of(session, user_id)
        if current == PLAYER and await session.scalar(select(TeamMember.id).where(TeamMember.user_id == int(user_id))):
            raise ValueError("Вы в команде — сначала выйдите из неё, потом можно стать зрителем.")
    row = await session.scalar(select(UserKind).where(UserKind.user_id == int(user_id)))
    if row is None:
        session.add(UserKind(user_id=int(user_id), kind=kind))
    else:
        row.kind = kind
    return kind


async def is_viewer(user_id: int) -> bool:
    async with session_scope() as s:
        kind, _ = await kind_of(s, user_id)
    return kind == VIEWER


async def follows(session, user_id: int) -> list[int]:
    return [int(x) for x in (await session.execute(select(TeamFollow.team_id).where(TeamFollow.user_id == int(user_id)))).scalars().all()]


async def set_follow(session, user_id: int, team_id: int, on: bool) -> None:
    if on:
        if not await session.scalar(select(TeamFollow.id).where(TeamFollow.user_id == int(user_id), TeamFollow.team_id == int(team_id))):
            session.add(TeamFollow(user_id=int(user_id), team_id=int(team_id)))
    else:
        await session.execute(delete(TeamFollow).where(TeamFollow.user_id == int(user_id), TeamFollow.team_id == int(team_id)))


async def follower_telegram_ids(session, team_ids: list[int | None], exclude_user_ids: set[int] | None = None) -> list[int]:
    ids = [int(t) for t in team_ids if t]
    if not ids:
        return []
    rows = (await session.execute(
        select(User.id, User.telegram_id).join(TeamFollow, TeamFollow.user_id == User.id).where(TeamFollow.team_id.in_(ids))
    )).all()
    ex = exclude_user_ids or set()
    return sorted({int(tg) for uid, tg in rows if int(uid) not in ex})


# ---------------------------------------------------------------------------
# Чат матча
# ---------------------------------------------------------------------------

async def chat_access(session, m: TournamentMatch, user_id: int, is_admin: bool) -> tuple[bool, bool, str | None]:
    """(может читать, может писать, причина). Читают игроки двух команд и админы; открыт за час до матча."""
    from services.tournament_engine import timeutil

    team_id = await session.scalar(select(TeamMember.team_id).where(TeamMember.user_id == int(user_id)).limit(1))
    participant = bool(team_id and team_id in (m.team1_id, m.team2_id))
    if not (participant or is_admin):
        return False, False, "Чат матча доступен только игрокам двух команд и организаторам."
    at = timeutil.as_utc(m.scheduled_at)
    opened = is_admin or (at is not None and timeutil.now_utc() >= at - CHAT_OPENS_BEFORE)
    if not opened:
        return False, False, "Чат откроется за час до начала матча." if at else "Чат откроется за час до матча — время ещё не назначено."
    return True, m.status != "FINISHED" or is_admin, None


async def open_due_chats(now=None) -> list[dict[str, Any]]:
    """За час до матча: системное сообщение с лобби в чат матча (один раз) + кого уведомить."""
    from config import GLOBAL_CONFIG
    from services.tournament_engine import timeutil
    from services.tournament_engine.service import _team_names, match_team_members
    from services.tournament_extras import lobby_for

    now = now or timeutil.now_utc()
    out = []
    async with session_scope() as s:
        rows = (await s.execute(select(TournamentMatch).where(
            TournamentMatch.status.in_(("SCHEDULED", "POSTPONED")), TournamentMatch.scheduled_at.is_not(None),
            TournamentMatch.team1_id.is_not(None), TournamentMatch.team2_id.is_not(None)))).scalars().all()
        for m in rows:
            at = timeutil.as_utc(m.scheduled_at)
            if not (at - CHAT_OPENS_BEFORE <= now <= at):
                continue
            logged = (await s.execute(select(TournamentMatchLog.new_json).where(
                TournamentMatchLog.match_id == m.id, TournamentMatchLog.action == "chat_open"))).scalars().all()
            if any(at.isoformat() in (x or "") for x in logged):
                continue
            names = await _team_names(s, {m.team1_id, m.team2_id})
            lobby = lobby_for(m, GLOBAL_CONFIG.bot_token)
            s.add(MatchChatMessage(match_id=m.id, user_id=None, is_system=True, text=(
                f"Чат матча {m.match_no}: {names.get(m.team1_id)} — {names.get(m.team2_id)}, начало {timeutil.fmt_local(at, label=True)}.\n"
                f"Лобби: {lobby['name']}\nПароль: {lobby['password']}\n"
                f"Лобби создаёт капитан «{names.get(m.team1_id)}». Когда команда в сборе — нажмите «Мы готовы» в напоминании.")))
            s.add(TournamentMatchLog(match_id=m.id, stage_id=m.stage_id, action="chat_open", new_json=f'{{"scheduled_at": "{at.isoformat()}"}}'))
            members = await match_team_members(s, m)
            out.append({"match_id": int(m.id), "match_no": m.match_no, "text": f"{names.get(m.team1_id)} — {names.get(m.team2_id)}",
                        "when": timeutil.fmt_local(at, label=True), "telegram_ids": [tg for _, tg in members]})
        await s.commit()
    return out
