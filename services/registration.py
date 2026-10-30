"""RegistrationService: сохранение результата проверки в БД + уведомление админов."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import VerificationStatus
from database.repositories import ProfileRepository, RankRangeRepository, UserRepository, VerificationRepository
from services.player_verification import VerificationResult
from utils.formatting import verification_request_card
from utils.ranks import rank_name_to_tier

logger = logging.getLogger("REGISTRATION")

STATUS_MAP = {
    "VERIFIED": VerificationStatus.VERIFIED,
    "PENDING": VerificationStatus.PENDING,
    "REJECTED": VerificationStatus.REJECTED,
}


class RegistrationService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.users = UserRepository(session)
        self.profiles = ProfileRepository(session)
        self.verifications = VerificationRepository(session)

    async def apply_verification(
        self,
        user_id: int,
        telegram_id: int,
        username: str | None,
        result: VerificationResult,
        notify_admins_callback=None,
        force_pending: bool = False,
    ) -> VerificationStatus:
        """Сохраняет результат верификации. При PENDING — заявка администраторам.

        notify_admins_callback: async callable(text) -> None (отправка карточки админам).
        Возвращает итоговый VerificationStatus.
        """
        status = VerificationStatus.PENDING if force_pending else STATUS_MAP.get(result.status, VerificationStatus.PENDING)
        profile = await self.profiles.get_or_create(user_id)
        profile.steam_id64 = result.steam_id64
        profile.account_id = result.account_id
        profile.steam_nickname = result.nickname
        # Статистика: сохраняем только реально полученные значения.
        # None НЕ затирает ранее сохранённые данные (защита при recheck с недоступными API).
        if result.mmr is not None:
            profile.mmr = result.mmr
        if result.rank_name is not None:
            profile.rank_name = result.rank_name
        if result.matches is not None:
            profile.matches = result.matches
        if result.winrate is not None:
            profile.winrate = result.winrate
        if result.hours_total is not None:
            profile.hours_total = result.hours_total
        if result.medal_rank is not None:
            profile.medal_rank = result.medal_rank
            from utils.ranks import rank_label_from_tier
            profile.rank_name = rank_label_from_tier(result.medal_rank) or profile.rank_name
        if result.leaderboard_rank is not None:
            profile.leaderboard_rank = result.leaderboard_rank
        elif profile.medal_rank is None:
            # Старые профили могли иметь rank_name (например, «Архонт 1»),
            # но не иметь сохранённого rank_tier. Восстанавливаем tier из
            # уже сохранённого реального ранга, чтобы диапазон MMR находился.
            derived_tier = rank_name_to_tier(profile.rank_name)
            if derived_tier is not None:
                profile.medal_rank = derived_tier
        await self.profiles.set_status(profile, status, source=result.source)
        if force_pending:
            # Новая привязка Steam требует нового ручного подтверждения MMR.
            profile.exact_mmr = None
            profile.mmr_verified = False
            profile.mmr_verified_by = None
            profile.mmr_verified_at = None

        user = await self.users.get_by_id(user_id)
        await self.verifications.record(
            user_id=user_id,
            profile_id=profile.id,
            status=status,
            source=result.source,
            performed_by=None if status != VerificationStatus.VERIFIED else None,
            comment="; ".join(result.notes) or None,
        )
        await self.session.commit()
        logger.info(
            "[REGISTRATION] User %s steam=%s saved with status %s",
            user_id, result.steam_id64, status.value,
        )

        if status == VerificationStatus.PENDING and notify_admins_callback is not None:
            card = verification_request_card(
                tg_username=username,
                tg_id=telegram_id,
                steam_profile_url=result.profile_url,
                steam_id64=result.steam_id64,
                account_id=result.account_id,
                nickname=result.nickname,
                source=result.source,
                mmr=result.mmr,
                matches=result.matches,
                winrate=result.winrate,
                rank_name=result.rank_name,
                leaderboard_rank=result.leaderboard_rank,
            )
            try:
                await notify_admins_callback(card)
            except Exception as e:  # noqa: BLE001 — уведомление не ломает регистрацию
                logger.error("[REGISTRATION] Admin notification failed: %s", e)

        return status


    async def save_exact_mmr(
        self,
        user_id: int,
        exact_mmr: int,
        *,
        notify_admins_callback=None,
    ) -> tuple[bool, str, object | None]:
        """Сохраняет заявленный точный MMR после проверки диапазона rank_tier."""
        profile = await self.profiles.get_by_user_id(user_id)
        if profile is None:
            return False, "Профиль не найден.", None

        if profile.medal_rank is None:
            derived_tier = rank_name_to_tier(profile.rank_name)
            if derived_tier is not None:
                profile.medal_rank = derived_tier
                await self.session.flush()

        rank = await RankRangeRepository(self.session).get_by_tier(profile.medal_rank)
        # Диапазон медали — только справочная подсказка. OpenDota rank_tier
        # может быть устаревшим/не совпадать с введённым текущим MMR, поэтому
        # ручной ввод не блокируем. Финальное решение остаётся за организатором.
        if exact_mmr < 0 or exact_mmr > 9500:
            return False, "MMR должен быть от 0 до 9500.", rank

        profile.exact_mmr = exact_mmr
        profile.approved_mmr = None
        profile.mmr_verified = False
        profile.mmr_verified_by = None
        profile.mmr_verified_at = None
        profile.verification_status = VerificationStatus.PENDING
        profile.updated_at = datetime.now(timezone.utc)

        await self.verifications.record(
            user_id=user_id,
            profile_id=profile.id,
            status=VerificationStatus.PENDING,
            source="manual_mmr",
            comment=f"Заявленный exact_mmr={exact_mmr}",
        )
        await self.session.commit()

        return True, "ok", rank

    async def set_mmr_decision(
        self,
        profile,
        *,
        verified: bool,
        admin_id: int,
        comment: str | None = None,
    ) -> None:
        profile.mmr_verified = verified
        profile.approved_mmr = profile.exact_mmr if verified else None
        profile.mmr_verified_by = admin_id
        profile.mmr_verified_at = datetime.now(timezone.utc)
        profile.verification_status = (
            VerificationStatus.VERIFIED if verified else VerificationStatus.REJECTED
        )
        profile.verified_by = admin_id
        profile.verified_at = datetime.now(timezone.utc)
        profile.verification_source = "manual_mmr"
        if comment:
            profile.admin_comment = comment

        await self.verifications.record(
            user_id=profile.user_id,
            profile_id=profile.id,
            status=profile.verification_status,
            source="manual_mmr",
            performed_by=admin_id,
            comment=comment or (
                "Точный MMR подтверждён по аккаунту."
                if verified else "Точный MMR не подтверждён."
            ),
        )
        await self.session.commit()
