"""5.13: кнопки организатора в уведомлениях — техническая победа при неявке и замены в составах."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from database.models import TournamentMatch
from database.repositories import UserRepository
from handlers.admin.panel import _deny
from handlers.helpers import db_session, safe_html

logger = logging.getLogger("ADMIN")

router = Router(name="admin_ops")


def _invalidate() -> None:
    try:
        from miniapp_v2.common import invalidate_public

        invalidate_public("tournaments:", "team:", "teams:")
    except Exception:  # noqa: BLE001
        pass


async def _append(callback: CallbackQuery, line: str) -> None:
    try:
        base = callback.message.html_text if callback.message and callback.message.html_text else ""
        await callback.message.edit_text(f"{base}\n\n{line}", parse_mode="HTML", reply_markup=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ADMIN] edit skipped: %s", exc)


@router.callback_query(F.data.startswith("tl:"))
async def cb_tech_loss(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    try:
        _, match_id, slot = (callback.data or "").split(":")
        match_id, slot = int(match_id), int(slot)
    except ValueError:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    from services.tournament_engine import service as engine

    async with db_session() as session:
        admin = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        m = await session.get(TournamentMatch, match_id)
        if m is None:
            await callback.answer("Матч не найден.", show_alert=True)
            return
        if m.status == "FINISHED":
            await callback.answer(f"Результат уже внесён: {m.score1}:{m.score2}.", show_alert=True)
            return
        try:
            await engine.set_result(session, match_id, forfeit_winner=slot, admin_user_id=admin.id if admin else None)
        except engine.EngineError as exc:
            await callback.answer(str(exc)[:190], show_alert=True)
            return
        await session.commit()
        names = await engine._team_names(session, {m.team1_id, m.team2_id})
        caps = await engine._captain_tg(session, {int(m.team1_id), int(m.team2_id)})
        winner = names.get(m.winner_team_id, "?")
        no, rnd, t1, t2 = m.match_no, m.round_name, m.team1_id, m.team2_id
    _invalidate()
    who = callback.from_user.username or callback.from_user.first_name or "организатор"
    await _append(callback, f"✅ Техническая победа: <b>{safe_html(winner)}</b> (решение: {safe_html(who)}).")
    text = (f"Матч {no} ({safe_html(rnd)}): техническая победа «{safe_html(winner)}» — соперник не подтвердил готовность "
            "и не вышел на игру. Сетка обновлена.")
    for tid in (t1, t2):
        if caps.get(int(tid)):
            try:
                await callback.bot.send_message(caps[int(tid)], text, parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
    await callback.answer("Техническая победа засчитана.")


@router.callback_query(F.data.startswith("mr:ok:"))
async def cb_report_approve(callback: CallbackQuery) -> None:
    """Результат капитана, подтверждённый данными OpenDota, — одобрить одной кнопкой."""
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    try:
        report_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    from services import tournament_extras as extras
    from services.tournament_engine import service as engine

    async with db_session() as session:
        admin = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if admin is None:
        await callback.answer("Профиль админа не найден.", show_alert=True)
        return
    try:
        out = await extras.decide_report(report_id, admin.id, True)
    except engine.EngineError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    async with db_session() as session:
        m = await session.get(TournamentMatch, out["match_id"])
        names = await engine._team_names(session, {m.team1_id, m.team2_id})
        caps = await engine._captain_tg(session, {int(m.team1_id), int(m.team2_id)})
        line = f"Матч {m.match_no}: {safe_html(names.get(m.team1_id))} {m.score1}:{m.score2} {safe_html(names.get(m.team2_id))}"
    _invalidate()
    await _append(callback, "✅ Результат утверждён.")
    for tg in caps.values():
        try:
            await callback.bot.send_message(tg, f"Результат утверждён организатором. {line}. Сетка обновлена.", parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
    await callback.answer("Результат утверждён.")


@router.callback_query(F.data.startswith(("rq:ok:", "rq:no:")))
async def cb_roster_request(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    approve = (callback.data or "").startswith("rq:ok:")
    try:
        request_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    from services import roster

    async with db_session() as session:
        admin = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        try:
            out = await roster.decide(session, request_id, approve, admin.id if admin else None)
            await session.commit()
        except ValueError as exc:
            await callback.answer(str(exc)[:190], show_alert=True)
            return
    _invalidate()
    for tg, text in out["messages"]:
        try:
            await callback.bot.send_message(tg, text, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
    await _append(callback, "✅ Одобрено." if approve else "❌ Отклонено.")
    await callback.answer("Готово.")
