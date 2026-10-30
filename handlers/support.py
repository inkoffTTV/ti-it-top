"""💬 Связь с организаторами: тикеты поддержки и ответы админов."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from database.repositories import UserRepository
from handlers.common import AdminReplyStates, SupportStates, get_config
from handlers.helpers import db_session, notify_admins, safe_html
from keyboards.main import (
    BTN_ADMIN,
    BTN_AI,
    BTN_HOME,
    BTN_MY_APPLICATION,
    BTN_MY_PROFILE,
    BTN_MY_TEAM,
    BTN_SUPPORT,
    BTN_TOURNAMENTS,
    back_cancel_kb,
    home_kb,
)
from services.support import SupportService
from utils.validation import validate_support_message

logger = logging.getLogger("SUPPORT")

router = Router(name="support")


async def start_support(message: Message, state: FSMContext) -> None:
    """Открывает ввод сообщения в поддержку (FSM)."""
    tg = message.from_user
    if tg is None or state is None:
        return
    await state.clear()
    async with db_session() as session:
        await UserRepository(session).upsert_from_telegram(
            tg.id, tg.username, tg.first_name, tg.last_name
        )
        await session.commit()
    await state.set_state(SupportStates.waiting_message)
    await message.answer(
        "💬 Напишите ваше сообщение организаторам одним сообщением.\n\n"
        "Ответ придёт в этот чат, когда организаторы обработают обращение.\n"
        "Отмена — «❌ Отмена» или «🏠 Главное меню».",
        reply_markup=back_cancel_kb(),
    )


@router.message(F.text == "💬 Связь с организаторами")
async def kb_support(message: Message, state: FSMContext) -> None:
    await start_support(message, state)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=home_kb())


_MENU_BUTTONS = {BTN_HOME, BTN_TOURNAMENTS, BTN_MY_TEAM, BTN_MY_APPLICATION, BTN_MY_PROFILE, BTN_SUPPORT, BTN_ADMIN, BTN_AI,
                 "📺 Трансляция", "🎮 Стать игроком"}


@router.message(SupportStates.waiting_message, F.text.in_(_MENU_BUTTONS))
@router.message(AdminReplyStates.waiting_reply, F.text.in_(_MENU_BUTTONS))
@router.message(SupportStates.waiting_message, F.text.startswith("/"))
async def leave_support_by_menu(message: Message, state: FSMContext) -> None:
    """Кнопка меню или команда во время ввода обращения — не обращение: выходим и передаём дальше.
    Раньше текст кнопки («🏠 Главное меню») уходил организаторам как тикет."""
    await state.clear()
    raise SkipHandler()


@router.message(SupportStates.waiting_message, F.text)
async def process_support_message(message: Message, state: FSMContext) -> None:
    tg = message.from_user
    ok, text, error = validate_support_message(message.text or "")
    if not ok:
        await message.answer(f"⚠️ {error}\nПопробуйте ещё раз или нажмите «❌ Отмена».")
        return
    await state.clear()
    config = get_config()
    async with db_session() as session:
        users = UserRepository(session)
        user = await users.upsert_from_telegram(tg.id, tg.username, tg.first_name, tg.last_name)
        svc = SupportService(session)
        ticket = await svc.create_ticket(user.id, text)
        mention = f"@{user.username}" if user.username else f"user id={user.id}"
    admin_text = (
        f"💬 <b>ОБРАЩЕНИЕ В ПОДДЕРЖКУ #{ticket.id}</b>\n\n"
        f"<b>От:</b> {safe_html(mention)} (tg:{tg.id})\n\n"
        f"{safe_html(text)}\n\n"
        f"Ответить: /reply {ticket.id} <текст>"
    )
    await notify_admins(message.bot, config, admin_text)
    await message.answer(
        "✅ Сообщение отправлено организаторам.\n"
        f"Номер обращения: <code>{ticket.id}</code>.\n"
        "Ответ придёт в этот чат.",
        reply_markup=home_kb(),
        parse_mode="HTML",
    )
    logger.info("[SUPPORT] Ticket %s created by tg %s", ticket.id, tg.id)


# ---------------------------------------------------------------------------
# Ответ администратора: /reply <ticket_id> <текст>
# ---------------------------------------------------------------------------

@router.message(Command("reply"))
async def cmd_reply(message: Message, state: FSMContext) -> None:
    from config import is_admin

    if not is_admin(message.from_user.id if message.from_user else None, get_config()):
        await message.answer("⛔ Команда доступна только организаторам.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: <code>/reply &lt;номер&gt; &lt;текст ответа&gt;</code>", parse_mode="HTML")
        return
    ticket_id = int(parts[1])
    reply_text = parts[2]
    ok, clean, error = validate_support_message(reply_text)
    if not ok:
        await message.answer(f"⚠️ {error}")
        return
    config = get_config()
    async with db_session() as session:
        svc = SupportService(session)
        try:
            ticket = await svc.answer(ticket_id, clean, message.from_user.id)
        except ValueError as e:
            await message.answer(f"⚠️ {safe_html(str(e))}")
            return
        user = await UserRepository(session).get_by_id(ticket.user_id)
        from services.activity import add_event
        await add_event(session, kind="support_reply", title="Ответ поддержки", body=clean, target_user_id=user.id if user else None)
        await session.commit()
    if user is None:
        await message.answer("⚠️ Пользователь не найден.")
        return
    try:
        await message.bot.send_message(
            user.telegram_id,
            f"💬 <b>Ответ организаторов</b> по обращению #{ticket_id}:\n\n{safe_html(clean)}",
            parse_mode="HTML",
        )
    except Exception as e:  # noqa: BLE001 — пользователь мог заблокировать бота
        logger.warning("[SUPPORT] Cannot deliver reply to tg %s: %s", user.telegram_id, e)
        await message.answer("⚠️ Пользователь не получил ответ (возможно, заблокировал бота).")
        return
    await message.answer(f"✅ Ответ по обращению #{ticket_id} доставлен пользователю.")
    logger.info("[SUPPORT] Ticket %s answered via /reply", ticket_id)


# Inline-ответ из карточки тикета (админ жмёт «✉️ Ответить») — ввод через FSM.
@router.callback_query(F.data.startswith("sup:reply:"))
async def cb_support_reply(callback: CallbackQuery, state: FSMContext) -> None:
    from config import is_admin
    from handlers.helpers import parse_cb

    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    if not is_admin(callback.from_user.id, get_config()):
        await callback.answer("Только для организаторов.", show_alert=True)
        return
    await state.update_data(admin_ticket_id=int(parts[2]))
    await state.set_state(AdminReplyStates.waiting_reply)
    await callback.message.answer(
        f"✉️ Введите текст ответа по обращению #{parts[2]} одним сообщением.\n"
        "Отмена — /cancel."
    )
    await callback.answer()


@router.message(AdminReplyStates.waiting_reply, F.text)
async def process_admin_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ticket_id = data.get("admin_ticket_id")
    await state.clear()
    if ticket_id is None:
        await message.answer("Сессия устарела. Используйте /reply <номер> <текст>.")
        return
    ok, clean, error = validate_support_message(message.text or "")
    if not ok:
        await message.answer(f"⚠️ {error}")
        return
    async with db_session() as session:
        svc = SupportService(session)
        try:
            ticket = await svc.answer(int(ticket_id), clean, message.from_user.id)
        except ValueError as e:
            await message.answer(f"⚠️ {safe_html(str(e))}")
            return
        user = await UserRepository(session).get_by_id(ticket.user_id)
        from services.activity import add_event
        await add_event(session, kind="support_reply", title="Ответ поддержки", body=clean, target_user_id=user.id if user else None)
        await session.commit()
    if user is None:
        await message.answer("⚠️ Пользователь не найден.")
        return
    try:
        await message.bot.send_message(
            user.telegram_id,
            f"💬 <b>Ответ организаторов</b> по обращению #{ticket_id}:\n\n{safe_html(clean)}",
            parse_mode="HTML",
        )
        await message.answer(f"✅ Ответ по обращению #{ticket_id} доставлен.")
    except Exception as e:  # noqa: BLE001
        logger.warning("[SUPPORT] Cannot deliver reply: %s", e)
        await message.answer("⚠️ Ответ сохранён, но пользователь его не получил (заблокировал бота?).")
