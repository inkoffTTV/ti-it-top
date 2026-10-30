"""/matches — ближайшие матчи команды игрока прямо в боте (время по Москве)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from database.engine import session_scope
from database.models import TeamMember, Tournament, TournamentMatch, User
from utils.formatting import escape

logger = logging.getLogger("MATCHES")
router = Router(name="matches")


async def team_matches_text(telegram_id: int) -> str:
    from services.tournament_engine import timeutil
    from services.tournament_engine.service import STATUS_TEXT, _team_names

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.telegram_id == int(telegram_id)))
        team_id = await session.scalar(select(TeamMember.team_id).where(TeamMember.user_id == user.id).limit(1)) if user else None
        if not team_id:
            return "👥 Вы пока не в команде — матчей нет.\nНайти команду: «👑 Моя команда» или поиск в Mini App."
        rows = (await session.execute(
            select(TournamentMatch, Tournament).join(Tournament, Tournament.id == TournamentMatch.tournament_id)
            .where((TournamentMatch.team1_id == team_id) | (TournamentMatch.team2_id == team_id))
            .order_by(TournamentMatch.status == "FINISHED", TournamentMatch.scheduled_at.is_(None),
                      TournamentMatch.scheduled_at, TournamentMatch.match_no)
            .limit(12)
        )).all()
        names = await _team_names(session, {x for m, _ in rows for x in (m.team1_id, m.team2_id) if x} | {int(team_id)})
    if not rows:
        return f"📅 У команды «{escape(names.get(int(team_id), ''))}» пока нет матчей в сетке."
    lines = [f"📅 <b>Матчи команды «{escape(names.get(int(team_id), ''))}»</b> (время МСК)\n"]
    for m, t in rows:
        opp_id = m.team2_id if m.team1_id == team_id else m.team1_id
        opp = escape(names.get(opp_id, "?")) if opp_id else "соперник определится позже"
        when = timeutil.fmt_local(m.scheduled_at) or "время не назначено"
        status = STATUS_TEXT.get(m.status, m.status)
        if m.status == "FINISHED":
            mine, theirs = (m.score1, m.score2) if m.team1_id == team_id else (m.score2, m.score1)
            res = "победа" if m.winner_team_id == team_id else ("ничья" if m.winner_team_id is None else "поражение")
            lines.append(f"✅ Матч {m.match_no} · {escape(m.round_name or '')}: vs {opp} — {mine}:{theirs}, {res}")
        else:
            icon = "🔴" if m.status == "LIVE" else ("⏰" if m.status == "POSTPONED" else "•")
            lines.append(f"{icon} Матч {m.match_no} · {escape(m.round_name or '')} · BO{m.best_of}\n   vs {opp} — {when} · {status}")
    lines.append("\nСетка и полное расписание — Mini App → Турниры. За 30 минут до матча придёт напоминание.")
    return "\n".join(lines)


async def today_text() -> str:
    """Все матчи сегодня (по Москве) во всех открытых турнирах: время, пары, статус, счёт."""
    from services.tournament_engine import timeutil
    from services.tournament_engine.service import STATUS_TEXT, _team_names
    from database.models import TournamentStatus

    now_local = timeutil.to_local(timeutil.now_utc())
    day = now_local.strftime("%d.%m.%Y")
    async with session_scope() as session:
        rows = (await session.execute(
            select(TournamentMatch, Tournament).join(Tournament, Tournament.id == TournamentMatch.tournament_id)
            .where(TournamentMatch.scheduled_at.is_not(None), Tournament.status != TournamentStatus.DRAFT)
        )).all()
        todays = [(m, t) for m, t in rows if timeutil.fmt_local(m.scheduled_at, label=False).startswith(day)]
        todays.sort(key=lambda p: timeutil.as_utc(p[0].scheduled_at))
        names = await _team_names(session, {x for m, _ in todays for x in (m.team1_id, m.team2_id) if x})
    if not todays:
        return f"📅 Сегодня ({day}) матчей нет.\nМои матчи — /matches"
    lines = [f"📅 <b>Матчи сегодня, {day}</b> (МСК)\n"]
    for m, t in todays:
        a = escape(names.get(m.team1_id, "TBD")) if m.team1_id else "TBD"
        b = escape(names.get(m.team2_id, "TBD")) if m.team2_id else "TBD"
        time_ = timeutil.to_local(m.scheduled_at).strftime("%H:%M")
        tail = f"{m.score1}:{m.score2}" if m.status == "FINISHED" else STATUS_TEXT.get(m.status, m.status)
        lines.append(f"{time_} · {a} — {b} · BO{m.best_of} · {tail}\n   {escape(t.name)} · {escape(m.round_name or '')}")
    lines.append("\nМои матчи — /matches")
    return "\n".join(lines)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    try:
        await message.answer(await today_text(), disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        logger.exception("[MATCHES] today failed")
        await message.answer("😔 Не удалось загрузить расписание. Попробуйте позже.")


@router.callback_query(F.data.startswith("ready:"))
async def cb_ready(callback: CallbackQuery) -> None:
    from services.tournament_extras import mark_ready

    try:
        ok, text = await mark_ready(int(callback.data.split(":", 1)[1]), callback.from_user.id)
    except (ValueError, IndexError):
        ok, text = False, "Кнопка устарела."
    await callback.answer(text, show_alert=not ok)
    if ok and callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass


@router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    """Регламент: описание текущего открытого турнира (редактируется в Mini App)."""
    from database.models import TournamentStatus

    async with session_scope() as s:
        t = await s.scalar(select(Tournament).where(Tournament.status.in_((TournamentStatus.REGISTRATION, TournamentStatus.ACTIVE)))
                           .order_by(Tournament.id.desc()).limit(1))
    if t is None or not (t.description or "").strip():
        await message.answer("Регламент появится вместе с ближайшим турниром.")
        return
    await message.answer(f"<b>{escape(t.name)}</b>\n\n{escape(t.description)}"[:4000], disable_web_page_preview=True)


@router.message(Command("matches"))
async def cmd_matches(message: Message) -> None:
    if message.from_user is None:
        return
    try:
        await message.answer(await team_matches_text(message.from_user.id), disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        logger.exception("[MATCHES] failed")
        await message.answer("😔 Не удалось загрузить матчи. Попробуйте позже.")
