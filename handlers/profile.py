"""👤 Мой профиль: карточка игрока и регистрация (ввод Steam)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.models import VerificationStatus
from database.repositories import ProfileRepository, RankRangeRepository, TeamRepository, UserRepository
from handlers.common import RegistrationStates
from handlers.helpers import db_session, send_main_menu, safe_html
from keyboards.main import home_kb, profile_kb
from keyboards.registration import registration_start_kb, steam_help_kb
from services.factory import get_services
from utils.formatting import profile_card
from utils.ranks import rank_name_to_tier

logger = logging.getLogger("REGISTRATION")

router = Router(name="profile")


async def _registration_status_text(user_id: int) -> tuple[bool, str]:
    async with db_session() as session:
        profile = await ProfileRepository(session).get_by_user_id(user_id)
    if profile is None or profile.steam_id64 is None:
        return False, "Регистрация не пройдена — нужно указать Steam."
    if profile.verification_status == VerificationStatus.PENDING:
        return True, "⏳ Ваш Steam ожидает подтверждения организатором."
    if profile.verification_status == VerificationStatus.REJECTED:
        return True, "❌ Проверка отклонена. Обратитесь к организаторам через поддержку."
    return True, "✅ Регистрация подтверждена."


async def show_profile(message: Message) -> None:
    tg = message.from_user
    if tg is None:
        return
    async with db_session() as session:
        users = UserRepository(session)
        profiles = ProfileRepository(session)
        teams = TeamRepository(session)
        user = await users.upsert_from_telegram(tg.id, tg.username, tg.first_name, tg.last_name)
        profile = await profiles.get_by_user_id(user.id)
        team = await teams.get_for_user(user.id)
        team_name = team.name if team else None
        registered = bool(profile and profile.steam_id64)
        await session.commit()

    card = profile_card(
        tg_username=tg.username,
        tg_id=tg.id,
        steam_nickname=profile.steam_nickname if profile else None,
        steam_id64=profile.steam_id64 if profile else None,
        account_id=profile.account_id if profile else None,
        verification_status=profile.verification_status if profile else VerificationStatus.UNVERIFIED,
        source=profile.verification_source if profile else None,
        mmr=profile.mmr if profile else None,
        matches=profile.matches if profile else None,
        winrate=profile.winrate if profile else None,
        team_name=team_name,
        hours_total=profile.hours_total if profile else None,
        exact_mmr=profile.exact_mmr if profile else None,
        approved_mmr=profile.approved_mmr if profile else None,
        mmr_verified=profile.mmr_verified if profile else False,
        preferred_positions=profile.preferred_positions if profile else None,
    )
    if registered:
        await message.answer(
            card,
            reply_markup=profile_kb(in_team=team is not None, role_mode=team.role_mode if team else None),
            parse_mode="HTML",
        )
    else:
        await send_main_menu(message, card)
        await message.answer(
            "Для участия в турнирах пройдите регистрацию игрока.\n"
            "Нажмите кнопку ниже и отправьте Steam ID64, Dota Account ID или ссылку на профиль Steam.",
            reply_markup=registration_start_kb(),
        )


@router.message(F.text == "👤 Мой профиль")
async def kb_profile(message: Message) -> None:
    await show_profile(message)


# ---------------------------------------------------------------------------
# FSM регистрации: ввод Steam
# ---------------------------------------------------------------------------

async def begin_registration(callback: CallbackQuery, state: FSMContext) -> None:
    tg = callback.from_user
    if tg is None:
        await callback.answer()
        return
    async with db_session() as session:
        user = await UserRepository(session).upsert_from_telegram(
            tg.id, tg.username, tg.first_name, tg.last_name
        )
        await state.update_data(user_id=user.id)
        await session.commit()
    await state.set_state(RegistrationStates.waiting_steam)
    try:
        await callback.message.edit_text(_steam_prompt(), parse_mode="HTML")
    except Exception as e:  # noqa: BLE001 — edit может упасть на «нет изменений»
        logger.debug("[REGISTRATION] edit_text skipped: %s", e)
        await callback.message.answer(_steam_prompt(), parse_mode="HTML")
    await callback.message.answer(
        "Отправить ID можно обычным сообщением. Для отмены — кнопка ниже.",
        reply_markup=steam_help_kb(),
    )


def _steam_prompt() -> str:
    return (
        "🎮 <b>Шаг 1/1. Укажите ваш Steam</b>\n\n"
        "Поддерживается:\n"
        "• Steam ID64 — <code>7656119XXXXXXXXXX</code>\n"
        "• Ссылка профиля — <code>steamcommunity.com/profiles/7656119XXXXXXXXXX</code>\n"
        "• Dota Account ID — <code>1201721536</code>\n"
        "• Vanity-ссылка — <code>steamcommunity.com/id/&lt;ник&gt;</code>"
    )


@router.callback_query(F.data == "profile:rebind_steam")
async def cb_profile_rebind_steam(callback: CallbackQuery, state: FSMContext) -> None:
    tg = callback.from_user
    if tg is None:
        await callback.answer()
        return
    async with db_session() as session:
        user = await UserRepository(session).upsert_from_telegram(
            tg.id, tg.username, tg.first_name, tg.last_name
        )
        await state.update_data(user_id=user.id, rebind_steam=True)
        await session.commit()
    await state.set_state(RegistrationStates.waiting_steam)
    try:
        await callback.message.edit_text(
            "🔄 <b>Перепривязка Steam</b>\n\n"
            "Отправьте новый Steam ID64, ссылку на Steam-профиль, Dota Account ID или vanity-ссылку.\n\n"
            "После проверки новый Steam будет привязан к этому Telegram-профилю, а MMR снова уйдёт на подтверждение организатору.",
            parse_mode="HTML",
            reply_markup=steam_help_kb(),
        )
    except Exception:
        await callback.message.answer(
            "🔄 <b>Перепривязка Steam</b>\n\n"
            "Отправьте новый Steam ID64, ссылку на Steam-профиль, Dota Account ID или vanity-ссылку.\n\n"
            "После проверки новый Steam будет привязан к этому Telegram-профилю, а MMR снова уйдёт на подтверждение организатору.",
            parse_mode="HTML",
            reply_markup=steam_help_kb(),
        )
    await callback.answer()


@router.message(RegistrationStates.waiting_steam, F.text)
async def process_steam_input(message: Message, state: FSMContext) -> None:
    tg = message.from_user
    if tg is None:
        return

    raw = (message.text or "").strip()
    data = await state.get_data()
    user_id = data.get("user_id")

    await message.answer("⏳ Проверяю Steam…")
    services = get_services()
    result = await services.verification.verify_input(raw)

    if result.status == "INVALID_ID":
        await message.answer(
            f"⚠️ {safe_html(result.error)}\n\n"
            "Попробуйте ещё раз или нажмите «Отменить регистрацию».",
            reply_markup=steam_help_kb(),
        )
        return

    from handlers.helpers import notify_admins
    from handlers.common import get_config
    from services.registration import RegistrationService

    config = get_config()

    async with db_session() as session:
        user = await UserRepository(session).get_by_id(user_id) if user_id else None
        if user is None:
            user = await UserRepository(session).upsert_from_telegram(
                tg.id, tg.username, tg.first_name, tg.last_name
            )

        # Для турнирного допуска автоматической проверки личности недостаточно:
        # точный MMR заявляется отдельно и затем подтверждается проверяющим.
        svc = RegistrationService(session)
        async def _notify_admins(card: str) -> None:
            from keyboards.admin import verification_kb
            profile_for_button = await ProfileRepository(session).get_by_user_id(user.id)
            await notify_admins(
                message.bot,
                config,
                card,
                reply_markup=verification_kb(profile_for_button.id) if profile_for_button else None,
            )

        status = await svc.apply_verification(
            user_id=user.id,
            telegram_id=tg.id,
            username=tg.username,
            result=result,
            notify_admins_callback=_notify_admins,
            force_pending=True,
        )
        user.is_registered = True
        await session.commit()

        profile = await ProfileRepository(session).get_by_user_id(user.id)
        rank = await RankRangeRepository(session).get_by_tier(
            profile.medal_rank if profile else result.medal_rank
        )

    await state.update_data(user_id=user.id)
    # 5.1: медаль по источникам OpenDota/STRATZ и аватар — в фоне, без AI.
    try:
        from services.player_checks import schedule_player_check

        schedule_player_check(config, int(user.id), trigger="bot_steam", use_ai=False, notify_admins=False)
    except Exception:  # noqa: BLE001
        logger.exception("[REGISTRATION] player check was not scheduled")

    if rank is not None:
        await state.set_state(RegistrationStates.waiting_mmr)
        await message.answer(
            "🎯 <b>Шаг 2/2. Укажите точный MMR</b>\n\n"
            f"🏅 Ранг OpenDota: <b>{safe_html(rank.rank_name)} {rank.star}</b>\n"
            f"📏 Допустимый диапазон: <b>{rank.mmr_min}–{rank.mmr_max}</b>\n\n"
            "Откройте профиль в Dota 2 и отправьте ваш текущий MMR числом.\n"
            "После этого данные будут переданы проверяющему.",
            reply_markup=steam_help_kb(),
            parse_mode="HTML",
        )
    else:
        await state.set_state(RegistrationStates.waiting_mmr)
        await message.answer(
            "🎯 <b>Шаг 2/2. Укажите точный MMR</b>\n\n"
            "OpenDota не вернул rank_tier, поэтому автоматический диапазон "
            "определить не удалось.\n\n"
            "Отправьте текущий MMR числом. Проверяющий сверит его с аккаунтом.",
            reply_markup=steam_help_kb(),
            parse_mode="HTML",
        )

    logger.info(
        "[REGISTRATION] User %s linked Steam; exact MMR is awaiting input",
        tg.id,
    )


@router.message(RegistrationStates.waiting_mmr, F.text)
async def process_exact_mmr(message: Message, state: FSMContext) -> None:
    tg = message.from_user
    if tg is None:
        return

    raw = (message.text or "").strip().replace(" ", "")
    if not raw.isdigit():
        await message.answer(
            "❌ Введите MMR целым числом, например: <code>5234</code>.",
            parse_mode="HTML",
        )
        return

    exact_mmr = int(raw)
    if exact_mmr < 0 or exact_mmr > 9500:
        await message.answer("❌ MMR должен быть от 0 до 9500.")
        return

    data = await state.get_data()
    user_id = data.get("user_id")
    if not user_id:
        await state.clear()
        await message.answer("❌ Сессия регистрации устарела. Начните регистрацию заново.")
        return

    from handlers.helpers import notify_admins
    from handlers.common import get_config
    from services.registration import RegistrationService

    config = get_config()

    async with db_session() as session:
        svc = RegistrationService(session)
        ok, error, rank = await svc.save_exact_mmr(
            int(user_id),
            exact_mmr,
        )

        if not ok:
            await message.answer(
                f"❌ {safe_html(error)}\n\n"
                "Введите MMR ещё раз.",
                parse_mode="HTML",
                reply_markup=steam_help_kb(),
            )
            return

        profile = await ProfileRepository(session).get_by_user_id(int(user_id))
        user = await UserRepository(session).get_by_id(int(user_id))
        if profile is not None and profile.medal_rank is None:
            derived_tier = rank_name_to_tier(profile.rank_name)
            if derived_tier is not None:
                profile.medal_rank = derived_tier
                await session.commit()
                rank = await RankRangeRepository(session).get_by_tier(derived_tier)

    if user is not None and profile is not None:
        rank_text = (
            f"{rank.rank_name} {rank.star} ({rank.mmr_min}–{rank.mmr_max})"
            if rank is not None
            else "диапазон не определён"
        )
        admin_text = (
            "🔎 <b>Новая проверка MMR</b>\n\n"
            f"👤 Telegram: @{safe_html(user.username) if user.username else user.telegram_id}\n"
            f"🆔 Telegram ID: <code>{user.telegram_id}</code>\n"
            f"🎮 Steam ID64: <code>{safe_html(profile.steam_id64)}</code>\n"
            f"🎯 Заявленный MMR: <b>{exact_mmr}</b>\n"
            f"🏅 Ранг: <b>{safe_html(rank_text)}</b>\n\n"
            "Проверьте MMR непосредственно на аккаунте и подтвердите или отклоните заявку."
        )
        from keyboards.admin import verification_kb
        await notify_admins(
            message.bot,
            config,
            admin_text,
            reply_markup=verification_kb(profile.id),
        )
        # 5.1: фоновая проверка игрока (медаль по источникам + риск смурфа, AI).
        # Не задерживает ответ игроку; при подозрении админы получат сводку.
        try:
            from services.player_checks import schedule_player_check

            schedule_player_check(config, int(user.id), trigger="bot_mmr", use_ai=False)
        except Exception:  # noqa: BLE001
            logger.exception("[REGISTRATION] player check was not scheduled")

    await state.clear()
    await message.answer(
        "⏳ <b>Данные отправлены на проверку.</b>\n\n"
        f"Заявленный MMR: <b>{exact_mmr}</b>\n"
        "Организатор сверит его с игровым аккаунтом и сообщит результат.",
        reply_markup=home_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "profile:mmr")
async def cb_profile_mmr(callback: CallbackQuery, state: FSMContext) -> None:
    tg = callback.from_user
    if tg is None:
        await callback.answer()
        return
    async with db_session() as session:
        user = await UserRepository(session).get_by_telegram_id(tg.id)
        profile = await ProfileRepository(session).get_by_user_id(user.id) if user else None
        if user is None or profile is None or profile.steam_id64 is None:
            await callback.answer("Сначала пройдите регистрацию Steam.", show_alert=True)
            return
        rank = await RankRangeRepository(session).get_by_tier(profile.medal_rank)
    await state.update_data(user_id=user.id)
    await state.set_state(RegistrationStates.waiting_mmr)
    if rank is not None:
        text = (
            "✏️ <b>Указать точный MMR</b>\n\n"
            f"🏅 Ранг: <b>{safe_html(rank.rank_name)} {rank.star}</b>\n"
            f"📏 Допустимый диапазон: <b>{rank.mmr_min}–{rank.mmr_max}</b>\n\n"
            "Введите текущий MMR числом. После изменения заявка снова уйдёт организатору на ручную проверку."
        )
    else:
        text = (
            "✏️ <b>Указать точный MMR</b>\n\n"
            "Введите текущий MMR числом. Организатор сверит его с аккаунтом вручную."
        )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=steam_help_kb())
    await callback.answer()



def _position_pool_kb(selected: set[int]) -> InlineKeyboardMarkup:
    labels = {
        1: "1 — Керри",
        2: "2 — Мид",
        3: "3 — Хард",
        4: "4 — Четверка",
        5: "5 — Пятерка",
    }
    rows = []
    for pos, label in labels.items():
        rows.append([
            InlineKeyboardButton(
                text=("✅ " if pos in selected else "⬜ ") + label,
                callback_data=f"profile:pool:{pos}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="💾 Сохранить", callback_data="profile:pool:save"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="profile:pool:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "profile:pool")
async def cb_profile_pool(callback: CallbackQuery, state: FSMContext) -> None:
    tg = callback.from_user
    async with db_session() as session:
        user = await UserRepository(session).get_by_telegram_id(tg.id)
        profile = await ProfileRepository(session).get_by_user_id(user.id) if user else None
    raw = (profile.preferred_positions if profile else None) or ""
    selected = {int(x) for x in raw.split(",") if x.isdigit() and 1 <= int(x) <= 5}
    await state.update_data(pool_positions=sorted(selected))
    await callback.message.edit_text(
        "🎮 <b>Пул ролей</b>\n\nВыберите все позиции, на которых готовы играть.",
        reply_markup=_position_pool_kb(selected),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile:pool:"))
async def cb_profile_pool_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.rsplit(":", 1)[-1]
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text(
            "❌ Изменение пула ролей отменено.",
            reply_markup=home_kb(),
        )
        await callback.answer()
        return
    if action == "save":
        data = await state.get_data()
        positions = list(data.get("pool_positions") or [])
        tg = callback.from_user
        async with db_session() as session:
            user = await UserRepository(session).get_by_telegram_id(tg.id)
            if user:
                await __import__("services.teams", fromlist=["TeamService"]).TeamService(
                    session
                ).set_preferred_positions(user.id, positions)
        await state.clear()
        await callback.message.edit_text(
            "✅ Пул ролей сохранён.",
            reply_markup=home_kb(),
        )
        await callback.answer()
        return

    try:
        position = int(action)
    except ValueError:
        await callback.answer("Некорректная позиция.", show_alert=True)
        return
    data = await state.get_data()
    selected = set(data.get("pool_positions") or [])
    if position in selected:
        selected.remove(position)
    else:
        selected.add(position)
    await state.update_data(pool_positions=sorted(selected))
    await callback.message.edit_reply_markup(reply_markup=_position_pool_kb(selected))
    await callback.answer()


@router.callback_query(F.data == "profile:position")
async def cb_profile_position(callback: CallbackQuery) -> None:
    tg = callback.from_user
    async with db_session() as session:
        user = await UserRepository(session).get_by_telegram_id(tg.id)
        team = await TeamRepository(session).get_for_user(user.id) if user else None
        member = (
            await TeamRepository(session).get_member(team.id, user.id)
            if user and team else None
        )
    if user is None or team is None or member is None:
        await callback.answer("Вы не состоите в команде.", show_alert=True)
        return
    if team.role_mode != "PLAYER_CHOICE" and team.captain_user_id != user.id:
        await callback.answer("В этой команде позиции назначает капитан.", show_alert=True)
        return
    from keyboards.team import positions_kb
    await callback.message.edit_text(
        "🎯 <b>Моя позиция в команде</b>",
        reply_markup=positions_kb("profile:setpos", member.position),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile:setpos:"))
async def cb_profile_set_position(callback: CallbackQuery) -> None:
    position = int(callback.data.rsplit(":", 1)[-1])
    tg = callback.from_user
    async with db_session() as session:
        user = await UserRepository(session).get_by_telegram_id(tg.id)
        team = await TeamRepository(session).get_for_user(user.id) if user else None
        if user is None or team is None:
            await callback.answer("Команда не найдена.", show_alert=True)
            return
        try:
            await __import__("services.teams", fromlist=["TeamService"]).TeamService(
                session
            ).set_position(team.id, user.id, position, user.id)
        except (ValueError, PermissionError) as e:
            await callback.answer(str(e), show_alert=True)
            return
    await callback.answer(f"Позиция {position} назначена.")
    await callback.message.answer(
        f"✅ Ваша позиция: <b>{position}</b>",
        reply_markup=home_kb(),
        parse_mode="HTML",
    )
