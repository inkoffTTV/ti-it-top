"""Бизнес-логика команд: составы, инвайты, роли и аватарки."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    Team,
    TeamInvite,
    TeamInviteStatus,
    TeamMember,
    TeamRole,
    User,
    VerificationStatus,
    ApplicationStatus,
    utcnow,
)
from database.repositories import (
    ProfileRepository,
    TeamInviteRepository,
    ApplicationRepository,
    TeamRepository,
    UserRepository,
)

logger = logging.getLogger("TEAM")


async def roster_guard(session, team_id, kind, target_user_id, requested_by, *, invite=None) -> None:
    """5.13: после закрытия регистрации изменение состава уходит организаторам на одобрение (RosterFrozen)."""
    from services.roster import guard

    await guard(session, team_id, kind, target_user_id, requested_by, invite=invite)

MAX_ROSTER_SIZE = 6  # 5 основных + 1 запасной
ROLE_MODES = {"PLAYER_CHOICE", "CAPTAIN_CHOICE"}
POSITIONS = {
    1: "Керри",
    2: "Мид",
    3: "Хард",
    4: "Четверка",
    5: "Пятерка",
}


class TeamService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.teams = TeamRepository(session)
        self.users = UserRepository(session)
        self.profiles = ProfileRepository(session)
        self.invites = TeamInviteRepository(session)
        self.applications = ApplicationRepository(session)

    async def create_team(self, captain_user_id: int, raw_name: str) -> Team:
        from utils.validation import validate_team_name

        ok, name, error = validate_team_name(raw_name)
        if not ok:
            raise ValueError(error or "Некорректное название команды.")
        if await self.teams.get_by_name(name) is not None:
            raise ValueError("Команда с таким названием уже существует.")
        if await self.teams.get_by_captain(captain_user_id) is not None:
            raise ValueError("Вы уже капитан команды. Используйте «Моя команда».")
        if await self.teams.get_membership(captain_user_id) is not None:
            raise ValueError(
                "Вы уже состоите в команде. Попросите капитана текущей команды удалить вас."
            )
        team = await self.teams.create(name=name, captain_user_id=captain_user_id)
        await self.session.commit()
        return team

    async def resolve_player(self, query: str) -> User | None:
        q = (query or "").strip()
        if not q:
            return None
        if q.lstrip("-").isdigit():
            user = await self.users.get_by_telegram_id(int(q))
            if user is not None:
                return user
        if q.startswith("@") or q.isalnum() or "_" in q:
            return await self.users.get_by_username(q)
        return None

    async def add_player(self, team_id: int, caller_user_id: int, player_query: str) -> TeamMember:
        team = await self.teams.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id != caller_user_id:
            raise PermissionError("Только капитан может управлять составом.")
        if len(await self.teams.get_players(team_id)) >= MAX_ROSTER_SIZE:
            raise ValueError(f"Максимальный размер состава — {MAX_ROSTER_SIZE} игроков.")
        user = await self.resolve_player(player_query)
        if user is None:
            raise ValueError(
                "Игрок не найден. Он должен ранее нажать /start и иметь регистрацию."
            )
        if await self.teams.get_membership(user.id) is not None:
            raise ValueError("Этот игрок уже состоит в команде.")
        await roster_guard(self.session, team_id, "ADD", user.id, caller_user_id)
        member = await self.teams.add_member(team_id, user.id, role=TeamRole.PLAYER)
        await self.session.commit()
        return member

    async def remove_player(self, team_id: int, caller_user_id: int, player_query: str) -> bool:
        team = await self.teams.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id != caller_user_id:
            raise PermissionError("Только капитан может управлять составом.")
        user = await self.resolve_player(player_query)
        if user is None:
            raise ValueError("Игрок не найден в системе.")
        if await self.teams.get_member(team_id, user.id) is not None:
            await roster_guard(self.session, team_id, "REMOVE", user.id, caller_user_id)
        removed = await self.teams.remove_member(team_id, user.id)
        await self.session.commit()
        if not removed:
            raise ValueError("Этого игрока нет в составе команды.")
        return True

    async def delete_team(self, team_id: int, captain_user_id: int) -> list[int]:
        """Полностью расформировывает команду, если она ещё не принята в турнир.

        Удалять команду с APPROVED-заявкой запрещено, чтобы не уничтожать
        сведения о команде, реально допущенной к турниру. DRAFT/PENDING и
        старые REJECTED/CANCELLED заявки удаляются вместе с командой.
        Возвращает Telegram/DB user ids бывших участников для уведомлений.
        """
        team = await self.teams.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id != captain_user_id:
            raise PermissionError("Только капитан может расформировать команду.")

        applications = await self.applications.get_for_team(team_id)
        approved = [a for a in applications if a.status == ApplicationStatus.APPROVED]
        if approved:
            raise ValueError(
                "Команду нельзя удалить: она уже допущена к турниру. "
                "Обратитесь к организатору."
            )

        roster = await self.teams.get_roster(team_id)
        member_ids = [m.user_id for m in roster]

        # Явно удаляем заявки/инвайты до команды: это надёжно работает даже
        # в SQLite-конфигурациях, где foreign_keys мог быть отключён.
        for app in applications:
            await self.session.delete(app)
        invites = (
            await self.session.execute(
                select(TeamInvite).where(TeamInvite.team_id == team_id)
            )
        ).scalars().all()
        for invite in invites:
            await self.session.delete(invite)

        await self.session.delete(team)
        await self.session.flush()

        # Локальный файл аватара удаляется отдельно от БД.
        try:
            avatar_path = (
                __import__("pathlib", fromlist=["Path"]).Path(__file__).resolve().parent.parent
                / "miniapp" / "data" / "team_avatars" / f"team_{team_id}.jpg"
            )
            avatar_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("[TEAM] Failed to remove local avatar team=%s", team_id)

        logger.info("[TEAM] Team deleted id=%s captain=%s members=%s", team_id, captain_user_id, len(member_ids))
        return member_ids

    async def set_position(
        self,
        team_id: int,
        target_user_id: int,
        position: int,
        caller_user_id: int,
    ) -> None:
        if position not in POSITIONS:
            raise ValueError("Позиция должна быть от 1 до 5.")
        team = await self.teams.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        member = await self.teams.get_member(team_id, target_user_id)
        if member is None:
            raise ValueError("Игрок не состоит в этой команде.")

        if team.role_mode == "CAPTAIN_CHOICE":
            if team.captain_user_id != caller_user_id:
                raise PermissionError("В этом режиме позиции назначает только капитан.")
        else:
            if target_user_id != caller_user_id:
                raise PermissionError("В этом режиме игрок может менять только свою позицию.")

        # Одна позиция — один игрок в составе. Капитан также считается позицией.
        existing = await self.session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.position == position,
                TeamMember.user_id != target_user_id,
            )
        )
        other = existing.scalars().first()
        if other is not None:
            raise ValueError(f"Позиция {position} уже назначена другому игроку.")

        member.position = position
        await self.session.commit()

    async def set_role_mode(self, team_id: int, captain_user_id: int, mode: str) -> None:
        if mode not in ROLE_MODES:
            raise ValueError("Неизвестный режим ролей.")
        team = await self.teams.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id != captain_user_id:
            raise PermissionError("Только капитан может менять режим ролей.")
        team.role_mode = mode
        await self.session.commit()

    async def set_avatar(self, team_id: int, captain_user_id: int, file_id: str | None, file_type: str = "photo") -> None:
        team = await self.teams.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id != captain_user_id:
            raise PermissionError("Только капитан может менять аватарку.")
        team.avatar_file_id = file_id
        team.avatar_file_type = "document" if file_type == "document" else ("photo" if file_type == "photo" else "local")
        await self.session.commit()

    async def set_preferred_positions(
        self,
        user_id: int,
        positions: list[int],
    ) -> None:
        clean = sorted({p for p in positions if p in POSITIONS})
        profile = await self.profiles.get_or_create(user_id)
        profile.preferred_positions = ",".join(map(str, clean)) if clean else None
        await self.session.commit()

    async def create_invite(
        self,
        team_id: int,
        captain_user_id: int,
        target_user_id: int,
    ):
        team = await self.teams.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id != captain_user_id:
            raise PermissionError("Только капитан может приглашать игроков.")
        if len(await self.teams.get_players(team_id)) >= MAX_ROSTER_SIZE:
            raise ValueError(f"В команде уже {MAX_ROSTER_SIZE} игроков.")
        if target_user_id == captain_user_id:
            raise ValueError("Нельзя пригласить самого себя.")
        if await self.teams.get_membership(target_user_id) is not None:
            raise ValueError("Игрок уже состоит в команде.")

        profile = await self.profiles.get_by_user_id(target_user_id)
        if profile is None or profile.exact_mmr is None or not profile.mmr_verified:
            raise ValueError("Игрок ещё не прошёл проверку точного MMR.")

        return await self.invites.create(
            team_id=team_id,
            inviter_user_id=captain_user_id,
            invitee_user_id=target_user_id,
            expires_at=utcnow() + timedelta(hours=24),
        )

    async def accept_invite(self, invite_id: int, user_id: int) -> Team:
        invite = await self.invites.get_pending(invite_id)
        if invite is None:
            raise ValueError("Приглашение не найдено или уже истекло.")
        if invite.invitee_user_id != user_id:
            raise PermissionError("Это приглашение адресовано другому игроку.")

        if await self.teams.get_membership(user_id) is not None:
            raise ValueError("Вы уже состоите в команде.")

        team = await self.teams.get_by_id(invite.team_id)
        if team is None:
            raise ValueError("Команда больше не существует.")
        if len(await self.teams.get_players(team.id)) >= MAX_ROSTER_SIZE:
            raise ValueError("В команде больше нет свободных мест.")

        profile = await self.profiles.get_by_user_id(user_id)
        if profile is None or profile.exact_mmr is None or not profile.mmr_verified:
            raise ValueError("Ваш точный MMR ещё не подтверждён.")
        await roster_guard(self.session, team.id, "ADD", user_id, user_id, invite=invite)

        member = TeamMember(team_id=team.id, user_id=user_id, role=TeamRole.PLAYER)
        self.session.add(member)
        await self.invites.decide(invite, TeamInviteStatus.ACCEPTED)
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise ValueError("Не удалось принять приглашение: состав уже изменился.")
        return team

    async def decline_invite(self, invite_id: int, user_id: int) -> None:
        invite = await self.invites.get_pending(invite_id)
        if invite is None:
            raise ValueError("Приглашение не найдено или уже истекло.")
        if invite.invitee_user_id != user_id:
            raise PermissionError("Это приглашение адресовано другому игроку.")
        await self.invites.decide(invite, TeamInviteStatus.DECLINED)
        await self.session.commit()

    # ------------------------------------------------------------------
    # Mini App 5.0: выход, исключение, отмена приглашения, редактирование.
    # Новые методы; существующее поведение бота не меняется.
    # ------------------------------------------------------------------

    async def _team_has_approved_application(self, team_id: int) -> bool:
        applications = await self.applications.get_for_team(team_id)
        return any(a.status == ApplicationStatus.APPROVED for a in applications)

    async def leave_team(self, user_id: int) -> Team:
        """Игрок (не капитан) покидает команду."""
        membership = await self.teams.get_membership(user_id)
        if membership is None:
            raise ValueError("Вы не состоите в команде.")
        team = await self.teams.get_by_id(membership.team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id == user_id:
            raise ValueError(
                "Капитан не может покинуть команду. Расформируйте её или обратитесь к организатору."
            )
        await roster_guard(self.session, team.id, "LEAVE", user_id, user_id)
        await self.session.delete(membership)
        await self.session.commit()
        logger.info("[TEAM] User %s left team %s", user_id, team.id)
        return team

    async def kick_member(self, team_id: int, captain_user_id: int, target_user_id: int) -> None:
        """Капитан исключает игрока из состава (как кнопка team:kick в боте)."""
        team = await self.teams.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id != captain_user_id:
            raise PermissionError("Только капитан может исключать игроков.")
        if target_user_id == captain_user_id:
            raise ValueError("Капитан не может исключить сам себя.")
        if await self.teams.get_member(team_id, target_user_id) is not None:
            await roster_guard(self.session, team_id, "REMOVE", target_user_id, captain_user_id)
        removed = await self.teams.remove_member(team_id, target_user_id)
        if not removed:
            raise ValueError("Этого игрока нет в составе команды.")
        await self.session.commit()

    async def cancel_invite(self, invite_id: int, captain_user_id: int) -> None:
        """Капитан отзывает ещё не принятое приглашение."""
        invite = await self.invites.get_pending(invite_id)
        if invite is None:
            raise ValueError("Приглашение не найдено или уже неактивно.")
        team = await self.teams.get_by_id(invite.team_id)
        if team is None or team.captain_user_id != captain_user_id:
            raise PermissionError("Отозвать приглашение может только капитан команды.")
        await self.invites.decide(invite, TeamInviteStatus.EXPIRED)
        await self.session.commit()

    async def update_info(
        self,
        team_id: int,
        captain_user_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Team:
        """Капитан меняет название и/или описание команды."""
        import re

        from utils.validation import validate_team_name

        team = await self.teams.get_by_id(team_id)
        if team is None:
            raise ValueError("Команда не найдена.")
        if team.captain_user_id != captain_user_id:
            raise PermissionError("Только капитан может редактировать команду.")
        if name is not None:
            ok, clean, error = validate_team_name(name)
            if not ok:
                raise ValueError(error or "Некорректное название команды.")
            if clean != team.name:
                if await self._team_has_approved_application(team_id):
                    raise ValueError(
                        "Команда уже допущена к турниру — название меняет только организатор."
                    )
                existing = await self.teams.get_by_name(clean)
                if existing is not None and existing.id != team.id:
                    raise ValueError("Команда с таким названием уже существует.")
                team.name = clean
        if description is not None:
            lines = [re.sub(r"[ \t]+", " ", line).strip() for line in description.replace("\r\n", "\n").split("\n")]
            text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
            if len(text) > 500:
                raise ValueError("Описание слишком длинное (максимум 500 символов).")
            team.description = text or None
        team.updated_at = utcnow()
        await self.session.commit()
        return team

    async def roster_with_status(
        self,
        team_id: int,
    ) -> list[tuple[User, TeamMember, VerificationStatus, object | None]]:
        members = await self.teams.get_roster(team_id)
        out = []
        for m in members:
            user = await self.users.get_by_id(m.user_id)
            profile = await self.profiles.get_by_user_id(m.user_id)
            status = profile.verification_status if profile else VerificationStatus.UNVERIFIED
            if user is not None:
                out.append((user, m, status, profile))
        return out

    @staticmethod
    def positions_text(profile) -> str:
        raw = (profile.preferred_positions if profile else None) or ""
        vals = [int(x) for x in raw.split(",") if x.isdigit() and int(x) in POSITIONS]
        return ", ".join(str(x) for x in vals) if vals else "не выбраны"
