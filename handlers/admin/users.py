"""👥 Админ: пользователи — список, поиск, карточка."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.models import VerificationStatus
from database.repositories import ProfileRepository, TeamRepository, UserRepository
from handlers.admin.panel import _deny
from handlers.helpers import db_session, parse_cb, safe_html
from keyboards.admin import search_user_kb, user_detail_kb
from services.applications import ApplicationService
from utils.formatting import profile_card

logger = logging.getLogger("ADMIN")

router = Router(name="admin_users")


class AdminUserSearchStates(StatesGroup):
    waiting_query = State()


async def render_user_list(callback: CallbackQuery) -> None:
    async with db_session() as session:
        users = await UserRepository(session).list_users(limit=30)
    lines = ["👥 <b>Пользователи (последние 30)</b>", ""]
    if not users:
        lines.append("Пока никого. Пользователи появятся после /start.")
    for u in users:
        name = u.username or u.first_name or "без имени"
        reg = "✅" if u.is_registered else "⚪"
        lines.append(f"{reg} {safe_html(name)} — tg:{u.telegram_id} (id={u.id})")
    text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=search_user_kb(), parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] user list edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=search_user_kb(), parse_mode="HTML")


@router.callback_query(F.data == "adm:users")
async def cb_users(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await render_user_list(callback)
    await callback.answer()


@router.callback_query(F.data == "adm:usearch")
async def cb_user_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await state.set_state(AdminUserSearchStates.waiting_query)
    await callback.message.edit_text(
        "🔎 Введите @username, имя или фрагмент.\nОтмена — /cancel."
    )
    await callback.answer()


@router.message(AdminUserSearchStates.waiting_query, F.text)
async def cb_user_search_input(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    await state.clear()
    async with db_session() as session:
        found = await UserRepository(session).search(query, limit=20)
    lines = [f"🔎 Результаты по запросу «{safe_html(query)}»: ({len(found)})", ""]
    kb_rows = []
    for u in found[:15]:
        name = u.username or u.first_name or "без имени"
        lines.append(f"• {safe_html(name)} — tg:{u.telegram_id}")
        kb_rows.append([InlineKeyboardButton(
            text=f"{name} ({u.telegram_id})", callback_data=f"adm:user:{u.id}"
        )])
    kb_rows.append([InlineKeyboardButton(text="◀️ К пользователям", callback_data="adm:users")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


async def render_user_detail(target: CallbackQuery, user_db_id: int) -> None:
    async with db_session() as session:
        users = UserRepository(session)
        user = await users.get_by_id(user_db_id)
        if user is None:
            await target.answer("Пользователь не найден.", show_alert=True)
            return
        profile = await ProfileRepository(session).get_by_user_id(user.id)
        team = await TeamRepository(session).get_for_user(user.id)
        app = await ApplicationService(session).my_application(user.id)
    card = profile_card(
        tg_username=user.username,
        tg_id=user.telegram_id,
        steam_nickname=profile.steam_nickname if profile else None,
        steam_id64=profile.steam_id64 if profile else None,
        account_id=profile.account_id if profile else None,
        verification_status=profile.verification_status if profile else VerificationStatus.UNVERIFIED,
        source=profile.verification_source if profile else None,
        mmr=profile.mmr if profile else None,
        matches=profile.matches if profile else None,
        winrate=profile.winrate if profile else None,
        team_name=team.name if team else None,
        application_status=app.status if app else None,
        hours_total=profile.hours_total if profile else None,
        approved_mmr=profile.approved_mmr if profile else None,
    )
    extra = ""
    if profile and profile.admin_comment:
        extra = f"\n<b>Комментарий админа:</b> {safe_html(profile.admin_comment)}"
    text = card + extra
    kb = user_detail_kb(user.id)
    try:
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] user detail edit skipped: %s", e)
        await target.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm:user:"))
async def cb_user_detail(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    from handlers.helpers import parse_cb

    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    await render_user_detail(callback, int(parts[2]))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:urecheck:"))
async def cb_user_recheck(callback: CallbackQuery) -> None:
    """Быстрая повторная авто-проверка игрока из карточки пользователя."""
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    from handlers.helpers import parse_cb
    from handlers.admin.verification import recheck_profile_by_user

    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    result_msg = await recheck_profile_by_user(int(parts[2]))
    await callback.answer(result_msg, show_alert=True)
