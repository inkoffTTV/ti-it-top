"""🏆 Турниры: список, детали, подача заявки командой."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from database.models import TournamentStatus
from database.repositories import (
    ApplicationRepository,
    TeamRepository,
    TournamentRepository,
    UserRepository,
)
from handlers.helpers import db_session, parse_cb, safe_html
from keyboards.main import home_kb
from keyboards.tournament import tournament_detail_kb, tournaments_list_kb
from services.applications import ApplicationService
from utils.formatting import fmt_dt, tournament_status_label

logger = logging.getLogger("TOURNAMENT")

router = Router(name="tournaments")


async def render_tournaments_list(callback: CallbackQuery) -> None:
    async with db_session() as session:
        tournaments = await TournamentRepository(session).list_public()
    if not tournaments:
        text = "🏆 Сейчас нет доступных турниров.\nСледите за анонсами."
    else:
        lines = ["🏆 <b>Турниры TI:IT TOP</b>", ""]
        for t in tournaments:
            lines.append(f"• {safe_html(t.name)} — {tournament_status_label(t.status)}")
        text = "\n".join(lines)
    kb = tournaments_list_kb(tournaments) if tournaments else home_kb()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001 — edit может не поддерживаться/без изменений
        logger.debug("[TOURNAMENT] edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def show_tournaments(message: Message) -> None:
    async with db_session() as session:
        tournaments = await TournamentRepository(session).list_public()
    if not tournaments:
        await message.answer(
            "🏆 Сейчас нет доступных турниров.\nСледите за анонсами.",
            reply_markup=home_kb(),
        )
        return
    lines = ["🏆 <b>Турниры TI:IT TOP</b>", "", "Выберите турнир для подробностей:"]
    await message.answer("\n".join(lines), reply_markup=tournaments_list_kb(tournaments), parse_mode="HTML")


@router.message(F.text == "🏆 Турниры")
async def kb_tournaments(message: Message) -> None:
    await show_tournaments(message)


def _detail_text(t) -> str:
    return "\n".join([
        f"🏆 <b>{safe_html(t.name)}</b>",
        "",
        f"<b>Статус:</b> {tournament_status_label(t.status)}",
        f"<b>Регистрация:</b> {'🟢 открыта' if t.registration_open else '🔒 закрыта'}",
        f"<b>Дата начала:</b> {fmt_dt(t.start_date)}",
        f"<b>Лимит команд:</b> {t.max_teams}",
        "",
        f"<b>Описание:</b>\n{safe_html(t.description) if t.description else 'Без описания.'}",
    ])


async def _render_detail(callback: CallbackQuery, tournament_id: int) -> None:
    tg = callback.from_user
    async with db_session() as session:
        t = await TournamentRepository(session).get_by_id(tournament_id)
        if t is None:
            await callback.answer("Турнир не найден или был удалён.", show_alert=True)
            return
        user = await UserRepository(session).get_by_telegram_id(tg.id)
        team = await TeamRepository(session).get_by_captain(user.id) if user else None
        already = False
        if team is not None:
            already = await ApplicationRepository(session).exists(t.id, team.id)
        can_register = team is not None and t.status == TournamentStatus.REGISTRATION and t.registration_open
        text = _detail_text(t)
        if team is None:
            text += "\n\nℹ️ Заявку может подать капитан команды («👑 Моя команда»)."
        elif already:
            text += "\n\n✅ Ваша команда уже подавала заявку на этот турнир."
    kb = tournament_detail_kb(t, can_register=can_register, already_applied=already)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[TOURNAMENT] detail edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("trn:view:"))
async def cb_tournament_view(callback: CallbackQuery) -> None:
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    await _render_detail(callback, int(parts[2]))
    await callback.answer()


@router.callback_query(F.data.startswith("trn:join:"))
async def cb_tournament_join(callback: CallbackQuery) -> None:
    """Подача заявки с полным набором проверок (см. ApplicationService)."""
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    tournament_id = int(parts[2])
    tg = callback.from_user
    async with db_session() as session:
        user = await UserRepository(session).get_by_telegram_id(tg.id)
        if user is None:
            await callback.answer("Сначала нажмите /start.", show_alert=True)
            return
        svc = ApplicationService(session)
        try:
            app = await svc.submit(user.id, tournament_id)
        except ValueError as e:
            await callback.answer(f"⚠️ {e}", show_alert=True)
            return
        t = await TournamentRepository(session).get_by_id(app.tournament_id)
        team = await TeamRepository(session).get_by_id(app.team_id)

    # Уведомление администраторам
    from handlers.common import get_config
    from handlers.helpers import notify_admins

    config = get_config()
    admin_text = (
        f"📋 <b>НОВАЯ ЗАЯВКА #{app.id}</b>\n\n"
        f"<b>Турнир:</b> {safe_html(t.name) if t else '?'}\n"
        f"<b>Команда:</b> {safe_html(team.name) if team else '?'}\n"
        f"<b>Капитан:</b> {safe_html(tg.username or str(tg.id))}\n\n"
        "Открыть: ⚙️ Админ-панель → 📋 Заявки"
    )
    await notify_admins(callback.message.bot, config, admin_text)

    text = (
        "✅ <b>Заявка подана!</b>\n\n"
        f"Турнир: {safe_html(t.name) if t else '?'}\n"
        f"Команда: {safe_html(team.name) if team else '?'}\n"
        "Статус: ⏳ На рассмотрении у организаторов.\n\n"
        "Следить за статусом — «📋 Моя заявка»."
    )
    try:
        await callback.message.edit_text(text, reply_markup=home_kb(), parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[APPLICATION] edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=home_kb(), parse_mode="HTML")
    await callback.answer()
    logger.info("[APPLICATION] Team of user %s applied to tournament %s", tg.id, tournament_id)
