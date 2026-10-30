"""💬 Админ: тикеты поддержки — список, карточка."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from database.models import TicketStatus
from database.repositories import SupportRepository, UserRepository
from handlers.admin.panel import _deny
from handlers.helpers import db_session, parse_cb, safe_html
from keyboards.admin import support_ticket_kb
from utils.formatting import fmt_dt

logger = logging.getLogger("ADMIN")

router = Router(name="admin_support")


@router.callback_query(F.data == "adm:support")
async def cb_support_list(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    async with db_session() as session:
        tickets = await SupportRepository(session).list_open(limit=30)
        users = UserRepository(session)
        rows = []
        lines = [f"💬 <b>Обращения в поддержку ({len(tickets)})</b>", ""]
        if not tickets:
            lines.append("Открытых обращений нет.")
        for t in tickets:
            u = await users.get_by_id(t.user_id)
            who = (u.username or str(u.telegram_id)) if u else f"user:{t.user_id}"
            preview = (t.message[:60] + "…") if len(t.message) > 60 else t.message
            lines.append(f"#{t.id} {safe_html(who)}: {safe_html(preview)}")
            rows.append([InlineKeyboardButton(
                text=f"#{t.id} {who[:24]}", callback_data=f"sup:ticket:{t.id}"
            )])
    rows.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="adm:menu")])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] support list edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("sup:ticket:"))
async def cb_support_ticket(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    ticket_id = int(parts[2])
    async with db_session() as session:
        repo = SupportRepository(session)
        ticket = await repo.get_by_id(ticket_id)
        if ticket is None:
            await callback.answer("Тикет не найден.", show_alert=True)
            return
        user = await UserRepository(session).get_by_id(ticket.user_id)
    who = (user.username or str(user.telegram_id)) if user else f"user:{ticket.user_id}"
    status = "🟢 Открыт" if ticket.status == TicketStatus.OPEN else "✅ Отвечен"
    text = (
        f"💬 <b>Обращение #{ticket.id}</b>\n\n"
        f"<b>От:</b> {safe_html(who)}\n"
        f"<b>Когда:</b> {fmt_dt(ticket.created_at)}\n"
        f"<b>Статус:</b> {status}\n\n"
        f"{safe_html(ticket.message)}"
    )
    if ticket.admin_reply:
        text += f"\n\n<b>Ваш ответ:</b> {safe_html(ticket.admin_reply)}"
    kb = support_ticket_kb(ticket.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] ticket edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
