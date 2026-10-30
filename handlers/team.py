"""👑 Моя команда: состав, инвайты, позиции, аватарка и поиск свободных игроков."""
from __future__ import annotations

import io
import logging
from pathlib import Path
from statistics import mean

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    Message,
)
from aiogram.filters import Command, CommandObject
from sqlalchemy import select

from database.models import TeamInviteStatus
from database.repositories import (
    ProfileRepository,
    TeamInviteRepository,
    TeamRepository,
    UserRepository,
)
from handlers.common import TeamStates
from handlers.helpers import db_session, safe_html
from keyboards.main import home_kb
from keyboards.team import (
    agents_kb,
    captain_member_positions_kb,
    no_team_kb,
    role_mode_kb,
    team_menu_kb,
)
from services.teams import MAX_ROSTER_SIZE, POSITIONS, TeamService
from utils.formatting import format_roster, status_label
from utils.ranks import rank_label_from_mmr

logger = logging.getLogger("TEAM")
LOCAL_AVATAR_DIR = Path(__file__).resolve().parent.parent / "miniapp" / "data" / "team_avatars"

router = Router(name="team")


async def _edit_or_answer(message: Message, text: str, **kwargs):
    """Безопасно показывает новый экран команды.

    Карточка команды отправляется как фото с caption. Telegram в разных
    сценариях может не дать отредактировать такой caption, поэтому для
    навигации команды медиасообщения НЕ редактируем: отправляем новый
    текстовый экран. Это исключает `there is no text in the message to edit`
    и оставляет рабочую inline-клавиатуру на новом экране.
    """
    if message.text is not None:
        try:
            return await message.edit_text(text, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[TEAM] edit_text failed, sending new message: %s", exc)
            return await message.answer(text, **kwargs)

    # Фото/документ/анимация: не пытаемся менять caption.
    # Сначала убираем старую inline-клавиатуру, затем создаём новый экран.
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    return await message.answer(text, **kwargs)

PLACEHOLDER = (
    __import__("pathlib", fromlist=["Path"]).Path(__file__).resolve().parent.parent
    / "static"
    / "team_placeholder.png"
)


async def _get_caller_team(callback: CallbackQuery):
    tg = callback.from_user
    async with db_session() as session:
        users = UserRepository(session)
        user = await users.get_by_telegram_id(tg.id)
        if user is None:
            user = await users.upsert_from_telegram(
                tg.id, tg.username, tg.first_name, tg.last_name
            )
            await session.commit()
        team = await TeamRepository(session).get_for_user(user.id)
    return user, team


def _preferred_positions(profile) -> str:
    raw = (profile.preferred_positions if profile else None) or ""
    vals = [int(x) for x in raw.split(",") if x.isdigit() and int(x) in POSITIONS]
    return ", ".join(str(x) for x in vals) if vals else "не выбраны"


def _team_caption(team, roster) -> str:
    lines = [f"👑 <b>{safe_html(team.name)}</b>", ""]
    exact = [p.exact_mmr for _, _, _, p in roster if p and p.exact_mmr is not None]
    if len(exact) == len(roster) and exact:
        avg_mmr = round(mean(exact))
        lines.append(f"📊 <b>AVG MMR:</b> {avg_mmr}")
    else:
        lines.append(f"📊 <b>AVG MMR:</b> ожидается ({len(exact)}/{len(roster)})")

    lines.append(
        f"🎯 <b>Режим ролей:</b> "
        f"{'игроки выбирают сами' if team.role_mode == 'PLAYER_CHOICE' else 'назначает капитан'}"
    )
    lines.append("")
    lines.append(f"📋 <b>Состав ({len(roster)}/{MAX_ROSTER_SIZE})</b>")

    for user, member, status, profile in roster:
        name = f"@{safe_html(user.username)}" if user.username else safe_html(user.first_name or str(user.telegram_id))
        pos = " • тренер" if getattr(member.role, "value", member.role) == "COACH" else (f" • pos {member.position}" if member.position else "")
        mmr = f" • {profile.exact_mmr} MMR" if profile and profile.exact_mmr is not None else ""
        lines.append(
            f"{'👑' if member.role.value == 'CAPTAIN' else '🎮'} "
            f"{name}{pos}{mmr} — {status_label(status)}"
        )
    return "\n".join(lines)


async def show_my_team(message: Message) -> None:
    tg = message.from_user
    if tg is None:
        return

    async with db_session() as session:
        user = await UserRepository(session).upsert_from_telegram(
            tg.id, tg.username, tg.first_name, tg.last_name
        )
        teams = TeamRepository(session)
        team = await teams.get_for_user(user.id)
        if team is None:
            await message.answer(
                "У вас пока нет команды.\n"
                "Капитан может создать команду кнопкой ниже.",
                reply_markup=no_team_kb(),
            )
            await session.commit()
            return
        roster = await TeamService(session).roster_with_status(team.id)
        is_captain = team.captain_user_id == user.id
        caption = _team_caption(team, roster)

    kb = team_menu_kb(is_captain, team.role_mode)

    try:
        if team.avatar_file_id and team.avatar_file_type == "photo":
            await message.answer_photo(
                photo=team.avatar_file_id,
                caption=caption[:1024],
                reply_markup=kb,
                parse_mode="HTML",
            )
        elif team.avatar_file_id and team.avatar_file_type == "document":
            await message.answer_document(
                document=team.avatar_file_id,
                caption=caption[:1024],
                reply_markup=kb,
                parse_mode="HTML",
            )
        elif (LOCAL_AVATAR_DIR / f"team_{team.id}.jpg").is_file():
            await message.answer_photo(
                photo=FSInputFile(LOCAL_AVATAR_DIR / f"team_{team.id}.jpg"),
                caption=caption[:1024],
                reply_markup=kb,
                parse_mode="HTML",
            )
        elif PLACEHOLDER.is_file():
            await message.answer_photo(
                photo=FSInputFile(PLACEHOLDER),
                caption=caption[:1024],
                reply_markup=kb,
                parse_mode="HTML",
            )
        else:
            await message.answer(caption, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.warning("[TEAM] Avatar send failed, fallback to text: %s", e)
        await message.answer(caption, reply_markup=kb, parse_mode="HTML")


@router.message(F.text == "👑 Моя команда")
async def kb_team(message: Message) -> None:
    await show_my_team(message)


@router.callback_query(F.data == "team:create")
async def cb_team_create(callback: CallbackQuery, state: FSMContext) -> None:
    user, team = await _get_caller_team(callback)
    if team is not None:
        await _edit_or_answer(callback.message, 
            f"Команда <b>{safe_html(team.name)}</b> уже существует. "
            "Управление — через «👑 Моя команда».",
            parse_mode="HTML",
        )
        await callback.answer()
        return
    await state.update_data(user_id=user.id)
    await state.set_state(TeamStates.waiting_name)
    await _edit_or_answer(callback.message, 
        "✏️ Введите название команды (до 64 символов).\n\n"
        "Отмена — /cancel или «🏠 Главное меню».",
    )
    await callback.answer()


@router.message(TeamStates.waiting_name, F.text)
async def process_team_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = data.get("user_id")
    raw = message.text or ""
    if user_id is None:
        await state.clear()
        await message.answer("Сессия устарела. Откройте «👑 Моя команда» заново.")
        return
    try:
        async with db_session() as session:
            team = await TeamService(session).create_team(user_id, raw)
    except ValueError as e:
        await message.answer(
            f"⚠️ {safe_html(str(e))}\n\nПопробуйте ещё раз.",
            parse_mode="HTML",
        )
        return
    await state.clear()
    await message.answer(
        f"✅ Команда <b>{safe_html(team.name)}</b> создана! Вы — капитан.\n"
        "Теперь добавьте игроков в состав.",
        reply_markup=team_menu_kb(True),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "team:menu")
async def cb_team_menu(callback: CallbackQuery) -> None:
    await show_my_team(callback.message)
    await callback.answer()


@router.callback_query(F.data.in_({"team:roster", "team:status"}))
async def cb_team_roster_or_status(callback: CallbackQuery) -> None:
    user, team = await _get_caller_team(callback)
    if team is None:
        await callback.answer("Команда не найдена.", show_alert=True)
        return

    async with db_session() as session:
        roster = await TeamService(session).roster_with_status(team.id)

    if callback.data == "team:roster":
        text = _team_caption(team, roster)
    else:
        verified = sum(1 for _, _, s, _ in roster if s.value == "VERIFIED")
        pending = sum(1 for _, _, s, _ in roster if s.value == "PENDING")
        exact = [p.exact_mmr for _, _, _, p in roster if p and p.exact_mmr is not None]
        avg = str(round(mean(exact))) if len(exact) == len(roster) and exact else "ожидается"
        text = (
            f"ℹ️ <b>Статус команды {safe_html(team.name)}</b>\n\n"
            f"Игроков: {len(roster)}/{MAX_ROSTER_SIZE}\n"
            f"✅ Подтверждено: {verified}\n"
            f"⏳ Ожидают проверки: {pending}\n"
            f"📊 AVG MMR: {avg}\n"
            f"🎯 Режим ролей: "
            f"{'выбор игроком' if team.role_mode == 'PLAYER_CHOICE' else 'выбор капитаном'}"
        )

    await callback.message.answer(
        text,
        reply_markup=team_menu_kb(team.captain_user_id == user.id, team.role_mode),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.in_({"team:add", "team:remove"}))
async def cb_team_add_remove(callback: CallbackQuery, state: FSMContext) -> None:
    user, team = await _get_caller_team(callback)
    if team is None:
        await callback.answer("Сначала создайте команду.", show_alert=True)
        return
    if team.captain_user_id != user.id:
        await callback.answer("Только капитан может управлять составом.", show_alert=True)
        return
    await state.update_data(user_id=user.id, team_id=team.id)
    if callback.data == "team:add":
        await state.set_state(TeamStates.waiting_add_player)
        prompt = (
            "➕ Введите @username или Telegram ID игрока.\n\n"
            "Игрок должен ранее нажать /start в этом боте.\n"
            "Для приглашения также можно использовать «🔎 Поиск игроков»."
        )
    else:
        await state.set_state(TeamStates.waiting_remove_player)
        prompt = "➖ Введите @username или Telegram ID игрока для удаления из состава."
    await _edit_or_answer(callback.message, prompt)
    await callback.answer()


async def _member_input(message: Message, state: FSMContext, add: bool) -> None:
    data = await state.get_data()
    user_id = data.get("user_id")
    team_id = data.get("team_id")
    query = (message.text or "").strip()
    if user_id is None or team_id is None:
        await state.clear()
        await message.answer("Сессия устарела. Откройте «👑 Моя команда» заново.")
        return
    try:
        async with db_session() as session:
            svc = TeamService(session)
            if add:
                member = await svc.add_player(team_id, user_id, query)
                result_msg = f"✅ Игрок {safe_html(query)} добавлен в состав."
            else:
                await svc.remove_player(team_id, user_id, query)
                result_msg = f"✅ Игрок {safe_html(query)} удалён из состава."
    except (ValueError, PermissionError) as e:
        result_msg = f"⚠️ {safe_html(str(e))}"
    await state.clear()
    await message.answer(result_msg, reply_markup=home_kb(), parse_mode="HTML")


@router.message(TeamStates.waiting_add_player, F.text)
async def process_add_player(message: Message, state: FSMContext) -> None:
    await _member_input(message, state, add=True)


@router.message(TeamStates.waiting_remove_player, F.text)
async def process_remove_player(message: Message, state: FSMContext) -> None:
    await _member_input(message, state, add=False)


# ---------------------------------------------------------------------------
# Аватар команды
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "team:avatar")
async def cb_team_avatar(callback: CallbackQuery, state: FSMContext) -> None:
    user, team = await _get_caller_team(callback)
    if team is None or team.captain_user_id != user.id:
        await callback.answer("Только капитан может менять аватарку.", show_alert=True)
        return
    await state.update_data(team_id=team.id, user_id=user.id)
    await state.set_state(TeamStates.waiting_avatar)
    await callback.message.answer(
        "🖼 Отправьте аватар команды фотографией или изображением как документ.\n"
        "Отмена — /cancel."
    )
    await callback.answer()


async def _store_document_as_photo(message: Message, file_id: str, filename: str) -> str:
    """Скачивает image-document, приводит его к JPEG и получает photo file_id."""
    from PIL import Image

    file = await message.bot.get_file(file_id)
    buffer = io.BytesIO()
    await message.bot.download(file, destination=buffer)
    buffer.seek(0)

    image = Image.open(buffer)
    image = image.convert("RGB")
    converted = io.BytesIO()
    image.save(converted, format="JPEG", quality=92, optimize=True)
    converted.seek(0)

    sent = await message.bot.send_photo(
        message.chat.id,
        BufferedInputFile(converted.getvalue(), filename="team_avatar.jpg"),
    )
    new_file_id = sent.photo[-1].file_id
    try:
        await message.bot.delete_message(message.chat.id, sent.message_id)
    except Exception:
        pass
    return new_file_id


@router.message(TeamStates.waiting_avatar, F.photo)
async def team_avatar_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    team_id = data.get("team_id")
    user_id = data.get("user_id")
    if not team_id or not user_id:
        await state.clear()
        return
    file_id = message.photo[-1].file_id
    async with db_session() as session:
        await TeamService(session).set_avatar(team_id, user_id, file_id, "photo")
    await state.clear()
    await message.answer("✅ Аватар команды обновлён.", reply_markup=home_kb())


@router.message(TeamStates.waiting_avatar, F.document)
async def team_avatar_document(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    team_id = data.get("team_id")
    user_id = data.get("user_id")
    if not team_id or not user_id:
        await state.clear()
        return
    doc = message.document
    if not (doc.mime_type or "").startswith("image/"):
        await message.answer("❌ Документ должен быть изображением.")
        return
    try:
        file_id = await _store_document_as_photo(
            message,
            doc.file_id,
            doc.file_name or "avatar.jpg",
        )
        file_type = "photo"
    except Exception as e:
        logger.warning("[TEAM] Document avatar conversion failed: %s", e)
        file_id = doc.file_id
        file_type = "document"
    async with db_session() as session:
        await TeamService(session).set_avatar(team_id, user_id, file_id, file_type)
    await state.clear()
    await message.answer("✅ Аватар команды обновлён.", reply_markup=home_kb())


# ---------------------------------------------------------------------------
# Режимы и позиции
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "team:role_mode")
async def cb_role_mode(callback: CallbackQuery) -> None:
    user, team = await _get_caller_team(callback)
    if team is None or team.captain_user_id != user.id:
        await callback.answer("Только капитан.", show_alert=True)
        return
    await _edit_or_answer(callback.message, 
        "⚙️ <b>Режим управления позициями</b>\n\n"
        "👤 Выбор игроком — каждый участник меняет свою позицию.\n"
        "👑 Выбор капитаном — позиции назначает только капитан.",
        reply_markup=role_mode_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("team:mode:"))
async def cb_set_role_mode(callback: CallbackQuery) -> None:
    user, team = await _get_caller_team(callback)
    if team is None or team.captain_user_id != user.id:
        await callback.answer("Только капитан.", show_alert=True)
        return
    mode = callback.data.rsplit(":", 1)[-1]
    try:
        async with db_session() as session:
            await TeamService(session).set_role_mode(team.id, user.id, mode)
        await callback.answer("Режим изменён.")
        await show_my_team(callback.message)
    except (ValueError, PermissionError) as e:
        await callback.answer(str(e), show_alert=True)


@router.callback_query(F.data == "team:roles")
async def cb_team_roles(callback: CallbackQuery) -> None:
    user, team = await _get_caller_team(callback)
    if team is None or team.captain_user_id != user.id:
        await callback.answer("Только капитан.", show_alert=True)
        return
    if team.role_mode != "CAPTAIN_CHOICE":
        await callback.answer(
            "Сейчас позиции выбирают сами игроки. Сначала включите «👑 Выбор капитаном» в режиме ролей.",
            show_alert=True,
        )
        return
    async with db_session() as session:
        roster = await TeamService(session).roster_with_status(team.id)
    rows = []
    for member_user, member, _status, _profile in roster:
        name = member_user.username or member_user.first_name or str(member_user.telegram_id)
        rows.append([__import__("aiogram.types", fromlist=["InlineKeyboardButton"]).InlineKeyboardButton(
            text=f"{name[:30]} • pos {member.position or '—'}",
            callback_data=f"team:role:{member.user_id}",
        )])
    rows.append([__import__("aiogram.types", fromlist=["InlineKeyboardButton"]).InlineKeyboardButton(
        text="◀️ Назад", callback_data="team:menu"
    )])
    await _edit_or_answer(callback.message, 
        "🎯 <b>Позиции состава</b>\n\nВыберите игрока.",
        reply_markup=__import__("aiogram.types", fromlist=["InlineKeyboardMarkup"]).InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("team:role:"))
async def cb_choose_member_position(callback: CallbackQuery) -> None:
    user, team = await _get_caller_team(callback)
    if team is None or team.captain_user_id != user.id:
        await callback.answer("Только капитан.", show_alert=True)
        return
    target_id = int(callback.data.rsplit(":", 1)[-1])
    await _edit_or_answer(callback.message, 
        "🎯 Выберите позицию для игрока:",
        reply_markup=captain_member_positions_kb(target_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("team:setpos:"))
async def cb_set_member_position(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    _, _, target_id, position = parts
    user, team = await _get_caller_team(callback)
    if team is None or team.captain_user_id != user.id:
        await callback.answer("Только капитан.", show_alert=True)
        return
    try:
        async with db_session() as session:
            await TeamService(session).set_position(
                team.id, int(target_id), int(position), user.id
            )
        await callback.answer("Позиция назначена.")
        await show_my_team(callback.message)
    except (ValueError, PermissionError) as e:
        await callback.answer(str(e), show_alert=True)


@router.callback_query(F.data == "team:my_position")
async def cb_my_position(callback: CallbackQuery) -> None:
    user, team = await _get_caller_team(callback)
    if team is None:
        await callback.answer("Команда не найдена.", show_alert=True)
        return
    if team.role_mode != "PLAYER_CHOICE" and team.captain_user_id != user.id:
        await callback.answer("Позиции назначает капитан.", show_alert=True)
        return
    async with db_session() as session:
        member = await TeamRepository(session).get_member(team.id, user.id)
    await _edit_or_answer(callback.message, 
        "🎯 <b>Выберите свою позицию</b>",
        reply_markup=__import__("keyboards.team", fromlist=["positions_kb"]).positions_kb(
            "team:mypos",
            member.position if member else None,
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("team:mypos:"))
async def cb_my_position_set(callback: CallbackQuery) -> None:
    position = int(callback.data.rsplit(":", 1)[-1])
    user, team = await _get_caller_team(callback)
    if team is None:
        await callback.answer("Команда не найдена.", show_alert=True)
        return
    try:
        async with db_session() as session:
            await TeamService(session).set_position(
                team.id, user.id, position, user.id
            )
        await callback.answer(f"Позиция {position} назначена.")
        await show_my_team(callback.message)
    except (ValueError, PermissionError) as e:
        await callback.answer(str(e), show_alert=True)


# ---------------------------------------------------------------------------
# Поиск свободных агентов и инвайты
# ---------------------------------------------------------------------------

async def _render_agents(callback: CallbackQuery, page: int) -> None:
    user, team = await _get_caller_team(callback)
    if team is None or team.captain_user_id != user.id:
        await callback.answer("Только капитан.", show_alert=True)
        return

    offset = max(0, page) * 5
    async with db_session() as session:
        repo = TeamRepository(session)
        agents = await repo.free_agents(limit=5, offset=offset)
        count = await repo.free_agents_count()

    if not agents:
        text = (
            "🔎 <b>Свободные игроки</b>\n\n"
            "Сейчас нет игроков, которые одновременно свободны, указали точный MMR "
            "и уже прошли ручную проверку организатора.\n\n"
            "После подтверждения MMR игрок автоматически появится здесь."
        )
    else:
        lines = [f"🔎 <b>Свободные игроки</b> • страница {page + 1}", ""]
        for idx, (agent, profile) in enumerate(agents, 1):
            rank = (
                rank_label_from_mmr(profile.approved_mmr)
                if profile and profile.approved_mmr is not None
                else "ПТС не утверждён"
            )
            pool = _preferred_positions(profile)
            name = f"@{safe_html(agent.username)}" if agent.username else safe_html(agent.first_name or str(agent.telegram_id))
            lines.append(
                f"<b>{idx}. {name}</b>\n"
                f"🏅 {safe_html(rank)}\n"
                f"📊 MMR: <b>{profile.exact_mmr}</b>\n"
                f"🎮 Пул ролей: {safe_html(pool)}"
            )
            lines.append("")
        text = "\n".join(lines)

    has_next = offset + len(agents) < count
    await _edit_or_answer(callback.message, 
        text,
        reply_markup=agents_kb(agents, page, has_next),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("team:agents:"))
async def cb_agents(callback: CallbackQuery) -> None:
    try:
        page = max(0, int(callback.data.rsplit(":", 1)[-1]))
    except (TypeError, ValueError):
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    try:
        await _render_agents(callback, page)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[TEAM] Free-agent search failed: %s", exc)
        await callback.answer("Не удалось открыть поиск игроков. Ошибка записана в лог.", show_alert=True)


async def _send_invite(bot, team, invite, invitee):
    text = (
        f"✉️ <b>Приглашение в команду</b>\n\n"
        f"Команда: <b>{safe_html(team.name)}</b>\n"
        "Вас приглашают в состав. Вы принимаете приглашение?\n\n"
        "Срок действия: 24 часа."
    )
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"team:invite_accept:{invite.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"team:invite_decline:{invite.id}"),
            ]
        ]
    )
    await bot.send_message(invitee.telegram_id, text, reply_markup=kb, parse_mode="HTML")


async def _create_and_send_invite(message_or_callback, target_user_id: int):
    if isinstance(message_or_callback, CallbackQuery):
        user, team = await _get_caller_team(message_or_callback)
        bot = message_or_callback.message.bot
    else:
        tg = message_or_callback.from_user
        async with db_session() as session:
            user = await UserRepository(session).get_by_telegram_id(tg.id)
            team = await TeamRepository(session).get_for_user(user.id) if user else None
        bot = message_or_callback.bot

    if user is None or team is None:
        raise ValueError("Команда не найдена.")
    async with db_session() as session:
        svc = TeamService(session)
        invite = await svc.create_invite(team.id, user.id, target_user_id)
        invitee = await UserRepository(session).get_by_id(target_user_id)
        if invitee is None:
            raise ValueError("Игрок не найден.")
        from services.activity import add_event
        await add_event(session, kind="team_invite", title="Новое приглашение в команду", body=f"Команда: {team.name}", actor_user_id=user.id, target_user_id=invitee.id)
        await session.commit()
    await _send_invite(bot, team, invite, invitee)
    return invite


@router.callback_query(F.data.startswith("team:invite:"))
async def cb_agent_invite(callback: CallbackQuery) -> None:
    target_id = int(callback.data.rsplit(":", 1)[-1])
    try:
        invite = await _create_and_send_invite(callback, target_id)
        await callback.answer("✉️ Приглашение отправлено.")
        await callback.message.answer(
            f"✉️ Приглашение #{invite.id} отправлено.",
            reply_markup=home_kb(),
        )
    except (ValueError, PermissionError) as e:
        await callback.answer(str(e), show_alert=True)


@router.callback_query(F.data == "team:invite_help")
async def cb_invite_help(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "✉️ Для приглашения используйте:\n\n"
        "<code>/invite @username</code>\n\n"
        "Игрок должен уже иметь регистрацию и подтверждённый точный MMR.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Command("invite"))
async def cmd_invite(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return
    query = (getattr(command, "args", None) or "").strip()
    if not query:
        await message.answer("Использование: <code>/invite @username</code>", parse_mode="HTML")
        return
    async with db_session() as session:
        user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
        team = await TeamRepository(session).get_for_user(user.id) if user else None
        target = await TeamService(session).resolve_player(query) if user and team else None
    if user is None or team is None:
        await message.answer("У вас нет команды.")
        return
    if target is None:
        await message.answer("Игрок не найден. Он должен нажать /start.")
        return
    try:
        await _create_and_send_invite(message, target.id)
        await message.answer("✉️ Приглашение отправлено.")
    except (ValueError, PermissionError) as e:
        await message.answer(f"⚠️ {safe_html(str(e))}", parse_mode="HTML")


@router.callback_query(F.data.startswith("team:invite_accept:"))
async def cb_invite_accept(callback: CallbackQuery) -> None:
    invite_id = int(callback.data.rsplit(":", 1)[-1])
    user_tg = callback.from_user
    async with db_session() as session:
        user = await UserRepository(session).get_by_telegram_id(user_tg.id)
        if user is None:
            await callback.answer("Профиль не найден.", show_alert=True)
            return
        try:
            team = await TeamService(session).accept_invite(invite_id, user.id)
            captain = await UserRepository(session).get_by_id(team.captain_user_id)
            from services.activity import add_event
            await add_event(session, kind="invite_accepted", title="Приглашение принято", body=f"Команда: {team.name}", actor_user_id=user.id)
            await session.commit()
        except (ValueError, PermissionError) as e:
            await callback.answer(str(e)[:195], show_alert=True)
            return
    await _edit_or_answer(callback.message,
        f"✅ Вы приняли приглашение в команду <b>{safe_html(team.name)}</b>.",
        parse_mode="HTML",
    )
    if captain:
        try:
            await callback.message.bot.send_message(
                captain.telegram_id,
                f"✅ Игрок @{safe_html(user_tg.username) if user_tg.username else user_tg.id} "
                f"принял приглашение в <b>{safe_html(team.name)}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    await callback.answer("Приглашение принято.")


@router.callback_query(F.data.startswith("team:invite_decline:"))
async def cb_invite_decline(callback: CallbackQuery) -> None:
    invite_id = int(callback.data.rsplit(":", 1)[-1])
    user_tg = callback.from_user
    async with db_session() as session:
        user = await UserRepository(session).get_by_telegram_id(user_tg.id)
        if user is None:
            await callback.answer("Профиль не найден.", show_alert=True)
            return
        try:
            invite = await TeamInviteRepository(session).get_pending(invite_id)
            if invite is None or invite.invitee_user_id != user.id:
                raise ValueError("Приглашение недействительно.")
            team = await TeamRepository(session).get_by_id(invite.team_id)
            await TeamInviteRepository(session).decide(invite, TeamInviteStatus.DECLINED)
            from services.activity import add_event
            await add_event(session, kind="invite_declined", title="Приглашение отклонено", body=f"Команда: {team.name if team else '—'}", actor_user_id=user.id)
            await session.commit()
            captain = await UserRepository(session).get_by_id(team.captain_user_id) if team else None
        except (ValueError, PermissionError) as e:
            await callback.answer(str(e), show_alert=True)
            return
    await _edit_or_answer(callback.message, "❌ Вы отклонили приглашение.", parse_mode="HTML")
    if captain:
        try:
            await callback.message.bot.send_message(
                captain.telegram_id,
                f"❌ Игрок @{safe_html(user_tg.username) if user_tg.username else user_tg.id} "
                f"отклонил приглашение в <b>{safe_html(team.name)}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    await callback.answer("Приглашение отклонено.")


# ---------------------------------------------------------------------------
# Входящие приглашения
# ---------------------------------------------------------------------------

def _invite_list_kb(invites) -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    for invite, team in invites:
        rows.append([
            InlineKeyboardButton(text=f"✅ Принять — {team.name[:24]}", callback_data=f"team:invite_accept:{invite.id}"),
        ])
        rows.append([
            InlineKeyboardButton(text=f"❌ Отклонить — {team.name[:24]}", callback_data=f"team:invite_decline:{invite.id}"),
        ])
    rows.append([InlineKeyboardButton(text="◀️ Моя команда", callback_data="team:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "team:invites")
async def cb_team_invites(callback: CallbackQuery) -> None:
    tg = callback.from_user
    async with db_session() as session:
        user = await UserRepository(session).get_by_telegram_id(tg.id)
        if user is None:
            await callback.answer("Профиль не найден.", show_alert=True)
            return
        invite_repo = TeamInviteRepository(session)
        invites = await invite_repo.list_pending_for_user(user.id, limit=10)
        rows = []
        for invite in invites:
            team = await TeamRepository(session).get_by_id(invite.team_id)
            if team is not None:
                rows.append((invite, team))
    if not rows:
        await _edit_or_answer(callback.message, 
            "📨 <b>Приглашения в команды</b>\n\nУ вас нет активных приглашений.",
            reply_markup=__import__("keyboards.team", fromlist=["team_menu_kb"]).team_menu_kb(False),
            parse_mode="HTML",
        )
    else:
        lines = ["📨 <b>Приглашения в команды</b>", ""]
        for invite, team in rows:
            lines.append(f"👑 <b>{safe_html(team.name)}</b> — действует 24 часа")
        await _edit_or_answer(callback.message, 
            "\n".join(lines), reply_markup=_invite_list_kb(rows), parse_mode="HTML"
        )
    await callback.answer()


# ---------------------------------------------------------------------------
# Удаление игрока
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("team:kick:"))
async def cb_team_kick(callback: CallbackQuery) -> None:
    target_user_id = int(callback.data.rsplit(":", 1)[-1])
    user, team = await _get_caller_team(callback)
    if team is None or team.captain_user_id != user.id:
        await callback.answer("Только капитан может удалять игроков.", show_alert=True)
        return
    try:
        async with db_session() as session:
            if target_user_id == user.id:
                raise ValueError("Капитан не может исключить сам себя.")
            if await TeamRepository(session).get_member(team.id, target_user_id) is not None:
                from services.teams import roster_guard

                await roster_guard(session, team.id, "REMOVE", target_user_id, user.id)  # 5.13: заморозка состава
            removed = await TeamRepository(session).remove_member(team.id, target_user_id)
            await session.commit()
        msg = "✅ Игрок удалён из состава." if removed else "Игрока нет в составе."
    except ValueError as e:
        msg = f"⚠️ {e}"
    await callback.answer(msg[:195], show_alert=True)


@router.callback_query(F.data == "team:delete")
async def cb_team_delete(callback: CallbackQuery) -> None:
    user, team = await _get_caller_team(callback)
    if team is None:
        await callback.answer("Команда не найдена.", show_alert=True)
        return
    if team.captain_user_id != user.id:
        await callback.answer("Только капитан может расформировать команду.", show_alert=True)
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Да, расформировать", callback_data=f"team:delete_confirm:{team.id}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="team:menu")],
    ])
    await _edit_or_answer(
        callback.message,
        f"⚠️ <b>Расформировать команду «{safe_html(team.name)}»?</b>\n\n"
        "Все участники будут освобождены, приглашения и незавершённые заявки команды будут удалены.\n"
        "Отменить это действие автоматически нельзя.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("team:delete_confirm:"))
async def cb_team_delete_confirm(callback: CallbackQuery) -> None:
    try:
        team_id = int(callback.data.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        await callback.answer("Некорректная команда.", show_alert=True)
        return
    user, team = await _get_caller_team(callback)
    if team is None or team.id != team_id:
        await callback.answer("Команда уже удалена или недоступна.", show_alert=True)
        return
    if team.captain_user_id != user.id:
        await callback.answer("Только капитан может расформировать команду.", show_alert=True)
        return
    try:
        async with db_session() as session:
            member_ids = await TeamService(session).delete_team(team_id, user.id)
            await session.commit()
        # Уведомления здесь специально не блокируют удаление, если Telegram
        # временно недоступен. Бывшие участники всё равно становятся свободными.
        await callback.message.answer(
            f"✅ Команда <b>{safe_html(team.name)}</b> расформирована.\n"
            "Все участники снова свободны и могут создать/вступить в другую команду.",
            reply_markup=home_kb(),
            parse_mode="HTML",
        )
    except (ValueError, PermissionError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Команда расформирована.")


@router.message(F.text == "/cancel")
async def team_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        return
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=home_kb())
