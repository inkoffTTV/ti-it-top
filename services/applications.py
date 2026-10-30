"""Заявки команд на турниры: все проверки перед подачей."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    ApplicationStatus,
    TournamentApplication,
    VerificationStatus,
)
from database.repositories import (
    ApplicationRepository,
    ProfileRepository,
    TeamRepository,
    TournamentRepository,
    UserRepository,
)
from services.teams import MAX_ROSTER_SIZE
from services.tournaments import TournamentService

logger = logging.getLogger("APPLICATION")

MIN_PLAYERS_TO_APPLY = 5


class ApplicationService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.applications = ApplicationRepository(session)
        self.tournaments = TournamentRepository(session)
        self.teams = TeamRepository(session)
        self.users = UserRepository(session)
        self.profiles = ProfileRepository(session)
        self.tournament_service = TournamentService(session)

    async def validate_registration(
        self, user_id: int, tournament_id: int
    ) -> tuple[bool, str]:
        """Полный набор проверок из ТЗ. Возвращает (ok, сообщение)."""
        user = await self.users.get_by_id(user_id)
        if user is None:
            return False, "Сначала зарегистрируйтесь: /start"

        team = await self.teams.get_by_captain(user_id)
        if team is None:
            return False, "Вы не капитан команды. Создайте команду через «👑 Моя команда»."

        roster = await self.teams.get_players(team.id)  # тренер не считается игроком
        if not roster:
            return False, "Состав команды пуст — добавьте игроков."
        if len(roster) < MIN_PLAYERS_TO_APPLY:
            return False, (
                f"Для заявки нужно минимум {MIN_PLAYERS_TO_APPLY} игроков в составе "
                f"(сейчас {len(roster)})."
            )
        if len(roster) > MAX_ROSTER_SIZE:
            return False, "В составе больше допустимого количества игроков."

        for m in roster:
            profile = await self.profiles.get_by_user_id(m.user_id)
            status = profile.verification_status if profile else VerificationStatus.UNVERIFIED
            if status == VerificationStatus.REJECTED:
                uname = profile.user.username if profile and profile.user else m.user_id
                return False, (
                    f"Игрок {uname} отклонён организаторами. Замените его в составе."
                )

        t = await self.tournaments.get_by_id(tournament_id)
        if t is None:
            return False, "Турнир не найден или был удалён."
        ok, err = TournamentService.can_register(t)
        if not ok:
            return False, err or "Регистрация закрыта."

        if await self.applications.exists(tournament_id, team.id):
            return False, "Команда уже подавала заявку на этот турнир."

        active = await self.applications.get_active_for_team(team.id)
        if active is not None and active.tournament_id != tournament_id:
            return False, "У команды уже есть активная заявка на другой турнир."

        slots = await self.tournament_service.registration_slots_left(t)
        if slots <= 0:
            return False, "Лимит участников турнира исчерпан."

        return True, "ok"

    async def submit(self, user_id: int, tournament_id: int) -> TournamentApplication:
        ok, message = await self.validate_registration(user_id, tournament_id)
        if not ok:
            raise ValueError(message)
        team = await self.teams.get_by_captain(user_id)
        assert team is not None
        app = await self.applications.create(
            tournament_id=tournament_id, team_id=team.id, created_by=user_id
        )
        await self.session.commit()
        return app

    async def my_application(self, user_id: int) -> TournamentApplication | None:
        """Активная/последняя заявка команды пользователя (капитана или игрока)."""
        team = await self.teams.get_for_user(user_id)
        if team is None:
            return None
        active = await self.applications.get_active_for_team(team.id)
        if active is not None:
            return active
        all_apps = await self.applications.get_for_team(team.id)
        return all_apps[0] if all_apps else None

    @staticmethod
    def decision_text(app: TournamentApplication) -> str:
        if app.status == ApplicationStatus.APPROVED:
            return "✅ Заявка одобрена организатором."
        if app.status == ApplicationStatus.REJECTED:
            comment = f" Комментарий: {app.admin_comment}" if app.admin_comment else ""
            return f"❌ Заявка отклонена организатором.{comment}"
        if app.status == ApplicationStatus.CANCELLED:
            return "⛔ Заявка отменена."
        return "⏳ Заявка на рассмотрении."
