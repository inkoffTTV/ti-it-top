"""📢 Админская рассылка пользователям бота."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import F, Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from database.models import BroadcastLog, User
from handlers.admin.panel import _deny
from handlers.common import get_config
from handlers.helpers import db_session, safe_html

logger = logging.getLogger("ADMIN_BROADCAST")

router = Router(name="admin_broadcast")


class BroadcastStates(StatesGroup):
    waiting_message = State()


def broadcast_actions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить всем", callback_data="adm:broadcast:send"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data="adm:broadcast:edit"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:broadcast:cancel")],
        ]
    )


def broadcast_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Новая рассылка", callback_data="adm:broadcast:new")],
            [InlineKeyboardButton(text="📜 История рассылок", callback_data="adm:broadcast:history")],
            [InlineKeyboardButton(text="◀️ В админ-панель", callback_data="adm:menu")],
        ]
    )


def _content_label(message: Message) -> str:
    if message.animation:
        return "GIF/анимация"
    if message.photo:
        return "изображение"
    if message.video:
        return "видео"
    if message.document:
        return "файл"
    if message.audio:
        return "аудио"
    if message.voice:
        return "голосовое"
    if message.text:
        return "текст"
    return "сообщение"


async def _send_copy(bot: Bot, target_id: int, source_chat_id: int, source_message_id: int) -> None:
    """Копирует сообщение без скачивания файла и с сохранением медиа/подписи."""
    await bot.copy_message(
        chat_id=target_id,
        from_chat_id=source_chat_id,
        message_id=source_message_id,
    )


@router.callback_query(F.data == "adm:broadcast")
async def cb_broadcast_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Здесь можно отправить пользователям текст, фото, GIF, видео или файл.\n"
        "Сообщение сначала будет показано тебе для подтверждения.",
        reply_markup=broadcast_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "adm:broadcast:new")
async def cb_broadcast_new(callback: CallbackQuery, state: FSMContext) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await state.set_state(BroadcastStates.waiting_message)
    await callback.message.edit_text(
        "📢 <b>Новая рассылка</b>\n\n"
        "Отправь следующим сообщением то, что нужно разослать пользователям.\n\n"
        "Можно отправить:\n"
        "• текст\n"
        "• фото\n"
        "• GIF/анимацию\n"
        "• видео\n"
        "• файл\n\n"
        "Отмена — /cancel или кнопка ниже.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm:broadcast:cancel")]]
        ),
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_message)
async def broadcast_message_received(message: Message, state: FSMContext) -> None:
    if not get_config().admin_ids or message.from_user is None or message.from_user.id not in get_config().admin_ids:
        await state.clear()
        return

    # Сохраняем только ссылку на исходное сообщение в чате администратора.
    # Сам файл/медиа не скачивается.
    await state.update_data(
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        content_type=_content_label(message),
    )

    await message.answer(
        "📢 <b>Предпросмотр рассылки</b>\n\n"
        f"Тип: <b>{safe_html(_content_label(message))}</b>\n\n"
        "Ниже показано сообщение, которое будет отправлено пользователям.",
        parse_mode="HTML",
    )

    try:
        await message.bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramAPIError as e:
        logger.warning("[BROADCAST] Preview copy failed: %s", e)
        await message.answer("⚠️ Не удалось показать копию сообщения, но его можно отправить.")

    await message.answer(
        "Отправить это сообщение всем пользователям бота?",
        reply_markup=broadcast_actions_kb(),
    )


@router.callback_query(F.data == "adm:broadcast:edit")
async def cb_broadcast_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await state.set_state(BroadcastStates.waiting_message)
    await callback.message.answer(
        "✏️ Отправь новое сообщение для рассылки.\n\n"
        "Можно использовать текст, фото, GIF, видео или файл."
    )
    await callback.answer()


@router.callback_query(F.data == "adm:broadcast:cancel")
async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "📢 <b>Рассылка отменена.</b>",
        reply_markup=broadcast_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "adm:broadcast:send")
async def cb_broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return

    data = await state.get_data()
    source_chat_id = data.get("source_chat_id")
    source_message_id = data.get("source_message_id")
    content_type = data.get("content_type", "сообщение")

    if not source_chat_id or not source_message_id:
        await state.clear()
        await callback.answer("Рассылка устарела. Создай её заново.", show_alert=True)
        return

    await callback.answer("Рассылка запущена…")
    await callback.message.edit_text(
        "📢 <b>Рассылка запущена</b>\n\n"
        "Сообщение отправляется пользователям. Статистику пришлю после завершения.",
        parse_mode="HTML",
    )

    bot = callback.bot

    async with db_session() as session:
        result = await session.execute(select(User.telegram_id).order_by(User.id))
        user_ids = [int(x) for x in result.scalars().all() if x]

    total = len(user_ids)
    sent = 0
    blocked = 0
    failed = 0

    for telegram_id in user_ids:
        try:
            await _send_copy(bot, telegram_id, int(source_chat_id), int(source_message_id))
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
            try:
                await _send_copy(bot, telegram_id, int(source_chat_id), int(source_message_id))
                sent += 1
            except TelegramForbiddenError:
                blocked += 1
            except Exception as retry_error:  # noqa: BLE001
                failed += 1
                logger.warning("[BROADCAST] Retry failed user=%s: %s", telegram_id, retry_error)
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning("[BROADCAST] Send failed user=%s: %s", telegram_id, e)

        # Держимся заметно ниже лимита Telegram на массовую отправку.
        await asyncio.sleep(0.05)

    async with db_session() as session:
        log = BroadcastLog(
            admin_user_id=callback.from_user.id,
            content_type=str(content_type),
            total=total,
            sent=sent,
            blocked=blocked,
            failed=failed,
        )
        session.add(log)
        await session.commit()

    await state.clear()

    await callback.message.answer(
        "📊 <b>Рассылка завершена</b>\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"✅ Доставлено: {sent}\n"
        f"🚫 Заблокировали бота: {blocked}\n"
        f"❌ Ошибок: {failed}\n\n"
        f"📦 Тип сообщения: {safe_html(str(content_type))}",
        reply_markup=broadcast_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm:broadcast:history")
async def cb_broadcast_history(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return

    async with db_session() as session:
        result = await session.execute(
            select(BroadcastLog).order_by(BroadcastLog.id.desc()).limit(10)
        )
        rows = list(result.scalars().all())

    if not rows:
        text = "📜 <b>История рассылок</b>\n\nПока рассылок не было."
    else:
        lines = ["📜 <b>Последние рассылки</b>", ""]
        for row in rows:
            dt = row.created_at.strftime("%d.%m.%Y %H:%M") if row.created_at else "—"
            lines.append(
                f"🕒 {dt}\n"
                f"📦 {safe_html(row.content_type)}\n"
                f"👥 {row.total} → ✅ {row.sent} | 🚫 {row.blocked} | ❌ {row.failed}\n"
            )
        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        reply_markup=broadcast_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(F.text == "/cancel")
async def broadcast_cancel_command(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.from_user.id not in get_config().admin_ids:
        return
    current = await state.get_state()
    if current != BroadcastStates.waiting_message.state:
        return
    await state.clear()
    await message.answer("📢 Рассылка отменена.", reply_markup=broadcast_menu_kb())
