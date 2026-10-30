"""Клавиатуры админ-панели."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users"),
                InlineKeyboardButton(text="👑 Команды", callback_data="adm:teams"),
            ],
            [
                InlineKeyboardButton(text="🏆 Турниры", callback_data="adm:tournaments"),
                InlineKeyboardButton(text="📋 Заявки", callback_data="adm:applications"),
            ],
            [
                InlineKeyboardButton(text="🔎 Проверка игроков", callback_data="adm:verification"),
                InlineKeyboardButton(text="💬 Поддержка", callback_data="adm:support"),
            ],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
            [InlineKeyboardButton(text="🌐 Диагностика API", callback_data="adm:diag")],
            [InlineKeyboardButton(text="🛡 Администраторы", callback_data="adm:admins")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def verification_kb(profile_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить MMR", callback_data=f"ver:approve:{profile_id}"),
                InlineKeyboardButton(text="❌ Отклонить MMR", callback_data=f"ver:reject:{profile_id}"),
            ],
            [InlineKeyboardButton(text="🔄 Повторить проверку", callback_data=f"ver:recheck:{profile_id}")],
            [InlineKeyboardButton(text="✏️ Ввести MMR вручную", callback_data=f"ver:mmr:{profile_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:verification")],
        ]
    )


def back_to_admin_kb(section: str = "adm:menu") -> InlineKeyboardMarkup:
    labels = {
        "adm:menu": "⚙️ В админ-панель",
        "adm:users": "👥 К пользователям",
        "adm:teams": "👑 К командам",
        "adm:tournaments": "🏆 К турнирам",
        "adm:applications": "📋 К заявкам",
        "adm:verification": "🔎 К проверкам",
        "adm:support": "💬 к тикетам",
    }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=labels.get(section, "◀️ Назад"), callback_data=section)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def user_detail_kb(user_db_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Проверить заново", callback_data=f"adm:urecheck:{user_db_id}")],
            [InlineKeyboardButton(text="◀️ К пользователям", callback_data="adm:users")],
        ]
    )


def tournament_admin_kb(tournament_id: int, registration_open: bool) -> InlineKeyboardMarkup:
    toggle_label = "🔒 Закрыть регистрацию" if registration_open else "🔓 Открыть регистрацию"
    toggle_cb = f"trn:close:{tournament_id}" if registration_open else f"trn:open:{tournament_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data=toggle_cb)],
            [InlineKeyboardButton(text="▶️ Начать турнир", callback_data=f"trn:start:{tournament_id}")],
            [InlineKeyboardButton(text="🏁 Завершить турнир", callback_data=f"trn:finish:{tournament_id}")],
            [InlineKeyboardButton(text="⛔ Отменить турнир", callback_data=f"trn:cancel:{tournament_id}")],
            [InlineKeyboardButton(text="◀️ К турнирам", callback_data="adm:tournaments")],
        ]
    )


def support_ticket_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✉️ Ответить", callback_data=f"sup:reply:{ticket_id}")],
            [InlineKeyboardButton(text="◀️ К тикетам", callback_data="adm:support")],
        ]
    )


def search_user_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Найти по запросу", callback_data="adm:usearch")],
            [InlineKeyboardButton(text="◀️ В админ-панель", callback_data="adm:menu")],
        ]
    )
