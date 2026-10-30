"""Управление несколькими администраторами через Telegram-админку."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from config import is_admin
from database.models import AdminUser, User
from handlers.admin.panel import _deny
from handlers.common import get_config
from handlers.helpers import db_session
from keyboards.admin import back_to_admin_kb

router = Router(name="admin_admins")


class AdminManageStates(StatesGroup):
    waiting_telegram_id = State()


async def _render(callback: CallbackQuery) -> None:
    async with db_session() as session:
        rows = (await session.execute(
            select(AdminUser).where(AdminUser.active.is_(True)).order_by(AdminUser.id)
        )).scalars().all()
        lines = ["🛡 <b>Администраторы</b>", "", "Добавлять можно по Telegram ID.", ""]
        for row in rows:
            user = await session.scalar(select(User).where(User.telegram_id == row.telegram_id))
            label = f"@{user.username}" if user and user.username else (user.first_name if user else "не найден")
            lines.append(f"• <b>{label}</b> — <code>{row.telegram_id}</code>")
        if not rows:
            lines.append("Администраторов нет — это состояние блокируется до добавления первого.")
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить администратора", callback_data="adm:admins:add")],
        [InlineKeyboardButton(text="◀️ В админ-панель", callback_data="adm:menu")],
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "adm:admins")
async def cb_admins(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await _render(callback)
    await callback.answer()


@router.callback_query(F.data == "adm:admins:add")
async def cb_admin_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await state.set_state(AdminManageStates.waiting_telegram_id)
    await callback.message.answer("🛡 Введи Telegram ID нового администратора.\nОтмена — /cancel.")
    await callback.answer()


@router.message(AdminManageStates.waiting_telegram_id, F.text)
async def admin_add_input(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id, get_config()):
        await state.clear()
        await message.answer("⛔ Нет прав.")
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("⚠️ Нужен числовой Telegram ID. Например: 123456789")
        return
    telegram_id = int(raw)
    async with db_session() as session:
        row = await session.scalar(select(AdminUser).where(AdminUser.telegram_id == telegram_id))
        if row is None:
            row = AdminUser(telegram_id=telegram_id, added_by=message.from_user.id, active=True)
            session.add(row)
        else:
            row.active = True
        await session.commit()
    get_config().admin_ids.add(telegram_id)
    await state.clear()
    await message.answer(f"✅ Администратор <code>{telegram_id}</code> добавлен.", parse_mode="HTML", reply_markup=back_to_admin_kb("adm:admins"))
