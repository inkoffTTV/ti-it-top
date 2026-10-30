"""Роутеры обработчиков TI:IT TOP. Порядок регистрации важен:
специфичные роутеры раньше start (start содержит fallback-хендлеры)."""
from __future__ import annotations

from aiogram import Dispatcher

from handlers import admin as admin_pkg  # noqa: F401  (пакет)
from handlers.admin import applications as admin_applications
from handlers.admin import admins as admin_admins
from handlers.admin import broadcast as admin_broadcast
from handlers.admin import ops as admin_ops
from handlers.admin import panel as admin_panel
from handlers.admin import support as admin_support
from handlers.admin import teams as admin_teams
from handlers.admin import tournaments as admin_tournaments
from handlers.admin import users as admin_users
from handlers.admin import verification as admin_verification
from handlers import ai_chat, applications, matches, profile, start, support, team, tournaments


def register_routers(dp: Dispatcher) -> None:
    # AI-помощник — самым первым: его хендлеры срабатывают только в режиме
    # помощника, а кнопки меню в этом режиме выключают его и передаются дальше.
    dp.include_router(ai_chat.router)
    dp.include_router(matches.router)  # /matches — ближайшие матчи команды
    # Админские — первыми: они проверяют is_admin внутри и не должны
    # перехватываться общими хендлерами.
    dp.include_router(admin_panel.router)
    dp.include_router(admin_ops.router)  # 5.13: «Присудить победу», замены в составах
    dp.include_router(admin_users.router)
    dp.include_router(admin_teams.router)
    dp.include_router(admin_tournaments.router)
    dp.include_router(admin_applications.router)
    dp.include_router(admin_broadcast.router)
    dp.include_router(admin_verification.router)
    dp.include_router(admin_support.router)
    dp.include_router(admin_admins.router)
    # Пользовательские
    dp.include_router(profile.router)
    dp.include_router(team.router)
    dp.include_router(tournaments.router)
    dp.include_router(applications.router)
    dp.include_router(support.router)
    # start — последним (главное меню, fallback)
    dp.include_router(start.router)
