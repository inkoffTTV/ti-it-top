"""Модели SQLAlchemy 2.x (async) для TI:IT TOP."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Перечисления
# ---------------------------------------------------------------------------

class VerificationStatus(str, enum.Enum):
    UNVERIFIED = "UNVERIFIED"
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class TeamRole(str, enum.Enum):
    CAPTAIN = "CAPTAIN"
    PLAYER = "PLAYER"
    COACH = "COACH"  # 5.5: тренер — не игрок: вне лимита игроков, минимума для заявки и среднего MMR


class TeamInviteStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


class TournamentStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    REGISTRATION = "REGISTRATION"
    ACTIVE = "ACTIVE"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"


class ApplicationStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class TicketStatus(str, enum.Enum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    CLOSED = "CLOSED"


# ---------------------------------------------------------------------------
# Таблицы
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    profile: Mapped["PlayerProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    captain_of: Mapped[list["Team"]] = relationship(
        back_populates="captain", foreign_keys="Team.captain_user_id"
    )
    memberships: Mapped[list["TeamMember"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class PlayerProfile(Base):
    __tablename__ = "player_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    steam_id64: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    steam_nickname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus), default=VerificationStatus.UNVERIFIED
    )
    verification_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    admin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    mmr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    matches: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winrate: Mapped[float | None] = mapped_column(Float, nullable=True)
    hours_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    medal_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leaderboard_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exact_mmr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_mmr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mmr_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mmr_verified_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mmr_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preferred_positions: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="profile")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    captain_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    avatar_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_file_type: Mapped[str] = mapped_column(String(16), default="photo", nullable=False)
    role_mode: Mapped[str] = mapped_column(String(32), default="PLAYER_CHOICE", nullable=False)
    # Mini App 5.0: описание команды. deferred=True — колонка не входит в
    # обычные SELECT бота, поэтому старый код не зависит от её наличия.
    description: Mapped[str | None] = mapped_column(Text, nullable=True, deferred=True)

    captain: Mapped[User] = relationship(back_populates="captain_of", foreign_keys=[captain_user_id])
    members: Mapped[list["TeamMember"]] = relationship(
        back_populates="team", cascade="all, delete-orphan", order_by="TeamMember.id"
    )


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_team_user"),
        UniqueConstraint("user_id", name="uq_team_member_user"),
        CheckConstraint("position IS NULL OR position BETWEEN 1 AND 5", name="ck_team_member_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[TeamRole] = mapped_column(Enum(TeamRole), default=TeamRole.PLAYER)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class RankRange(Base):
    """Допустимый диапазон точного MMR для каждого rank_tier OpenDota."""

    __tablename__ = "rank_ranges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rank_tier: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    rank_name: Mapped[str] = mapped_column(String(64))
    star: Mapped[int] = mapped_column(Integer, default=0)
    mmr_min: Mapped[int] = mapped_column(Integer)
    mmr_max: Mapped[int] = mapped_column(Integer)


class TeamInvite(Base):
    """Приглашение игрока в команду, действующее ограниченное время."""

    __tablename__ = "team_invites"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_team_invite_expiry"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    inviter_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    invitee_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[TeamInviteStatus] = mapped_column(Enum(TeamInviteStatus), default=TeamInviteStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    team: Mapped["Team"] = relationship()
    inviter: Mapped["User"] = relationship(foreign_keys=[inviter_user_id])
    invitee: Mapped["User"] = relationship(foreign_keys=[invitee_user_id])


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TournamentStatus] = mapped_column(
        Enum(TournamentStatus), default=TournamentStatus.DRAFT
    )
    max_teams: Mapped[int] = mapped_column(Integer, default=16)
    registration_open: Mapped[bool] = mapped_column(Boolean, default=False)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    applications: Mapped[list["TournamentApplication"]] = relationship(
        back_populates="tournament", cascade="all, delete-orphan"
    )


class TournamentApplication(Base):
    __tablename__ = "tournament_applications"
    __table_args__ = (
        UniqueConstraint("tournament_id", "team_id", name="uq_tournament_team"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE")
    )
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus), default=ApplicationStatus.PENDING
    )
    admin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    tournament: Mapped[Tournament] = relationship(back_populates="applications")
    team: Mapped[Team] = relationship()


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus), default=TicketStatus.OPEN
    )
    admin_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship()


class BroadcastLog(Base):
    """История административных рассылок."""

    __tablename__ = "broadcast_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(Integer, index=True)
    content_type: Mapped[str] = mapped_column(String(64), default="сообщение")
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminUser(Base):
    """Постоянный список администраторов Mini App/бота.

    ADMIN_IDS из .env используется как bootstrap; добавленные через админку
    записи сохраняются в БД и подхватываются после перезапуска.
    """

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    added_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMessage(Base):
    """Общий чат Mini App."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(1000))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user: Mapped[User] = relationship()


