"""🔎 Админ: проверка игроков — очередь PENDING, ручное решение, 🔄 recheck."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from database.models import VerificationStatus
from database.repositories import (
    ProfileRepository,
    TeamRepository,
    UserRepository,
    VerificationRepository,
)
from handlers.admin.panel import _deny
from handlers.helpers import db_session, parse_cb, safe_html
from keyboards.admin import back_to_admin_kb, verification_kb
from services.factory import get_services
from utils.ranks import rank_name_to_tier
from utils.formatting import status_label, verification_request_card

logger = logging.getLogger("ADMIN")

router = Router(name="admin_verification")

class ManualMMRStates(StatesGroup):
    waiting_mmr = State()
    waiting_profile_id = State()



@router.callback_query(F.data == "adm:verification")
async def cb_verification_queue(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    async with db_session() as session:
        profiles = await ProfileRepository(session).pending_profiles(limit=30)
        users = UserRepository(session)
        rows = []
        lines = [f"🔎 <b>Очередь проверки игроков ({len(profiles)})</b>", ""]
        if not profiles:
            lines.append("Ожидающих проверки нет.")
        for p in profiles:
            u = await users.get_by_id(p.user_id)
            name = (u.username or u.first_name or str(p.user_id)) if u else f"user:{p.user_id}"
            sid = p.steam_id64 or "?"
            lines.append(f"• {safe_html(name)} — Steam {sid}")
            rows.append([InlineKeyboardButton(
                text=f"{name[:28]} • {sid[-9:]}",
                callback_data=f"ver:view:{p.id}",
            )])
    rows.append([InlineKeyboardButton(text="◀️ В админ-панель", callback_data="adm:menu")])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] verification queue edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _profile_card_text(session, profile_id: int) -> tuple[str | None, object | None]:
    profiles = ProfileRepository(session)
    from database.models import PlayerProfile

    profile = await session.get(PlayerProfile, profile_id)
    if profile is None:
        return None, None
    user = await UserRepository(session).get_by_id(profile.user_id)
    team = await TeamRepository(session).get_for_user(profile.user_id)
    card = verification_request_card(
        tg_username=user.username if user else None,
        tg_id=user.telegram_id if user else profile.user_id,
        steam_profile_url=(
            f"https://steamcommunity.com/profiles/{profile.steam_id64}"
            if profile.steam_id64 else None
        ),
        steam_id64=profile.steam_id64,
        account_id=profile.account_id,
        nickname=profile.steam_nickname,
        source=profile.verification_source,
        mmr=profile.mmr,
        matches=profile.matches,
        winrate=profile.winrate,
        rank_name=profile.rank_name,
        leaderboard_rank=profile.leaderboard_rank,
        exact_mmr=profile.exact_mmr,
        mmr_verified=profile.mmr_verified,
    )
    if profile.medal_rank is None:
        derived_tier = rank_name_to_tier(profile.rank_name)
        if derived_tier is not None:
            profile.medal_rank = derived_tier
            await session.commit()
    rank = await __import__('database.repositories', fromlist=['RankRangeRepository']).RankRangeRepository(session).get_by_tier(profile.medal_rank)
    extra = [f"\n<b>Статус:</b> {status_label(profile.verification_status)}"]
    if rank is not None:
        extra.append(f"<b>Допустимый MMR:</b> {rank.mmr_min}–{rank.mmr_max}")
    extra.append(f"<b>Точный MMR подтверждён:</b> {'Да' if profile.mmr_verified else 'Нет'}")
    if team is not None:
        extra.append(f"<b>Команда:</b> {safe_html(team.name)}")
    if profile.admin_comment:
        extra.append(f"<b>Комментарий:</b> {safe_html(profile.admin_comment)}")
    return card + "\n" + "\n".join(extra), profile


@router.callback_query(F.data.startswith("ver:view:"))
async def cb_verification_view(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    profile_id = int(parts[2])
    async with db_session() as session:
        text, profile = await _profile_card_text(session, profile_id)
    if text is None or profile is None:
        await callback.answer("Профиль не найден.", show_alert=True)
        return
    kb = verification_kb(profile.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADMIN] profile card edit skipped: %s", e)
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _decide_profile(callback: CallbackQuery, profile_id: int, decision: VerificationStatus) -> str:
    """Ручное решение по Steam + exact MMR."""
    from database.models import PlayerProfile
    from services.registration import RegistrationService

    admin_tg_id = callback.from_user.id
    async with db_session() as session:
        profile = await session.get(PlayerProfile, profile_id)
        if profile is None:
            return "Профиль не найден (возможно, удалён)."

        user = await UserRepository(session).get_by_id(profile.user_id)
        svc = RegistrationService(session)

        if decision == VerificationStatus.VERIFIED:
            if profile.exact_mmr is None:
                return "Сначала игрок должен указать точный MMR."
            # Диапазон rank_tier показывается как справочная информация,
            # но не блокирует ручное подтверждение: организатор сверяет
            # фактический MMR непосредственно по аккаунту игрока.
            await svc.set_mmr_decision(
                profile,
                verified=True,
                admin_id=admin_tg_id,
            )
        else:
            await svc.set_mmr_decision(
                profile,
                verified=False,
                admin_id=admin_tg_id,
                comment="Проверка точного MMR отклонена организатором.",
            )

        telegram_id = user.telegram_id if user else None
        from services.activity import add_event
        await add_event(session, kind="mmr_decision", title="MMR подтверждён" if decision == VerificationStatus.VERIFIED else "MMR отклонён", body=f"Профиль #{profile.id}", target_user_id=profile.user_id)
        await session.commit()

    if telegram_id is not None:
        label = (
            "✅ <b>Проверка игрока пройдена.</b>\n"
            "Ваш точный MMR подтверждён организатором."
            if decision == VerificationStatus.VERIFIED
            else
            "❌ <b>Проверка игрока отклонена.</b>\n"
            "Указанный MMR не был подтверждён. Обратитесь к организаторам."
        )
        try:
            await callback.message.bot.send_message(telegram_id, label, parse_mode="HTML")
        except Exception as e:  # noqa: BLE001
            logger.warning("[VERIFICATION] Cannot notify user tg=%s: %s", telegram_id, e)

    logger.info(
        "[VERIFICATION] Admin %s set profile %s -> %s",
        admin_tg_id,
        profile_id,
        decision.value,
    )
    return f"Профиль #{profile_id}: {status_label(decision)}"


@router.callback_query(F.data.startswith("ver:mmr:"))
async def cb_manual_mmr_start(callback: CallbackQuery, state: FSMContext) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    profile_id = int(parts[2])
    async with db_session() as session:
        from database.models import PlayerProfile
        profile = await session.get(PlayerProfile, profile_id)
        if profile is None:
            await callback.answer("Профиль не найден.", show_alert=True)
            return
    await state.set_state(ManualMMRStates.waiting_mmr)
    await state.update_data(profile_id=profile_id)
    await callback.message.answer(
        "✏️ <b>Ручной ввод MMR</b>\n\n"
        "Отправьте числовой MMR игрока (например, <code>5234</code>).\n"
        "Для отмены: /cancel" ,
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ManualMMRStates.waiting_mmr, F.text)
async def cb_manual_mmr_input(message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "")
    if not raw.isdigit():
        await message.answer("❌ Введите MMR целым числом, например: <code>5234</code>.", parse_mode="HTML")
        return
    mmr = int(raw)
    if mmr < 0 or mmr > 9500:
        await message.answer("❌ MMR должен быть в диапазоне 0–9500.")
        return
    data = await state.get_data()
    profile_id = data.get("profile_id")
    if not profile_id:
        await state.clear()
        await message.answer("❌ Сессия ручного ввода устарела. Откройте профиль заново.")
        return
    async with db_session() as session:
        from database.models import PlayerProfile
        profile = await session.get(PlayerProfile, int(profile_id))
        if profile is None:
            await state.clear()
            await message.answer("❌ Профиль не найден.")
            return
        rank = await __import__(
            "database.repositories", fromlist=["RankRangeRepository"]
        ).RankRangeRepository(session).get_by_tier(profile.medal_rank)
        if rank is not None and not (rank.mmr_min <= mmr <= rank.mmr_max):
            from handlers.common import get_config
            if get_config().mmr_rank_range_hard_block:
                await message.answer(
                    f"❌ MMR {mmr} не входит в диапазон {rank.mmr_min}–{rank.mmr_max} "
                    f"для {rank.rank_name} {rank.star}."
                )
                return
            await message.answer(
                f"⚠️ MMR {mmr} отличается от справочного диапазона {rank.mmr_min}–{rank.mmr_max}. "
                "Сохраняю: итоговое решение остаётся за организатором."
            )
        profile.exact_mmr = mmr
        profile.approved_mmr = None
        profile.mmr_verified = False
        profile.mmr_verified_by = None
        profile.mmr_verified_at = None
        profile.verification_status = VerificationStatus.PENDING
        profile.updated_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        await session.commit()
        user = await UserRepository(session).get_by_id(profile.user_id)
    await state.clear()
    await message.answer(f"✅ MMR сохранён: <b>{mmr}</b>", parse_mode="HTML")


@router.callback_query(F.data.startswith("ver:approve:"))
async def cb_verification_approve(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    msg = await _decide_profile(callback, int(parts[2]), VerificationStatus.VERIFIED)
    await callback.answer(msg, show_alert=True)


@router.callback_query(F.data.startswith("ver:reject:"))
async def cb_verification_reject(callback: CallbackQuery) -> None:
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    msg = await _decide_profile(callback, int(parts[2]), VerificationStatus.REJECTED)
    await callback.answer(msg, show_alert=True)


@router.callback_query(F.data.startswith("ver:recheck:"))
async def cb_verification_recheck(callback: CallbackQuery) -> None:
    """🔄 Повторная проверка: сброс кэша и повторный опрос провайдеров."""
    if _deny(callback):
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    parts = parse_cb(callback.data or "")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    profile_id = int(parts[2])
    from database.models import PlayerProfile

    async with db_session() as session:
        profile = await session.get(PlayerProfile, profile_id)
        if profile is None or not profile.steam_id64 or profile.account_id is None:
            await callback.answer("Нет данных Steam для повторной проверки.", show_alert=True)
            return
        sid, aid = profile.steam_id64, profile.account_id
    services = get_services()
    result = await services.verification.recheck(sid, aid)
    # Авто-recheck обновляет данные, но не выдаёт турнирный допуск.
    # Финальный VERIFIED ставится только после ручной сверки exact_mmr.
    async with db_session() as session:
        profile = await session.get(PlayerProfile, profile_id)
        if profile is not None:
            if result.mmr is not None:
                profile.mmr = result.mmr
            if result.rank_name is not None:
                profile.rank_name = result.rank_name
            if result.medal_rank is not None:
                profile.medal_rank = result.medal_rank
                from utils.ranks import rank_label_from_tier
                profile.rank_name = rank_label_from_tier(result.medal_rank) or profile.rank_name
            if result.leaderboard_rank is not None:
                profile.leaderboard_rank = result.leaderboard_rank
            if result.matches is not None:
                profile.matches = result.matches
            if result.winrate is not None:
                profile.winrate = result.winrate
            if result.hours_total is not None:
                profile.hours_total = result.hours_total
            if not profile.mmr_verified:
                profile.verification_status = VerificationStatus.PENDING
                profile.verification_source = f"recheck:{result.source}"
            await session.commit()
    await callback.answer(
        f"🔄 Данные обновлены: {result.status}. Финальный допуск — после проверки MMR.",
        show_alert=True,
    )


async def recheck_profile_by_user(user_db_id: int) -> str:
    """Recheck из карточки пользователя (adm:urecheck:<user_db_id>)."""
    from database.models import PlayerProfile
    from sqlalchemy import select

    async with db_session() as session:
        res = await session.execute(select(PlayerProfile).where(PlayerProfile.user_id == user_db_id))
        profile = res.scalars().first()
        if profile is None or not profile.steam_id64 or profile.account_id is None:
            return "У пользователя нет Steam-данных для проверки."
        sid, aid = profile.steam_id64, profile.account_id
    services = get_services()
    result = await services.verification.recheck(sid, aid)
    if result.status == "VERIFIED":
        async with db_session() as session:
            prof = await session.get(PlayerProfile, profile.id)
            if prof is not None:
                # Обновляем реально полученные данные (None не затирает старое).
                if result.mmr is not None:
                    prof.mmr = result.mmr
                if result.rank_name is not None:
                    prof.rank_name = result.rank_name
                if result.matches is not None:
                    prof.matches = result.matches
                if result.winrate is not None:
                    prof.winrate = result.winrate
                if result.hours_total is not None:
                    prof.hours_total = result.hours_total
                if result.medal_rank is not None:
                    prof.medal_rank = result.medal_rank
                    from utils.ranks import rank_label_from_tier
                    prof.rank_name = rank_label_from_tier(result.medal_rank) or prof.rank_name
                if result.leaderboard_rank is not None:
                    prof.leaderboard_rank = result.leaderboard_rank
                if result.nickname:
                    prof.steam_nickname = result.nickname
                if not prof.mmr_verified:
                    prof.verification_status = VerificationStatus.PENDING
                    prof.verification_source = f"recheck:{result.source}"
                await session.commit()
    return f"Повторная проверка: {result.status} (source={result.source})"
