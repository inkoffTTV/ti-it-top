"""📋 Моя заявка: статус заявки команды пользователя."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from database.repositories import TeamRepository, TournamentRepository, UserRepository
from handlers.helpers import db_session, safe_html
from keyboards.application import my_application_none_kb
from keyboards.main import home_kb
from services.applications import ApplicationService
from utils.formatting import application_status_label, fmt_dt

logger = logging.getLogger("APPLICATION")

router = Router(name="applications")


async def show_my_application(message: Message) -> None:
    tg = message.from_user
    if tg is None:
        return
    async with db_session() as session:
        user = await UserRepository(session).upsert_from_telegram(
            tg.id, tg.username, tg.first_name, tg.last_name
        )
        app = await ApplicationService(session).my_application(user.id)
        if app is None:
            team = await TeamRepository(session).get_for_user(user.id)
            await session.commit()
            if team is None:
                await message.answer(
                    "У вас нет активной заявки.\n"
                    "Вы не состоите в команде — создайте её через «👑 Моя команда».",
                    reply_markup=my_application_none_kb(),
                )
            else:
                await message.answer(
                    "У вас нет активной заявки.\n"
                    "Капитан может подать заявку через «🏆 Турниры».",
                    reply_markup=my_application_none_kb(),
                )
            return
        t = await TournamentRepository(session).get_by_id(app.tournament_id)
        team = await TeamRepository(session).get_by_id(app.team_id)
        await session.commit()

    svc_text = ApplicationService.decision_text(app)
    lines = [
        "📋 <b>Моя заявка</b>",
        "",
        f"<b>Турнир:</b> {safe_html(t.name) if t else 'Не найден'}",
        f"<b>Команда:</b> {safe_html(team.name) if team else 'Не найдена'}",
        f"<b>Статус:</b> {application_status_label(app.status)}",
        f"<b>Подана:</b> {fmt_dt(app.created_at)}",
    ]
    if app.decided_at is not None:
        lines.append(f"<b>Решение администратора:</b> {fmt_dt(app.decided_at)}")
    if app.admin_comment:
        lines.append(f"<b>Комментарий:</b> {safe_html(app.admin_comment)}")
    lines.append("")
    lines.append(svc_text)
    await message.answer("\n".join(lines), reply_markup=home_kb(), parse_mode="HTML")


@router.message(F.text == "📋 Моя заявка")
async def kb_my_application(message: Message) -> None:
    await show_my_application(message)
