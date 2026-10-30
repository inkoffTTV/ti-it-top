"""⚙️ Админ-панель: главное меню, статистика, создание турниров."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from config import is_admin
from database.engine import session_scope
from database.models import (
    ApplicationStatus,
    Team,
    Tournament,
    TournamentApplication,
    TournamentStatus,
    User,
    VerificationStatus,
)
from handlers.common import TournamentCreateStates, get_config
from handlers.helpers import db_session, safe_html
from keyboards.admin import admin_main_kb, back_to_admin_kb
from keyboards.main import home_kb
from services.tournaments import TournamentService
from utils.formatting import tournament_status_label
from utils.validation import normalize_text, parse_date

logger = logging.getLogger("ADMIN")

router = Router(name="admin_panel")


def _deny(callback_or_message) -> bool:
    """Проверка прав для каждого админ-хендлера. Никогда не доверяем кнопке."""
    user = getattr(callback_or_message, "from_user", None)
    return not is_admin(user.id if user else None, get_config())


async def show_admin_panel(message: Message) -> None:
    await message.answer(
        "⚙️ <b>Админ-панель TI:IT TOP</b>\n\nВыберите раздел:",
        reply_markup=admin_main_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm:menu")
async def cb_admin_menu(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            "⚙️ <b>Админ-панель TI:IT TOP</b>\n\nВыберите раздел:",
            reply_markup=admin_main_kb(),
            parse_mode="HTML",
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] menu edit skipped: %s", e)
        await show_admin_panel(callback.message)
    await callback.answer()


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

async def render_stats(target: CallbackQuery | Message) -> str:
    async with db_session() as session:
        users_n = await session.scalar(select(func.count()).select_from(User)) or 0
        teams_n = await session.scalar(select(func.count()).select_from(Team)) or 0
        tours_n = await session.scalar(select(func.count()).select_from(Tournament)) or 0
        apps_n = await session.scalar(select(func.count()).select_from(TournamentApplication)) or 0
        pending_apps = await session.scalar(
            select(func.count())
            .select_from(TournamentApplication)
            .where(TournamentApplication.status == ApplicationStatus.PENDING)
        ) or 0
        from database.models import PlayerProfile

        pending_players = await session.scalar(
            select(func.count())
            .select_from(PlayerProfile)
            .where(PlayerProfile.verification_status == VerificationStatus.PENDING)
        ) or 0
        verified_players = await session.scalar(
            select(func.count())
            .select_from(PlayerProfile)
            .where(PlayerProfile.verification_status == VerificationStatus.VERIFIED)
        ) or 0
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {users_n}\n"
        f"👑 Команд: {teams_n}\n"
        f"🏆 Турниров: {tours_n}\n"
        f"📋 Заявок всего: {apps_n} (на рассмотрении: {pending_apps})\n"
        f"🔎 Игроков подтверждено: {verified_players}\n"
        f"⏳ Ожидают проверки: {pending_players}"
    )
    kb = back_to_admin_kb("adm:menu")
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:  # noqa: BLE001
            logger.debug("[ADMIN] stats edit skipped: %s", e)
            await target.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    return text


@router.callback_query(F.data == "adm:stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await render_stats(callback)
    await callback.answer()


@router.callback_query(F.data == "adm:diag")
async def cb_admin_diag(callback: CallbackQuery) -> None:
    """🌐 Диагностика API — почему статистика может не тянуться."""
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    from services.diagnostics import run_diagnostics
    from services.factory import get_services

    s = get_services()
    await callback.answer("Проверяю доступность источников…")
    try:
        text = await run_diagnostics(s)
    except Exception as e:  # noqa: BLE001
        logger.error("[ADMIN] diagnostics failed: %s", e)
        from services.diagnostics import esc

        text = f"⚠️ Диагностика завершилась ошибкой: {esc(e)}"
    from keyboards.admin import back_to_admin_kb

    try:
        await callback.message.answer(text, reply_markup=back_to_admin_kb(), parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.error("[ADMIN] diagnostics send failed: %s", e)
        # Fallback: plain text без разметки — Telegram не сломается на тегах.
        try:
            import re

            plain = re.sub(r"<[^>]+>", "", text)
            await callback.message.answer(plain, reply_markup=back_to_admin_kb())
        except Exception as e2:  # noqa: BLE001
            logger.error("[ADMIN] diagnostics fallback send failed: %s", e2)


# ---------------------------------------------------------------------------
# Создание турнира (FSM)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:tcreate")
async def cb_tournament_create(callback: CallbackQuery, state: FSMContext) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await state.set_state(TournamentCreateStates.waiting_name)
    await callback.message.edit_text(
        "🏆 <b>Новый турнир — шаг 1/4</b>\n\nВведите название турнира.\nОтмена — /cancel.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(TournamentCreateStates.waiting_name, F.text)
async def tc_name(message: Message, state: FSMContext) -> None:
    name = normalize_text(message.text or "")
    if not name or len(name) > 128:
        await message.answer("⚠️ Название пустое или слишком длинное (макс. 128). Введите ещё раз или /cancel.")
        return
    await state.update_data(name=name)
    await state.set_state(TournamentCreateStates.waiting_description)
    await message.answer("📝 Шаг 2/4: введите описание турнира (или «-», чтобы пропустить). Отмена — /cancel.")


@router.message(TournamentCreateStates.waiting_description, F.text)
async def tc_description(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    description = None if raw in {"-", "—", "нет"} else raw[:2000]
    await state.update_data(description=description)
    await state.set_state(TournamentCreateStates.waiting_max_teams)
    await message.answer("🔢 Шаг 3/4: максимальное количество команд (число от 2 до 1024). Отмена — /cancel.")


@router.message(TournamentCreateStates.waiting_max_teams, F.text)
async def tc_max_teams(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (2 <= int(raw) <= 1024):
        await message.answer("⚠️ Нужно целое число от 2 до 1024. Попробуйте ещё раз или /cancel.")
        return
    await state.update_data(max_teams=int(raw))
    await state.set_state(TournamentCreateStates.waiting_start_date)
    await message.answer(
        "📅 Шаг 4/4: дата начала в формате ДД.ММ.ГГГГ [ЧЧ:ММ] по Москве (или «-»).\nОтмена — /cancel."
    )


@router.message(TournamentCreateStates.waiting_start_date, F.text)
async def tc_start_date(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    start_date = None
    if raw not in {"-", "—", "нет"}:
        start_date = parse_date(raw)
        if start_date is None:
            await message.answer("⚠️ Не понял дату. Формат: 31.12.2026 или 2026-12-31 18:00. Или «-».")
            return
    data = await state.get_data()
    await state.clear()
    try:
        async with db_session() as session:
            t = await TournamentService(session).create(
                name=data.get("name", ""),
                description=data.get("description"),
                max_teams=int(data.get("max_teams", 16)),
                start_date=start_date,
                open_registration=False,
            )
    except ValueError as e:
        await message.answer(f"⚠️ {safe_html(str(e))}")
        return
    await message.answer(
        f"✅ Турнир <b>{safe_html(t.name)}</b> создан (id={t.id}, статус 📝 черновик).\n"
        "Откройте регистрацию: ⚙️ → 🏆 Турниры → выбор турнира.",
        reply_markup=back_to_admin_kb("adm:tournaments"),
        parse_mode="HTML",
    )
    logger.info("[TOURNAMENT] Admin created tournament id=%s name=%s", t.id, t.name)
