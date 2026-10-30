"""🤖 AI-помощник в Telegram-боте.

Режим включается кнопкой «🤖 AI-помощник» или командой /ai. Пока режим активен,
текстовые сообщения уходят помощнику. Любая кнопка главного меню или команда
выключает режим и обрабатывается как обычно (SkipHandler передаёт её дальше).
Роутер регистрируется ПЕРВЫМ: его хендлеры срабатывают только в своём состоянии.
"""
from __future__ import annotations

import logging
import time

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from handlers.common import get_config
from keyboards.main import (
    BTN_ADMIN,
    BTN_AI,
    BTN_HOME,
    BTN_MY_APPLICATION,
    BTN_MY_PROFILE,
    BTN_MY_TEAM,
    BTN_SUPPORT,
    BTN_TOURNAMENTS,
)
from utils.formatting import escape

logger = logging.getLogger("AI_CHAT")
router = Router(name="ai_chat")

MAIN_BUTTONS = {BTN_HOME, BTN_TOURNAMENTS, BTN_MY_TEAM, BTN_MY_APPLICATION, BTN_MY_PROFILE, BTN_SUPPORT, BTN_ADMIN,
                "📺 Трансляция", "🎮 Стать игроком"}
MIN_INTERVAL = 8.0
_last_question: dict[int, float] = {}


class AIChatStates(StatesGroup):
    chatting = State()


def exit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Выйти из помощника", callback_data="ai:exit")]])


def _configured() -> bool:
    cfg = get_config()
    return bool((cfg.ai_api_key or "").strip() or (cfg.ai_backup_api_key or "").strip())


@router.message(Command("ai"))
@router.message(F.text == BTN_AI)
async def ai_enter(message: Message, state: FSMContext) -> None:
    from services import ai_budget

    if message.from_user is None:
        return
    if not _configured():
        await message.answer("🤖 AI-помощник пока не настроен. Напишите организаторам: «💬 Связь с организаторами».")
        return
    await state.set_state(AIChatStates.chatting)
    await state.update_data(ai_history=[])
    left_user, left_global = ai_budget.bot_remaining(message.from_user.id)
    left = min(left_user, left_global)
    await message.answer(
        "🤖 <b>AI-помощник TI:IT TOP</b>\n\n"
        "Спросите про регистрацию, MMR, команду, заявку или турнир — помощник видит ваш профиль и публичные турниры. "
        "Данные других игроков ему недоступны.\n\n"
        f"Вопросов на сегодня: <b>{left}</b>.\n"
        "Выйти — кнопка ниже или любая кнопка меню.\n\n"
        "<i>Ответы AI могут ошибаться. Решения по MMR и заявкам принимают только организаторы.</i>",
        reply_markup=exit_kb(),
    )


@router.message(AIChatStates.chatting, F.text.in_(MAIN_BUTTONS))
async def ai_leave_by_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    raise SkipHandler()


@router.message(AIChatStates.chatting, F.text.startswith("/"))
async def ai_leave_by_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    raise SkipHandler()


@router.message(AIChatStates.chatting, F.text)
async def ai_question(message: Message, state: FSMContext) -> None:
    from services import ai_budget
    from services.ai_simple import AIUnavailable
    from services.ai_user_assistant import answer

    tg = message.from_user
    if tg is None:
        return
    question = (message.text or "").strip()
    if len(question) < 2:
        await message.answer("Напишите вопрос чуть подробнее.", reply_markup=exit_kb())
        return
    now = time.monotonic()
    if now - _last_question.get(tg.id, 0.0) < MIN_INTERVAL:
        await message.answer("⏳ Подождите несколько секунд перед следующим вопросом.")
        return
    _last_question[tg.id] = now
    if not ai_budget.try_consume_bot(tg.id):
        left_user, left_global = ai_budget.bot_remaining(tg.id)
        reason = "ваш лимит вопросов на сегодня исчерпан" if left_user <= 0 else "общий лимит помощника на сегодня исчерпан"
        await message.answer(f"😔 {reason.capitalize()}. Завтра лимит обновится, а срочные вопросы можно задать организаторам через «💬 Связь с организаторами».",
                             reply_markup=exit_kb())
        return

    data = await state.get_data()
    history = list(data.get("ai_history") or [])
    waiting = await message.answer("🤖 Думаю…")
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:  # noqa: BLE001
        pass
    try:
        text = await answer(get_config(), tg.id, question, history)
    except AIUnavailable as exc:
        ai_budget.refund_bot(tg.id)
        logger.warning("[AI_CHAT] unavailable: %s", exc)
        await waiting.edit_text("😔 AI сейчас не отвечает (перегружены бесплатные сервера). Попробуйте через пару минут "
                                "или напишите организаторам: «💬 Связь с организаторами».", reply_markup=exit_kb())
        return
    except Exception:  # noqa: BLE001
        ai_budget.refund_bot(tg.id)
        logger.exception("[AI_CHAT] failed")
        await waiting.edit_text("😔 Не получилось ответить. Попробуйте ещё раз чуть позже.", reply_markup=exit_kb())
        return
    history = (history + [{"role": "user", "content": question[:1500]}, {"role": "assistant", "content": text[:1500]}])[-6:]
    await state.update_data(ai_history=history)
    left_user, left_global = ai_budget.bot_remaining(tg.id)
    footer = f"\n\n<i>Осталось вопросов сегодня: {min(left_user, left_global)}</i>"
    try:
        await waiting.edit_text("🤖 " + escape(text) + footer, reply_markup=exit_kb())
    except Exception:  # noqa: BLE001 — например, сообщение слишком длинное
        await message.answer("🤖 " + escape(text[:3500]) + footer, reply_markup=exit_kb())


@router.message(AIChatStates.chatting)
async def ai_non_text(message: Message) -> None:
    await message.answer("🤖 Помощник понимает только текст. Напишите вопрос словами.", reply_markup=exit_kb())


@router.callback_query(F.data == "ai:exit")
async def ai_exit(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() == AIChatStates.chatting.state:
        await state.clear()
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("✅ Вы вышли из AI-помощника. Главное меню — кнопки внизу.")
    except Exception:  # noqa: BLE001
        pass
    await callback.answer()
