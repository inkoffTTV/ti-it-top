"""Главная нижняя Reply-клавиатура и общие inline-кнопки навигации."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BTN_HOME = "🏠 Главное меню"
BTN_TOURNAMENTS = "🏆 Турниры"
BTN_MY_TEAM = "👑 Моя команда"
BTN_MY_APPLICATION = "📋 Моя заявка"
BTN_MY_PROFILE = "👤 Мой профиль"
BTN_SUPPORT = "💬 Связь с организаторами"
BTN_ADMIN = "⚙️ Админ-панель"
BTN_AI = "🤖 AI-помощник"
BTN_STREAM = "📺 Трансляция"
BTN_BECOME_PLAYER = "🎮 Стать игроком"


def viewer_keyboard(is_admin_user: bool = False) -> ReplyKeyboardMarkup:
    """5.9: меню зрителя — смотреть и болеть, без командных разделов."""
    rows = [
        [KeyboardButton(text=BTN_HOME), KeyboardButton(text=BTN_TOURNAMENTS)],
        [KeyboardButton(text=BTN_STREAM), KeyboardButton(text=BTN_SUPPORT)],
        [KeyboardButton(text=BTN_AI), KeyboardButton(text=BTN_BECOME_PLAYER)],
    ]
    if is_admin_user:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def main_keyboard(is_admin_user: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_HOME), KeyboardButton(text=BTN_TOURNAMENTS)],
        [KeyboardButton(text=BTN_MY_TEAM), KeyboardButton(text=BTN_MY_APPLICATION)],
        [KeyboardButton(text=BTN_MY_PROFILE), KeyboardButton(text=BTN_SUPPORT)],
        [KeyboardButton(text=BTN_AI)],
    ]
    if is_admin_user:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="Выберите раздел"
    )


def home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
    )


def back_cancel_kb(back_cb: str = "nav:home", back_text: str = "◀️ Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=back_text, callback_data=back_cb),
                InlineKeyboardButton(text="❌ Отмена", callback_data="action:cancel"),
            ]
        ]
    )


def profile_kb(
    *,
    in_team: bool,
    role_mode: str | None = None,
) -> InlineKeyboardMarkup:
    rows = []
    if in_team:
        if role_mode == "PLAYER_CHOICE":
            rows.append([InlineKeyboardButton(text="🎯 Моя позиция", callback_data="profile:position")])
        rows.append([InlineKeyboardButton(text="🎮 Пул ролей", callback_data="profile:pool")])
    else:
        rows.append([InlineKeyboardButton(text="🎮 Пул ролей", callback_data="profile:pool")])
    rows.append([InlineKeyboardButton(text="✏️ Указать / изменить MMR", callback_data="profile:mmr")])
    rows.append([InlineKeyboardButton(text="🔄 Перепривязать Steam", callback_data="profile:rebind_steam")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def miniapp_kb(url: str) -> InlineKeyboardMarkup:
    """Кнопка запуска Mini App. URL берётся только из конфигурации."""
    from aiogram.types import WebAppInfo
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Открыть TI:IT TOP", web_app=WebAppInfo(url=url))
        ]]
    )
