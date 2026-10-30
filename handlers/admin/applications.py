"""📋 Админ: заявки — список, карточка, одобрение/отклонение с комментарием."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.models import ApplicationStatus
from database.repositories import (
    ApplicationRepository,
    TeamRepository,
    TournamentRepository,
    UserRepository,
)
from handlers.admin.panel import _deny
from handlers.common import AdminReplyStates, get_config
from handlers.helpers import db_session, parse_cb, safe_html
from keyboards.admin import back_to_admin_kb
from keyboards.application import application_card_kb
from services.applications import ApplicationService
from services.teams import TeamService
from utils.formatting import application_status_label, format_roster, status_label
from utils.validation import validate_support_message

logger = logging.getLogger("ADMIN")

router = Router(name="admin_applications")


async def _application_text(session, app_id: int) -> str | None:
    apps = ApplicationRepository(session)
    app = await apps.get_by_id(app_id)
    if app is None:
        return None
    t = await TournamentRepository(session).get_by_id(app.tournament_id)
    team = await TeamRepository(session).get_by_id(app.team_id)
    captain = await UserRepository(session).get_by_id(app.created_by)
    roster = await TeamService(session).roster_with_status(app.team_id)
    body = format_roster([
        (
            safe_html(u.username or u.first_name or str(u.telegram_id)),
            m.role.value,
            status_label(s),
        )
        for u, m, s, _profile in roster
    ])
    lines = [
        f"📋 <b>Заявка #{app.id}</b>",
        "",
        f"<b>Турнир:</b> {safe_html(t.name) if t else '?'}",
        f"<b>Команда:</b> {safe_html(team.name) if team else '?'}",
        f"<b>Капитан:</b> {safe_html((captain.username or captain.first_name) if captain else '?')}",
        "",
        body,
        "",
        f"<b>Статус:</b> {application_status_label(app.status)}",
    ]
    if app.admin_comment:
        lines.append(f"<b>Комментарий:</b> {safe_html(app.admin_comment)}")
    return "\n".join(lines)


@router.callback_query(F.data == "adm:applications")
async def cb_applications(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    async with db_session() as session:
        apps = await ApplicationRepository(session).list_pending(limit=50)
        rows = []
        lines = [f"📋 <b>Заявки на рассмотрении ({len(apps)})</b>", ""]
        if not apps:
            lines.append("Ожидающих заявок нет.")
        for app in apps:
            t = await TournamentRepository(session).get_by_id(app.tournament_id)
            team = await TeamRepository(session).get_by_id(app.team_id)
            lines.append(
                f"• #{app.id} {safe_html(team.name) if team else '?'} → "
                f"{safe_html(t.name) if t else '?'}"
            )
            rows.append([InlineKeyboardButton(
                text=f"#{app.id} {team.name[:36] if team else '?'}",
                callback_data=f"adm:application:{app.id}",
            )])
    rows.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="adm:menu")])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] applications edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm:application:"))
async def cb_application_detail(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    app_id = int(parts[2])
    async with db_session() as session:
        app = await ApplicationRepository(session).get_by_id(app_id)
        text = await _application_text(session, app_id)
    if text is None or app is None:
        await callback.answer("Заявка не найдена (возможно, удалена).", show_alert=True)
        return
    kb = application_card_kb(app.id, app.status.value)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] application detail edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _decide(callback: CallbackQuery, app_id: int, status: ApplicationStatus, comment: str | None) -> str:
    """Изменяет заявку (callback нельзя считать доверенным: проверяем существование и актуальность)."""
    config = get_config()
    async with db_session() as session:
        apps = ApplicationRepository(session)
        app = await apps.get_by_id(app_id)
        if app is None:
            return "Заявка не найдена."
        if app.status != ApplicationStatus.PENDING:
            return f"Заявка уже обработана (статус: {application_status_label(app.status)})."
        admin_tg_id = callback.from_user.id
        await apps.decide(app, status, admin_tg_id, comment)
        t = await TournamentRepository(session).get_by_id(app.tournament_id)
        team = await TeamRepository(session).get_by_id(app.team_id)
        captain = await UserRepository(session).get_by_id(app.created_by)
        from services.activity import add_event
        decision_title = "Заявка одобрена" if status == ApplicationStatus.APPROVED else "Заявка отклонена"
        await add_event(session, kind="application_decision", title=decision_title, body=comment or f"Заявка #{app_id}", target_user_id=captain.id if captain else None)
        await session.commit()

    bot = callback.message.bot
    verdict = "✅ одобрена" if status == ApplicationStatus.APPROVED else "❌ отклонена"
    # Уведомление капитану
    if captain is not None:
        extra = f"\nКомментарий: {comment}" if comment else ""
        try:
            await bot.send_message(
                captain.telegram_id,
                f"📋 Заявка команды <b>{safe_html(team.name) if team else '?'}</b> "
                f"на турнир <b>{safe_html(t.name) if t else '?'}</b>: {verdict}.{extra}",
                parse_mode="HTML",
            )
        except Exception as e:  # noqa: BLE001 — капитан мог заблокировать бота
            logger.warning("[APPLICATION] Cannot notify captain tg=%s: %s", captain.telegram_id, e)
    from handlers.helpers import notify_admins

    await notify_admins(
        bot, config,
        f"📋 Заявка #{app_id} → {verdict} (админ {admin_tg_id}).",
    )
    logger.info("[APPLICATION] id=%s decided %s by admin %s", app_id, status.value, admin_tg_id)
    return f"Заявка #{app_id}: {verdict}."


@router.callback_query(F.data.startswith("app:approve:"))
async def cb_application_approve(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    msg = await _decide(callback, int(parts[2]), ApplicationStatus.APPROVED, None)
    await callback.answer(msg, show_alert=True)


@router.callback_query(F.data.startswith("app:reject:"))
async def cb_application_reject(callback: CallbackQuery, state: FSMContext) -> None:
    """Отклонение с запросом комментария администратора (FSM)."""
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    app_id = int(parts[2])
    async with db_session() as session:
        app = await ApplicationRepository(session).get_by_id(app_id)
        if app is None or app.status != ApplicationStatus.PENDING:
            await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
            return
    await state.update_data(decision_app_id=app_id)
    await state.set_state(AdminReplyStates.waiting_comment)
    await callback.message.answer(
        f"❌ Введите комментарий к отклонению заявки #{app_id} одним сообщением\n"
        "(или «-», чтобы без комментария). Отмена — /cancel."
    )
    await callback.answer()


@router.message(AdminReplyStates.waiting_comment, F.text)
async def process_reject_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    app_id = data.get("decision_app_id")
    raw = (message.text or "").strip()
    await state.clear()
    if app_id is None:
        await message.answer("Сессия устарела. Откройте заявку заново: ⚙️ → 📋 Заявки.")
        return
    comment = None if raw in {"-", "—", "нет"} else raw
    if comment is not None:
        ok, clean, error = validate_support_message(comment)
        if not ok:
            await message.answer(f"⚠️ {error}")
            return
        comment = clean

    class _MsgProxy:
        """Минимальная замена callback для _decide (бот + from_user)."""
        def __init__(self, msg: Message):
            self._msg = msg
            self.from_user = msg.from_user

        @property
        def message(self):
            return self._msg

    from database.models import ApplicationStatus as AS

    msg = await _decide(_MsgProxy(message), int(app_id), AS.REJECTED, comment)  # type: ignore[arg-type]
    await message.answer(f"✅ {msg}")
