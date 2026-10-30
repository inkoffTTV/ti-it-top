"""Правила турниров: публичная видимость, создание админом, статусы."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Tournament, TournamentStatus
from database.repositories import ApplicationRepository, TeamRepository, TournamentRepository

logger = logging.getLogger("TOURNAMENT")


class TournamentService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.tournaments = TournamentRepository(session)
        self.teams = TeamRepository(session)
        self.autopair: dict | None = None  # отчёт автодобора пар после закрытия регистрации
        self.finished_now = False
        self.applications = ApplicationRepository(session)

    async def public_list(self) -> list[Tournament]:
        return await self.tournaments.list_public()

    async def create(
        self,
        name: str,
        description: str | None,
        max_teams: int,
        start_date: datetime | None,
        open_registration: bool = False,
    ) -> Tournament:
        from utils.validation import normalize_text

        clean = normalize_text(name)
        if not clean:
            raise ValueError("Название турнира не может быть пустым.")
        if len(clean) > 128:
            raise ValueError("Название слишком длинное (макс. 128 символов).")
        if max_teams < 2 or max_teams > 1024:
            raise ValueError("Максимум команд должен быть от 2 до 1024.")
        t = await self.tournaments.create(
            name=clean,
            description=(description or None),
            max_teams=int(max_teams),
            start_date=start_date,
            status=TournamentStatus.REGISTRATION if open_registration else TournamentStatus.DRAFT,
            registration_open=open_registration,
        )
        await self.session.commit()
        return t

    async def set_status(self, tournament_id: int, status: TournamentStatus) -> Tournament:
        t = await self.tournaments.get_by_id(tournament_id)
        if t is None:
            raise ValueError("Турнир не найден.")
        closing = t.status == TournamentStatus.REGISTRATION and status in (TournamentStatus.ACTIVE, TournamentStatus.DRAFT)
        # 5.13: турнир только что завершён — вызывающий код рассылает картинку с итогами.
        self.finished_now = status == TournamentStatus.FINISHED and t.status != TournamentStatus.FINISHED
        t.status = status
        t.registration_open = status == TournamentStatus.REGISTRATION
        await self.tournaments.update(t)
        await self.session.commit()
        logger.info("[TOURNAMENT] id=%s status=%s", tournament_id, status.value)
        # Регистрация закрыта: все без соперника встают в пары, BYE — только лишней команде.
        self.autopair = await self._autopair(t) if closing else None
        return t

    async def _autopair(self, t: Tournament) -> dict | None:
        from services.tournament_engine import service as engine

        try:
            report = await engine.autopair_leftovers(self.session, t)
            await self.session.commit()
        except Exception:  # noqa: BLE001 — смена статуса важнее, добор можно повторить кнопкой
            logger.exception("[TOURNAMENT] autopair failed id=%s", t.id)
            await self.session.rollback()
            await self.session.refresh(t)
            return None
        logger.info("[TOURNAMENT] autopair id=%s action=%s %s", t.id, report.get("action"), report.get("text"))
        return report

    async def registration_slots_left(self, t: Tournament) -> int:
        approved = await self.tournaments.approved_count(t.id)
        return max(0, t.max_teams - approved)

    @staticmethod
    def can_register(t: Tournament) -> tuple[bool, str | None]:
        if t.status != TournamentStatus.REGISTRATION or not t.registration_open:
            return False, "Регистрация на этот турнир закрыта."
        return True, None
