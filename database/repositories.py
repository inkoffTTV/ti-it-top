"""Репозитории: слой доступа к данным (SQLAlchemy 2.x select-стиль)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    ApplicationStatus,
    PlayerProfile,
    RankRange,
    TeamInvite,
    TeamInviteStatus,
    SupportTicket,
    Team,
    TeamMember,
    TeamRole,
    Tournament,
    TournamentApplication,
    User,
    VerificationRecord,
    VerificationStatus,
)

logger = logging.getLogger("DATABASE")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        res = await self.session.execute(select(User).where(User.telegram_id == telegram_id))
        return res.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        name = username.lstrip("@").lower()
        res = await self.session.execute(
            select(User).where(func.lower(func.replace(User.username, "@", "")) == name)
        )
        return res.scalars().first()

    async def upsert_from_telegram(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> User:
        """Создаёт пользователя или обновляет данные. Дубликатов не создаёт."""
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            self.session.add(user)
            try:
                await self.session.flush()
            except IntegrityError:
                # Гонка: пользователь уже создан параллельным запросом.
                await self.session.rollback()
                user = await self.get_by_telegram_id(telegram_id)
                assert user is not None
                return user
            logger.info("[DATABASE] User created telegram_id=%s", telegram_id)
        else:
            changed = False
            if username is not None and user.username != username:
                user.username = username
                changed = True
            if first_name is not None and user.first_name != first_name:
                user.first_name = first_name
                changed = True
            if last_name is not None and user.last_name != last_name:
                user.last_name = last_name
                changed = True
            if changed:
                user.updated_at = utcnow()
                logger.info("[DATABASE] User updated telegram_id=%s", telegram_id)
        return user

    async def list_users(self, limit: int = 50, offset: int = 0) -> list[User]:
        res = await self.session.execute(
            select(User).order_by(User.id).limit(limit).offset(offset)
        )
        return list(res.scalars().all())

    async def count(self) -> int:
        res = await self.session.execute(select(func.count()).select_from(User))
        return int(res.scalar_one())

    async def search(self, query: str, limit: int = 25) -> list[User]:
        q = f"%{query.lstrip('@')}%"
        ql = q.lower()
        res = await self.session.execute(
            select(User)
            .where(
                func.lower(User.username).like(ql)
                | func.lower(User.first_name).like(ql)
                | func.lower(User.last_name).like(ql)
            )
            .limit(limit)
        )
        return list(res.scalars().all())


# ---------------------------------------------------------------------------
# Player profiles
# ---------------------------------------------------------------------------

class ProfileRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_user_id(self, user_id: int) -> PlayerProfile | None:
        res = await self.session.execute(
            select(PlayerProfile).where(PlayerProfile.user_id == user_id)
        )
        return res.scalar_one_or_none()

    async def get_or_create(self, user_id: int) -> PlayerProfile:
        profile = await self.get_by_user_id(user_id)
        if profile is None:
            profile = PlayerProfile(
                user_id=user_id, verification_status=VerificationStatus.UNVERIFIED
            )
            self.session.add(profile)
            try:
                await self.session.flush()
            except IntegrityError:
                await self.session.rollback()
                profile = await self.get_by_user_id(user_id)
                assert profile is not None
        return profile

    async def find_by_steam_id64(self, steam_id64: str) -> PlayerProfile | None:
        res = await self.session.execute(
            select(PlayerProfile).where(PlayerProfile.steam_id64 == steam_id64)
        )
        return res.scalar_one_or_none()

    async def pending_profiles(self, limit: int = 50) -> list[PlayerProfile]:
        res = await self.session.execute(
            select(PlayerProfile)
            .where(PlayerProfile.verification_status == VerificationStatus.PENDING)
            .order_by(PlayerProfile.updated_at)
            .limit(limit)
        )
        return list(res.scalars().all())

    async def set_status(
        self,
        profile: PlayerProfile,
        status: VerificationStatus,
        *,
        source: str | None = None,
        verified_by: int | None = None,
        admin_comment: str | None = None,
    ) -> PlayerProfile:
        profile.verification_status = status
        if source is not None:
            profile.verification_source = source
        if verified_by is not None:
            profile.verified_by = verified_by
        if admin_comment is not None:
            profile.admin_comment = admin_comment
        if status in (VerificationStatus.VERIFIED, VerificationStatus.REJECTED):
            profile.verified_at = utcnow()
        profile.updated_at = utcnow()
        return profile


# ---------------------------------------------------------------------------
# Rank ranges
# ---------------------------------------------------------------------------

class RankRangeRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_tier(self, rank_tier: int | None) -> RankRange | None:
        if rank_tier is None:
            return None
        return await self.session.scalar(
            select(RankRange).where(RankRange.rank_tier == int(rank_tier))
        )

    async def list_all(self) -> list[RankRange]:
        res = await self.session.execute(
            select(RankRange).order_by(RankRange.rank_tier)
        )
        return list(res.scalars().all())


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------

class TeamRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, team_id: int) -> Team | None:
        return await self.session.get(Team, team_id)

    async def get_by_name(self, name: str) -> Team | None:
        res = await self.session.execute(select(Team).where(Team.name == name))
        return res.scalar_one_or_none()

    async def get_by_captain(self, captain_user_id: int) -> Team | None:
        res = await self.session.execute(
            select(Team).where(Team.captain_user_id == captain_user_id)
        )
        return res.scalar_one_or_none()

    async def get_for_user(self, user_id: int) -> Team | None:
        """Команда, где пользователь — капитан или участник."""
        res = await self.session.execute(
            select(Team)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id == user_id)
            .limit(1)
        )
        team = res.scalar_one_or_none()
        if team is not None:
            return team
        return await self.get_by_captain(user_id)

    async def create(self, name: str, captain_user_id: int) -> Team:
        team = Team(name=name, captain_user_id=captain_user_id)
        self.session.add(team)
        try:
            await self.session.flush()
        except IntegrityError as e:
            await self.session.rollback()
            raise ValueError("Команда с таким названием уже существует.") from e
        member = TeamMember(team_id=team.id, user_id=captain_user_id, role=TeamRole.CAPTAIN)
        self.session.add(member)
        try:
            await self.session.flush()
        except IntegrityError as e:
            await self.session.rollback()
            raise ValueError("Капитан уже состоит в команде.") from e
        logger.info("[TEAM] Team created id=%s name=%s", team.id, team.name)
        return team

    async def add_member(self, team_id: int, user_id: int, role: TeamRole = TeamRole.PLAYER) -> TeamMember:
        team = await self.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        # expire_all() здесь запрещён: он инвалидирует незакоммиченные объекты
        # (например, cap.id до commit), что вызывает lazy IO в async-сессии.
        # Все чтения идут через SELECT, identity map возвращает актуальные данные.
        team = await self.get_by_id(team_id)
        user = await self.session.get(User, user_id)
        if user is None:
            raise ValueError("Пользователь не найден в системе.")
        existing = await self.get_member(team_id, user_id)
        if existing is not None:
            raise ValueError("Игрок уже находится в составе команды.")
        membership = await self.get_membership(user_id)
        if membership is not None:
            raise ValueError("Игрок уже состоит в другой команде.")
        if role == TeamRole.CAPTAIN:
            captain_member = await self.get_member(team_id, team.captain_user_id)
            if captain_member is not None and captain_member.user_id != user_id:
                raise ValueError("В команде уже есть капитан.")
        member = TeamMember(team_id=team_id, user_id=user_id, role=role)
        self.session.add(member)
        try:
            await self.session.flush()
        except IntegrityError as e:
            await self.session.rollback()
            raise ValueError("Не удалось добавить игрока (дубликат).") from e
        logger.info("[TEAM] Member added team=%s user=%s", team_id, user_id)
        return member

    async def remove_member(self, team_id: int, user_id: int) -> bool:
        team = await self.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id == user_id:
            raise ValueError("Нельзя удалить капитана из состава команды.")
        member = await self.get_member(team_id, user_id)
        if member is None:
            return False
        await self.session.delete(member)
        await self.session.flush()
        logger.info("[TEAM] Member removed team=%s user=%s", team_id, user_id)
        return True

    async def get_member(self, team_id: int, user_id: int) -> TeamMember | None:
        res = await self.session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id, TeamMember.user_id == user_id
            )
        )
        return res.scalar_one_or_none()

    async def get_membership(self, user_id: int) -> TeamMember | None:
        res = await self.session.execute(
            select(TeamMember).where(TeamMember.user_id == user_id).limit(1)
        )
        return res.scalar_one_or_none()

    async def get_roster(self, team_id: int) -> list[TeamMember]:
        res = await self.session.execute(
            select(TeamMember).where(TeamMember.team_id == team_id).order_by(TeamMember.id)
        )
        return list(res.scalars().all())

    async def get_players(self, team_id: int) -> list[TeamMember]:
        """Состав без тренера (для лимитов и минимума игроков)."""
        return [m for m in await self.get_roster(team_id) if m.role != TeamRole.COACH]

    async def free_agents(
        self,
        *,
        limit: int = 5,
        offset: int = 0,
    ) -> list[tuple[User, PlayerProfile]]:
        """Свободные подтверждённые игроки с точным MMR."""
        res = await self.session.execute(
            select(User, PlayerProfile)
            .join(PlayerProfile, PlayerProfile.user_id == User.id)
            .outerjoin(TeamMember, TeamMember.user_id == User.id)
            .where(
                TeamMember.id.is_(None),
                PlayerProfile.exact_mmr.is_not(None),
                PlayerProfile.mmr_verified.is_(True),
                PlayerProfile.verification_status == VerificationStatus.VERIFIED,
            )
            .order_by(PlayerProfile.exact_mmr.desc(), User.id)
            .limit(limit)
            .offset(offset)
        )
        return list(res.all())

    async def free_agents_count(self) -> int:
        res = await self.session.execute(
            select(func.count())
            .select_from(User)
            .join(PlayerProfile, PlayerProfile.user_id == User.id)
            .outerjoin(TeamMember, TeamMember.user_id == User.id)
            .where(
                TeamMember.id.is_(None),
                PlayerProfile.exact_mmr.is_not(None),
                PlayerProfile.mmr_verified.is_(True),
                PlayerProfile.verification_status == VerificationStatus.VERIFIED,
            )
        )
        return int(res.scalar_one())

    async def list_teams(self, limit: int = 100, offset: int = 0) -> list[Team]:
        res = await self.session.execute(
            select(Team).order_by(Team.id).limit(limit).offset(offset)
        )
        return list(res.scalars().all())

    async def count(self) -> int:
        res = await self.session.execute(select(func.count()).select_from(Team))
        return int(res.scalar_one())


# ---------------------------------------------------------------------------
# Team invites and free agents
# ---------------------------------------------------------------------------

class TeamInviteRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        team_id: int,
        inviter_user_id: int,
        invitee_user_id: int,
        expires_at: datetime,
    ) -> TeamInvite:
        # Устаревшие pending-инвайты этому игроку закрываем.
        res = await self.session.execute(
            select(TeamInvite).where(
                TeamInvite.invitee_user_id == invitee_user_id,
                TeamInvite.status == TeamInviteStatus.PENDING,
            )
        )
        for old in res.scalars().all():
            old.status = TeamInviteStatus.EXPIRED
            old.decided_at = utcnow()

        invite = TeamInvite(
            team_id=team_id,
            inviter_user_id=inviter_user_id,
            invitee_user_id=invitee_user_id,
            status=TeamInviteStatus.PENDING,
            expires_at=expires_at,
        )
        self.session.add(invite)
        await self.session.flush()
        return invite

    async def get_pending(self, invite_id: int) -> TeamInvite | None:
        invite = await self.session.get(TeamInvite, invite_id)
        if invite is None:
            return None
        if invite.status == TeamInviteStatus.PENDING:
            expires_at = invite.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= utcnow():
                invite.status = TeamInviteStatus.EXPIRED
                invite.decided_at = utcnow()
                await self.session.flush()
                return None
        return invite

    async def decide(self, invite: TeamInvite, status: TeamInviteStatus) -> None:
        invite.status = status
        invite.decided_at = utcnow()
        await self.session.flush()

    async def list_pending_for_user(self, user_id: int, limit: int = 20) -> list[TeamInvite]:
        now = utcnow()
        res = await self.session.execute(
            select(TeamInvite).where(
                TeamInvite.invitee_user_id == user_id,
                TeamInvite.status == TeamInviteStatus.PENDING,
                TeamInvite.expires_at > now,
            ).order_by(TeamInvite.id.desc()).limit(limit)
        )
        return list(res.scalars().all())


# ---------------------------------------------------------------------------
# Tournaments


# ---------------------------------------------------------------------------

class TournamentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, tournament_id: int) -> Tournament | None:
        return await self.session.get(Tournament, tournament_id)

    async def create(
        self,
        name: str,
        description: str | None,
        max_teams: int,
        start_date: datetime | None,
        status=None,
        registration_open: bool = False,
    ) -> Tournament:
        from database.models import TournamentStatus

        t = Tournament(
            name=name,
            description=description,
            max_teams=max_teams,
            start_date=start_date,
            status=status or TournamentStatus.DRAFT,
            registration_open=registration_open,
        )
        self.session.add(t)
        await self.session.flush()
        logger.info("[TOURNAMENT] Created id=%s name=%s", t.id, t.name)
        return t

    async def list_all(self, statuses=None, limit: int = 100) -> list[Tournament]:
        stmt = select(Tournament).order_by(Tournament.id.desc()).limit(limit)
        if statuses:
            stmt = stmt.where(Tournament.status.in_(statuses))
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def list_public(self) -> list[Tournament]:
        """Все турниры, кроме скрытых черновиков."""
        from database.models import TournamentStatus

        res = await self.session.execute(
            select(Tournament)
            .where(Tournament.status != TournamentStatus.DRAFT)
            .order_by(Tournament.id.desc())
        )
        return list(res.scalars().all())

    async def update(self, tournament: Tournament) -> Tournament:
        tournament.updated_at = utcnow()
        await self.session.flush()
        return tournament

    async def approved_count(self, tournament_id: int) -> int:
        res = await self.session.execute(
            select(func.count())
            .select_from(TournamentApplication)
            .where(
                TournamentApplication.tournament_id == tournament_id,
                TournamentApplication.status == ApplicationStatus.APPROVED,
            )
        )
        return int(res.scalar_one())

    async def count(self) -> int:
        res = await self.session.execute(select(func.count()).select_from(Tournament))
        return int(res.scalar_one())


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

class ApplicationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, app_id: int) -> TournamentApplication | None:
        return await self.session.get(TournamentApplication, app_id)

    async def get_for_team(self, team_id: int) -> list[TournamentApplication]:
        res = await self.session.execute(
            select(TournamentApplication)
            .where(TournamentApplication.team_id == team_id)
            .order_by(TournamentApplication.id.desc())
        )
        return list(res.scalars().all())

    async def get_active_for_team(self, team_id: int) -> TournamentApplication | None:
        res = await self.session.execute(
            select(TournamentApplication)
            .where(
                TournamentApplication.team_id == team_id,
                TournamentApplication.status.in_(
                    [ApplicationStatus.DRAFT, ApplicationStatus.PENDING]
                ),
            )
            .limit(1)
        )
        return res.scalar_one_or_none()

    async def exists(self, tournament_id: int, team_id: int) -> bool:
        res = await self.session.execute(
            select(TournamentApplication.id).where(
                TournamentApplication.tournament_id == tournament_id,
                TournamentApplication.team_id == team_id,
                TournamentApplication.status != ApplicationStatus.CANCELLED,
            )
        )
        return res.first() is not None

    async def create(
        self, tournament_id: int, team_id: int, created_by: int
    ) -> TournamentApplication:
        app = TournamentApplication(
            tournament_id=tournament_id,
            team_id=team_id,
            created_by=created_by,
            status=ApplicationStatus.PENDING,
        )
        self.session.add(app)
        try:
            await self.session.flush()
        except IntegrityError as e:
            await self.session.rollback()
            raise ValueError("Заявка от этой команды уже существует.") from e
        logger.info(
            "[APPLICATION] Created id=%s tournament=%s team=%s",
            app.id, tournament_id, team_id,
        )
        return app

    async def list_pending(self, limit: int = 100) -> list[TournamentApplication]:
        res = await self.session.execute(
            select(TournamentApplication)
            .where(TournamentApplication.status == ApplicationStatus.PENDING)
            .order_by(TournamentApplication.id)
            .limit(limit)
        )
        return list(res.scalars().all())

    async def list_all(self, limit: int = 100) -> list[TournamentApplication]:
        res = await self.session.execute(
            select(TournamentApplication).order_by(TournamentApplication.id.desc()).limit(limit)
        )
        return list(res.scalars().all())

    async def decide(
        self,
        app: TournamentApplication,
        status: ApplicationStatus,
        admin_telegram_id: int,
        comment: str | None = None,
    ) -> TournamentApplication:
        app.status = status
        app.decided_by = admin_telegram_id
        app.decided_at = utcnow()
        if comment:
            app.admin_comment = comment
        app.updated_at = utcnow()
        await self.session.flush()
        logger.info("[APPLICATION] Decided id=%s status=%s", app.id, status.value)
        return app

    async def count(self) -> int:
        res = await self.session.execute(
            select(func.count()).select_from(TournamentApplication)
        )
        return int(res.scalar_one())


# ---------------------------------------------------------------------------
# Support
# ---------------------------------------------------------------------------

class SupportRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_ticket(self, user_id: int, message: str) -> SupportTicket:
        ticket = SupportTicket(user_id=user_id, message=message)
        self.session.add(ticket)
        await self.session.flush()
        logger.info("[SUPPORT] Ticket created id=%s user=%s", ticket.id, user_id)
        return ticket

    async def get_by_id(self, ticket_id: int) -> SupportTicket | None:
        return await self.session.get(SupportTicket, ticket_id)

    async def list_open(self, limit: int = 50) -> list[SupportTicket]:
        from database.models import TicketStatus

        res = await self.session.execute(
            select(SupportTicket)
            .where(SupportTicket.status == TicketStatus.OPEN)
            .order_by(SupportTicket.id.desc())
            .limit(limit)
        )
        return list(res.scalars().all())

    async def answer(
        self, ticket: SupportTicket, reply: str, admin_telegram_id: int
    ) -> SupportTicket:
        from database.models import TicketStatus

        ticket.admin_reply = reply
        ticket.answered_by = admin_telegram_id
        ticket.status = TicketStatus.ANSWERED
        ticket.updated_at = utcnow()
        await self.session.flush()
        return ticket


# ---------------------------------------------------------------------------
# Verification records
# ---------------------------------------------------------------------------

class VerificationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(
        self,
        user_id: int,
        status: VerificationStatus,
        source: str | None = None,
        performed_by: int | None = None,
        comment: str | None = None,
        profile_id: int | None = None,
    ) -> VerificationRecord:
        rec = VerificationRecord(
            user_id=user_id,
            profile_id=profile_id,
            status=status,
            source=source,
            performed_by=performed_by,
            comment=comment,
        )
        self.session.add(rec)
        await self.session.flush()
        logger.info(
            "[VERIFICATION] Recorded user=%s status=%s source=%s",
            user_id, status.value, source,
        )
        return rec

    async def history(self, user_id: int, limit: int = 20) -> list[VerificationRecord]:
        res = await self.session.execute(
            select(VerificationRecord)
            .where(VerificationRecord.user_id == user_id)
            .order_by(VerificationRecord.id.desc())
            .limit(limit)
        )
        return list(res.scalars().all())
