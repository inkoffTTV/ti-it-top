"""Клавиатуры турниров."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.models import Tournament, TournamentStatus


def tournaments_list_kb(tournaments: list[Tournament]) -> InlineKeyboardMarkup:
    rows = []
    for t in tournaments:
        rows.append(
            [InlineKeyboardButton(
                text=f"🏆 {t.name}",
                callback_data=f"trn:view:{t.id}",
            )]
        )
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tournament_detail_kb(t: Tournament, can_register: bool, already_applied: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_register and not already_applied and t.status == TournamentStatus.REGISTRATION:
        rows.append(
            [InlineKeyboardButton(text="📝 Подать заявку на турнир", callback_data=f"trn:join:{t.id}")]
        )
    rows.append([InlineKeyboardButton(text="◀️ К списку турниров", callback_data="nav:tournaments")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
