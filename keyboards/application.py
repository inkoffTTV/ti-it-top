"""Клавиатуры заявок."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def application_card_kb(app_id: int, status_value: str) -> InlineKeyboardMarkup:
    rows = []
    if status_value == "PENDING":
        rows.append(
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"app:approve:{app_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"app:reject:{app_id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="◀️ Назад к заявкам", callback_data="adm:applications")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def my_application_none_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏆 Смотреть турниры", callback_data="nav:tournaments")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )
