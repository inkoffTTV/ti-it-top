"""Клавиатуры команд."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

POSITION_NAMES = {
    1: "1 — Керри",
    2: "2 — Мид",
    3: "3 — Хард",
    4: "4 — Четверка",
    5: "5 — Пятерка",
}


def no_team_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать команду", callback_data="team:create")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def team_menu_kb(is_captain: bool, role_mode: str = "PLAYER_CHOICE") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📋 Состав команды", callback_data="team:roster")],
        [InlineKeyboardButton(text="ℹ️ Статус команды", callback_data="team:status")],
    ]
    if is_captain:
        if role_mode == "PLAYER_CHOICE":
            rows.append([InlineKeyboardButton(text="🎯 Моя позиция", callback_data="team:my_position")])
        rows += [
            [InlineKeyboardButton(text="➕ Добавить игрока", callback_data="team:add")],
            [InlineKeyboardButton(text="🔎 Поиск игроков", callback_data="team:agents:0")],
            [InlineKeyboardButton(text="✉️ Пригласить по @username", callback_data="team:invite_help")],
            [InlineKeyboardButton(text="🎯 Управление позициями", callback_data="team:roles")],
            [InlineKeyboardButton(text="🖼 Изменить аватарку", callback_data="team:avatar")],
            [InlineKeyboardButton(
                text=f"⚙️ Режим ролей: {'игроки' if role_mode == 'PLAYER_CHOICE' else 'капитан'}",
                callback_data="team:role_mode",
            )],
            [InlineKeyboardButton(text="➖ Удалить игрока", callback_data="team:remove")],
        [InlineKeyboardButton(text="🗑 Расформировать команду", callback_data="team:delete")],
        ]
    else:
        rows.append([InlineKeyboardButton(text="🎯 Моя позиция", callback_data="team:my_position")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def member_action_kb(user_id: int, is_captain_caller: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="◀️ Назад", callback_data="team:menu")]]
    if is_captain_caller:
        rows.insert(
            0,
            [InlineKeyboardButton(text="➖ Удалить из состава", callback_data=f"team:kick:{user_id}")],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def positions_kb(prefix: str, selected: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if selected == pos else "") + label,
                callback_data=f"{prefix}:{pos}",
            )
        ]
        for pos, label in POSITION_NAMES.items()
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="team:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def captain_member_positions_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"team:setpos:{user_id}:{pos}")]
        for pos, label in POSITION_NAMES.items()
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="team:roles")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def role_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Выбор игроком", callback_data="team:mode:PLAYER_CHOICE")],
            [InlineKeyboardButton(text="👑 Выбор капитаном", callback_data="team:mode:CAPTAIN_CHOICE")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="team:menu")],
        ]
    )


def agents_kb(
    agents: list[tuple],
    page: int,
    has_next: bool,
) -> InlineKeyboardMarkup:
    rows = []
    for user, profile in agents:
        name = f"@{user.username}" if user.username else (user.first_name or str(user.telegram_id))
        rows.append([
            InlineKeyboardButton(
                text=f"✉️ {name[:28]} • {profile.exact_mmr} MMR",
                callback_data=f"team:invite:{user.id}",
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"team:agents:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"team:agents:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ В меню команды", callback_data="team:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
