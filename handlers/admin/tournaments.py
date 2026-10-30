"""🏆 Админ: турниры — список, карточка, управление статусами."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from database.models import TournamentStatus
from database.repositories import TournamentRepository
from handlers.admin.panel import _deny
from handlers.helpers import db_session, parse_cb, safe_html
from keyboards.admin import back_to_admin_kb, tournament_admin_kb
from services.tournaments import TournamentService
from utils.formatting import fmt_dt, tournament_status_label

logger = logging.getLogger("ADMIN")

router = Router(name="admin_tournaments")


@router.callback_query(F.data == "adm:tournaments")
async def cb_tournaments(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    async with db_session() as session:
        tournaments = await TournamentRepository(session).list_all(limit=50)
    lines = [f"🏆 <b>Турниры ({len(tournaments)})</b>", ""]
    rows = []
    if not tournaments:
        lines.append("Турниров нет. Создайте новый.")
    for t in tournaments:
        lines.append(f"• #{t.id} {safe_html(t.name)} — {tournament_status_label(t.status)}")
        rows.append([InlineKeyboardButton(
            text=f"#{t.id} {t.name[:40]}", callback_data=f"adm:tournament:{t.id}"
        )])
    rows.append([InlineKeyboardButton(text="➕ Создать турнир", callback_data="adm:tcreate")])
    rows.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="adm:menu")])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] tournaments edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _render_tournament_card(callback: CallbackQuery, tournament_id: int) -> None:
    async with db_session() as session:
        t = await TournamentRepository(session).get_by_id(tournament_id)
        if t is None:
            await callback.answer("Турнир не найден.", show_alert=True)
            return
        approved = await TournamentRepository(session).approved_count(t.id)
    text = (
        f"🏆 <b>{safe_html(t.name)}</b> (id={t.id})\n\n"
        f"<b>Статус:</b> {tournament_status_label(t.status)}\n"
        f"<b>Регистрация:</b> {'🟢 открыта' if t.registration_open else '🔒 закрыта'}\n"
        f"<b>Дата:</b> {fmt_dt(t.start_date)}\n"
        f"<b>Команд одобрено:</b> {approved} / {t.max_teams}\n\n"
        f"{safe_html(t.description) if t.description else 'Без описания.'}"
    )
    kb = tournament_admin_kb(t.id, t.registration_open)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] tournament card edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm:tournament:"))
async def cb_tournament_detail(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    await _render_tournament_card(callback, int(parts[2]))
    await callback.answer()


_STATUS_ACTIONS = {
    "trn:open": (TournamentStatus.REGISTRATION, "🟢 Регистрация открыта."),
    "trn:close": (TournamentStatus.DRAFT, "🔒 Регистрация закрыта (черновик)."),
    "trn:start": (TournamentStatus.ACTIVE, "🔵 Турнир начат."),
    "trn:finish": (TournamentStatus.FINISHED, "🏁 Турнир завершён."),
    "trn:cancel": (TournamentStatus.CANCELLED, "⛔ Турнир отменён."),
}


@router.callback_query(F.data.startswith(("trn:open:", "trn:close:", "trn:start:", "trn:finish:", "trn:cancel:")))
async def cb_tournament_action(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    action = f"{parts[0]}:{parts[1]}"
    status, label = _STATUS_ACTIONS[action]
    tournament_id = int(parts[2])
    try:
        async with db_session() as session:
            svc = TournamentService(session)
            await svc.set_status(tournament_id, status)
    except ValueError as e:
        await callback.answer(f"⚠️ {e}", show_alert=True)
        return
    logger.info("[TOURNAMENT] Admin set id=%s status=%s", tournament_id, status.value)
    await _render_tournament_card(callback, tournament_id)
    if svc.finished_now:  # 5.13: картинка с итогами — админам, игрокам, болельщикам
        from miniapp_v2.common import get_config
        from services.results_card import publish

        await publish(get_config(), tournament_id)
    if svc.autopair:
        from miniapp_v2 import notify
        from miniapp_v2.common import get_config

        notify.send_autopair(get_config(), svc.autopair)
        await callback.answer(f"{label}\n{svc.autopair.get('text') or ''}"[:190], show_alert=True)
        return
    await callback.answer(label)
