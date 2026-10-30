"""👑 Админ: команды — список и карточки составов."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from database.repositories import TeamRepository, UserRepository
from handlers.admin.panel import _deny
from handlers.helpers import db_session, parse_cb, safe_html
from keyboards.admin import back_to_admin_kb
from services.teams import TeamService
from utils.formatting import format_roster, status_label

logger = logging.getLogger("ADMIN")

router = Router(name="admin_teams")


@router.callback_query(F.data == "adm:teams")
async def cb_teams(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    async with db_session() as session:
        teams = await TeamRepository(session).list_teams(limit=50)
    lines = [f"👑 <b>Команды ({len(teams)})</b>", ""]
    rows = []
    if not teams:
        lines.append("Команд пока нет.")
    for t in teams:
        lines.append(f"• {safe_html(t.name)} (id={t.id})")
        rows.append([InlineKeyboardButton(
            text=t.name[:48], callback_data=f"adm:team:{t.id}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="adm:menu")])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] teams edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm:team:"))
async def cb_team_detail(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    team_id = int(parts[2])
    async with db_session() as session:
        team = await TeamRepository(session).get_by_id(team_id)
        if team is None:
            await callback.answer("Команда не найдена.", show_alert=True)
            return
        captain = await UserRepository(session).get_by_id(team.captain_user_id)
        roster = await TeamService(session).roster_with_status(team_id)
    body = format_roster([
        (
            safe_html(u.username or u.first_name or str(u.telegram_id)),
            m.role.value,
            status_label(s),
        )
        for u, m, s, _profile in roster
    ])
    text = (
        f"👑 <b>{safe_html(team.name)}</b> (id={team.id})\n\n"
        f"{body}\n\n"
        f"<b>Капитан:</b> {safe_html((captain.username or captain.first_name) if captain else '?')} "
        f"(tg:{captain.telegram_id if captain else '?'})"
    )
    try:
        await callback.message.edit_text(text, reply_markup=back_to_admin_kb("adm:teams"), parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] team detail edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=back_to_admin_kb("adm:teams"), parse_mode="HTML")
    await callback.answer()