class ActivityEvent(Base):
    """Публичная лента событий турнира/сообщества."""

    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Notification(Base):
    """Персональные уведомления пользователя."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64), default="system")
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user: Mapped[User] = relationship()


class PlayerCheck(Base):
    """Снимок проверки игрока (Mini App 5.1): медаль по источникам + риск смурфа/твинка.

    Хранит только реально полученные данные источников и вывод. AI-вердикт —
    рекомендация для организатора, решения по допуску он не принимает.
    """

    __tablename__ = "player_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="admin")
    sources_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    medal_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    medal_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    medal_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk_reasons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    features_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ai_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class VerificationRecord(Base):
    """Журнал всех проверок игроков (автоматических и ручных)."""

    __tablename__ = "verification_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("player_profiles.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[VerificationStatus] = mapped_column(Enum(VerificationStatus))
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    performed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship()


class UserBan(Base):
    """5.7: полная блокировка доступа к боту и Mini App (новая таблица, существующие не меняются)."""

    __tablename__ = "user_bans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    telegram_id: Mapped[int] = mapped_column(Integer, index=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    banned_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatchReport(Base):
    """5.8: результат от капитана: счёт + скриншот → подтверждение соперника → одобрение админа."""

    __tablename__ = "match_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(Integer, index=True)
    team_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    score1: Mapped[int] = mapped_column(Integer)
    score2: Mapped[int] = mapped_column(Integer)
    screenshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)  # PENDING/CONFIRMED/DISPUTED/APPROVED/REJECTED
    opponent_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserKind(Base):
    """5.9: кто пользователь — игрок или зритель (выбор при первом входе)."""

    __tablename__ = "user_kinds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="PLAYER")  # PLAYER | VIEWER
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TeamFollow(Base):
    """5.9: «болею за команду» — уведомления о её матчах и результатах."""

    __tablename__ = "team_follows"
    __table_args__ = (UniqueConstraint("user_id", "team_id", name="uq_team_follow"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    team_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatchChatMessage(Base):
    """5.9: закрытый чат матча (игроки двух команд + админы), открывается за час до начала."""

    __tablename__ = "match_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(String(1000))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AudienceVote(Base):
    """5.9.1: приз зрительских симпатий — голосуют все (один голос с аккаунта), за себя нельзя."""

    __tablename__ = "audience_votes"
    __table_args__ = (UniqueConstraint("tournament_id", "voter_user_id", name="uq_audience_vote"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(Integer, index=True)
    voter_user_id: Mapped[int] = mapped_column(Integer)
    candidate_user_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MvpVote(Base):
    """5.8: голос за MVP турнира: один голос от игрока на турнир, за себя нельзя."""

    __tablename__ = "mvp_votes"
    __table_args__ = (UniqueConstraint("tournament_id", "voter_user_id", name="uq_mvp_vote"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(Integer, index=True)
    voter_user_id: Mapped[int] = mapped_column(Integer)
    candidate_user_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Турнирный движок (Mini App 5.2): этапы, участники, матчи-серии, игры, журнал.
# Только новые таблицы; существующие не меняются. Время — UTC (как во всём проекте).
# Ссылки на команды/матчи — обычные int без FK: удаление команды не должно
# каскадно стирать историю матчей (в выдаче покажется «Команда удалена»).
# ---------------------------------------------------------------------------
class TournamentStage(Base):
    __tablename__ = "tournament_stages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id", ondelete="CASCADE"), index=True)
    order_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    format: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    settings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class StageParticipant(Base):
    __tablename__ = "stage_participants"
    __table_args__ = (UniqueConstraint("stage_id", "team_id", name="uq_stage_participant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("tournament_stages.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(Integer, index=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    final_place: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TournamentMatch(Base):
    """Матч = серия (BO1/BO3/BO5) между двумя командами."""

    __tablename__ = "tournament_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(Integer, index=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("tournament_stages.id", ondelete="CASCADE"), index=True)
    plan_key: Mapped[str] = mapped_column(String(32))
    bracket: Mapped[str] = mapped_column(String(16), default="main", nullable=False)
    round_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    match_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    round_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    team1_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    team2_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    best_of: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    score1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner_team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="WAITING", nullable=False, index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    winner_to_match_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner_to_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loser_to_match_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loser_to_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TournamentMatchGame(Base):
    """Отдельная игра серии (необязательная детализация, задел под автопроверку)."""

    __tablename__ = "tournament_match_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("tournament_matches.id", ondelete="CASCADE"), index=True)
    game_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    winner_team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dota_match_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TournamentMatchLog(Base):
    """Журнал правок матчей: кто, что и когда поменял."""

    __tablename__ = "tournament_match_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(Integer, index=True)
    stage_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(32))
    old_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RosterRequest(Base):
    """5.13: после закрытия регистрации состав меняется только с согласия организатора."""

    __tablename__ = "roster_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(Integer, index=True)
    tournament_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(8))  # ADD | REMOVE | LEAVE
    target_user_id: Mapped[int] = mapped_column(Integer, index=True)
    requested_by: Mapped[int] = mapped_column(Integer)
    invite_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)  # PENDING/APPROVED/REJECTED/CANCELLED
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MatchReportDota(Base):
    """5.13: результат капитана по ID матчей Dota — данные OpenDota (победитель, длительность, герои)."""

    __tablename__ = "match_report_dota"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    data_json: Mapped[str] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Prediction(Base):
    """5.13: прогноз зрителя/игрока на матч (один на матч), таблица лучших предсказателей."""

    __tablename__ = "predictions"
    __table_args__ = (UniqueConstraint("match_id", "user_id", name="uq_prediction_match_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(Integer, index=True)
    tournament_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    team_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TeamArchive(Base):
    """5.13: названия расформированных команд — чтобы в истории матчей не было «Команда удалена»."""

    __tablename__ = "team_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    disbanded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
