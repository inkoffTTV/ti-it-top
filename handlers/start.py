"""/start, главное меню, навигация и отмена FSM."""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import is_admin
from database.repositories import UserRepository
from handlers.helpers import db_session, send_main_menu
from keyboards.main import (
    BTN_ADMIN,
    BTN_HOME,
    BTN_MY_APPLICATION,
    BTN_MY_PROFILE,
    BTN_MY_TEAM,
    BTN_SUPPORT,
    BTN_TOURNAMENTS,
    miniapp_kb,
)
from keyboards.registration import registration_start_kb

logger = logging.getLogger("START")
router = Router(name="start")

WELCOME = (
    "👋 Добро пожаловать в бот турнира <b>TI:IT TOP</b>!\n\n"
    "🏆 Dota 2 турниры • 👑 команды • 📋 заявки\n\n"
    "{reg_hint}\n\n"
    "Используйте кнопки нижнего меню."
)

REG_HINT_DONE = "✅ Регистрация пройдена. Управление — через нижнее меню."
REG_HINT_TODO = (
    "Чтобы участвовать, пройдите регистрацию: кнопка «👤 Мой профиль» → «▶️ Начать регистрацию»,\n"
    "либо нажмите кнопку ниже."
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
BANNER_PATH = STATIC_DIR / "banner.mp4"



async def _send_banner(message: Message) -> bool:
    """Отправляет локальный баннер только через Bot API sendAnimation."""
    banner = BANNER_PATH
    if not banner.is_file():
        logger.warning("[START] Баннер не найден. Ожидался static/banner.mp4")
        return False

    try:
        await message.answer_animation(animation=FSInputFile(banner))
        logger.info("[START] Banner sent as animation: %s", banner.name)
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("[START] sendAnimation failed for %s: %s", banner.name, e)
        return False


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    tg = message.from_user
    if tg is None:
        return
    async with db_session() as session:
        repo = UserRepository(session)
        user = await repo.upsert_from_telegram(
            telegram_id=tg.id,
            username=tg.username,
            first_name=tg.first_name,
            last_name=tg.last_name,
        )
        registered = user.is_registered
        await session.commit()
    logger.info("[START] User %s entered (registered=%s)", tg.id, registered)
    text = WELCOME.format(reg_hint=REG_HINT_DONE if registered else REG_HINT_TODO)
    await _send_banner(message)
    await send_main_menu(message, text)
    from handlers.common import get_config
    cfg = get_config()
    if cfg.miniapp_url:
        await message.answer("Или откройте полноценный интерфейс:", reply_markup=miniapp_kb(cfg.miniapp_url + ("&" if "?" in cfg.miniapp_url else "?") + "v=5.15.0"))
    if not registered:
        from services.audience import kind_of

        async with db_session() as session:
            kind, chosen = await kind_of(session, user.id)
        if not chosen:
            await message.answer("Кто вы?", reply_markup=_kind_kb())
        elif kind != "VIEWER":
            await message.answer("Хотите зарегистрироваться как игрок прямо сейчас?", reply_markup=registration_start_kb())


def _kind_kb():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Я игрок — участвую в турнирах", callback_data="kind:PLAYER")],
        [InlineKeyboardButton(text="Я зритель — смотрю и болею", callback_data="kind:VIEWER")],
    ])


@router.callback_query(F.data.startswith("kind:"))
async def cb_kind(callback: CallbackQuery) -> None:
    from services.audience import set_kind

    kind = callback.data.split(":", 1)[1]
    async with db_session() as session:
        user = await UserRepository(session).upsert_from_telegram(callback.from_user.id, callback.from_user.username,
                                                                  callback.from_user.first_name, callback.from_user.last_name)
        try:
            await set_kind(session, user.id, kind)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await session.commit()
    await callback.answer()
    if callback.message:
        if kind == "VIEWER":
            await send_main_menu(callback.message, "<b>Вы зритель.</b> Смотрите турниры, сетку и трансляцию, болейте за команды "
                                                   "(в Mini App на странице команды — «Болеть»). Стать игроком можно в любой момент.")
        else:
            await send_main_menu(callback.message, "<b>Вы игрок.</b> Пройдите регистрацию, чтобы вступить в команду и участвовать.")
            await callback.message.answer("Начать регистрацию:", reply_markup=registration_start_kb())


@router.message(F.text == "📺 Трансляция")
async def kb_stream(message: Message) -> None:
    from services.audience import TWITCH_URL

    await message.answer(f"<b>Трансляция турниров TI:IT TOP</b>\n{TWITCH_URL}\n\nМатчи сегодня — /today, регламент — /rules.",
                         disable_web_page_preview=False)


@router.message(F.text == "🎮 Стать игроком")
async def kb_become_player(message: Message) -> None:
    from services.audience import set_kind

    async with db_session() as session:
        user = await UserRepository(session).upsert_from_telegram(message.from_user.id, message.from_user.username,
                                                                  message.from_user.first_name, message.from_user.last_name)
        await set_kind(session, user.id, "PLAYER")
        await session.commit()
    await send_main_menu(message, "<b>Теперь вы игрок.</b> Пройдите регистрацию: привяжите Steam и укажите MMR.")
    await message.answer("Начать регистрацию:", reply_markup=registration_start_kb())


@router.message(F.text == BTN_HOME)
async def kb_home(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_banner(message)
    await send_main_menu(message, "🏠 <b>Главное меню</b>\nВыберите раздел:")


@router.message(F.text == BTN_TOURNAMENTS)
async def kb_tournaments(message: Message) -> None:
    from handlers.tournaments import show_tournaments
    await show_tournaments(message)


@router.message(F.text == BTN_MY_TEAM)
async def kb_team(message: Message) -> None:
    from handlers.team import show_my_team
    await show_my_team(message)


@router.message(F.text == BTN_MY_APPLICATION)
async def kb_application(message: Message) -> None:
    from handlers.applications import show_my_application
    await show_my_application(message)


@router.message(F.text == BTN_SUPPORT)
async def kb_support(message: Message, state: FSMContext) -> None:
    from handlers.support import start_support
    await start_support(message, state)


@router.message(F.text == BTN_ADMIN)
async def kb_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("⛔ У вас нет прав администратора.")
        return
    await state.clear()
    from handlers.admin.panel import show_admin_panel
    await show_admin_panel(message)


@router.callback_query(F.data == "nav:home")
async def cb_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        try:
            await callback.message.edit_text(
                "🏠 <b>Главное меню</b>\nВыберите раздел:",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await _send_banner(callback.message)
        await send_main_menu(
            callback.message,
            "🏠 <b>Главное меню</b>\nВыберите раздел:",
        )
    await callback.answer()


@router.callback_query(F.data == "nav:tournaments")
async def cb_tournaments(callback: CallbackQuery) -> None:
    from handlers.tournaments import render_tournaments_list
    await render_tournaments_list(callback)
    await callback.answer()


@router.callback_query(F.data == "action:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "❌ Действие отменено.\nВернуться можно через главное меню."
        )
    await callback.answer()


@router.callback_query(F.data == "reg:start")
async def cb_reg_start(callback: CallbackQuery, state: FSMContext) -> None:
    from handlers.profile import begin_registration
    await begin_registration(callback, state)
    await callback.answer()
