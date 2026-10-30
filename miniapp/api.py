import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import String, func, or_, select

from config import Config, is_admin
from database.engine import session_scope
from database.models import (
    ApplicationStatus,
    PlayerProfile,
    RankRange,
    TeamInvite,
    TeamInviteStatus,
    TeamMember,
    Tournament,
    TournamentApplication,
    Team,
    TournamentStatus,
    User,
    VerificationStatus,
    TicketStatus,
    SupportTicket,
    BroadcastLog,
    AdminUser, ChatMessage, ActivityEvent, Notification,
)
from database.repositories import ProfileRepository, TeamInviteRepository, TeamRepository, UserRepository
from miniapp.auth import TelegramUser, validate_init_data
from services.applications import ApplicationService
from services.teams import MAX_ROSTER_SIZE, POSITIONS, ROLE_MODES, TeamService
from services.tournaments import TournamentService
from services.bracket import get_admin_state as get_bracket_admin_state, generate as generate_bracket, lock_existing as lock_bracket, public_state as get_bracket_public_state
from services.registration import RegistrationService
from services.support import SupportService
from services.activity import add_event, unread_count
from services.ai_assistant import ask_ai_agent, TOOL_DEFINITIONS
from services.factory import get_services
from database.repositories import ApplicationRepository, TournamentRepository
from utils.ranks import rank_info_from_mmr, rank_info_from_tier, rank_label_from_mmr, rank_label_from_tier


@dataclass(slots=True)
class MiniAppContext:
    config: Config


class ChatBody(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class AIAskBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=8)


# Локальное зеркало аватарок Mini App. Telegram file_id остаётся дополнительным
# источником для бота, но веб-версия больше не зависит от доступности Bot API.
TEAM_AVATAR_DIR = Path(__file__).resolve().parent / "data" / "team_avatars"
TEAM_AVATAR_MAX_SIDE = 512


def _local_avatar_path(team_id: int) -> Path:
    return TEAM_AVATAR_DIR / f"team_{int(team_id)}.jpg"


def _has_team_avatar(team) -> bool:
    return bool(team.avatar_file_id) or _local_avatar_path(team.id).is_file()


def _new_telegram_bot(config: Config):
    """Создаёт отдельную Bot-сессию с тем же HTTP proxy, что и проект.

    Раньше Mini App создавал Bot(token=...) без proxy, поэтому Telegram API мог
    работать из polling-части бота, но ломаться именно в загрузке аватара/рассылке.
    """
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession

    proxy = (config.http_proxy_url or "").strip() or None
    session = AiohttpSession(proxy=proxy)
    return Bot(token=config.bot_token, session=session)


def _prepare_avatar(raw: bytes) -> bytes:
    """Проверяет изображение и нормализует его в JPEG <= 512 px по длинной стороне."""
    from io import BytesIO
    from PIL import Image, ImageOps

    source = BytesIO(raw)
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            bg = Image.new("RGB", rgba.size, "black")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            image = bg
        else:
            image = image.convert("RGB")
        image.thumbnail((TEAM_AVATAR_MAX_SIDE, TEAM_AVATAR_MAX_SIDE), Image.Resampling.LANCZOS)
        out = BytesIO()
        image.save(out, format="JPEG", quality=92, optimize=True, progressive=True)
        return out.getvalue()



def _ctx() -> MiniAppContext:
    raise RuntimeError("Mini App context is not configured")


def _dev_auth_user_id(x_telegram_init_data: str | None) -> int | None:
    """Dev-вход без подписи Telegram — только при явном MINIAPP_ALLOW_DEV_AUTH=1.

    Раньше одного MINIAPP_DEV_USER_ID в .env хватало, чтобы любой запрос с
    заголовком «dev:…» из интернета выполнялся от имени этого пользователя
    (в т.ч. администратора). Теперь нужен отдельный флаг окружения, который
    в production не задаётся.
    """
    if os.getenv("MINIAPP_ALLOW_DEV_AUTH", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    dev_id = os.getenv("MINIAPP_DEV_USER_ID", "").strip()
    if not dev_id or not (x_telegram_init_data or "").startswith("dev:"):
        return None
    try:
        return int(dev_id)
    except ValueError:
        return None


def _rank_label(profile: PlayerProfile | None) -> str | None:
    """Ранг Mini App определяется только по MMR, подтверждённому организатором."""
    if not profile or profile.approved_mmr is None:
        return None
    return rank_label_from_mmr(profile.approved_mmr)


def _rank_payload(profile: PlayerProfile | None) -> dict:
    if not profile or profile.approved_mmr is None:
        return {
            "rank_name": None,
            "rank_tier": None,
            "rank_estimated_tier": None,
            "rank_estimated": False,
            "rank_estimate_name": None,
            "leaderboard_rank": profile.leaderboard_rank if profile else None,
        }
    info = rank_info_from_mmr(profile.approved_mmr)
    return {
        "rank_name": rank_label_from_mmr(profile.approved_mmr),
        "rank_tier": info.rank_tier if info else None,
        "rank_estimated_tier": info.rank_tier if info else None,
        "rank_estimated": True if info else False,
        "rank_estimate_name": rank_label_from_mmr(profile.approved_mmr),
        "leaderboard_rank": profile.leaderboard_rank,
    }


def _positions(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(x) for x in raw.split(",") if x.isdigit() and int(x) in POSITIONS]


async def current_user(x_telegram_init_data: str | None = Header(default=None), context: MiniAppContext = Depends(_ctx)) -> tuple[TelegramUser, User]:
    try:
        tg = validate_init_data(x_telegram_init_data or "", context.config.bot_token)
    except ValueError:
        dev_id = _dev_auth_user_id(x_telegram_init_data)
        if dev_id is None:
            raise HTTPException(status_code=401, detail="Telegram session is not valid")
        tg = TelegramUser(id=dev_id, username="dev")

    async with session_scope() as session:
        repo = UserRepository(session)
        user = await repo.upsert_from_telegram(tg.id, tg.username, tg.first_name, tg.last_name)
        await session.commit()
        return tg, user


def build_router(context: MiniAppContext) -> APIRouter:
    router = APIRouter(prefix="/api")

    async def auth(x_telegram_init_data: str | None = Header(default=None)):
        try:
            tg = validate_init_data(x_telegram_init_data or "", context.config.bot_token)
        except ValueError:
            dev_id = _dev_auth_user_id(x_telegram_init_data)
            if dev_id is None:
                raise HTTPException(status_code=401, detail="Telegram session is not valid")
            tg = TelegramUser(id=dev_id, username="dev")
        from services.bans import BLOCKED_TEXT, is_banned

        if not is_admin(tg.id, context.config) and await is_banned(tg.id):
            raise HTTPException(status_code=403, detail=BLOCKED_TEXT)
        async with session_scope() as session:
            repo = UserRepository(session)
            user = await repo.upsert_from_telegram(tg.id, tg.username, tg.first_name, tg.last_name)
            await session.commit()
            return tg, user

    class PositionBody(BaseModel):
        # Accept the normal JSON shape {"position": 1}; also tolerate
        # {"value": 1} / numeric strings so a Telegram WebApp client cannot
        # turn a harmless UI mismatch into an opaque HTTP 422.
        position: int | str | None = None
        value: int | str | None = None

        def normalized(self) -> int:
            raw = self.position if self.position is not None else self.value
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValueError("Позиция должна быть числом от 1 до 5.")
            if value not in POSITIONS:
                raise ValueError("Позиция должна быть от 1 до 5.")
            return value

    class RoleModeBody(BaseModel):
        mode: str

    class PositionsBody(BaseModel):
        positions: list[int] = Field(default_factory=list)

    class TeamCreateBody(BaseModel):
        name: str = Field(min_length=1, max_length=64)

    class InviteBody(BaseModel):
        user_id: int

    class MmrBody(BaseModel):
        mmr: int | str

        def normalized(self) -> int:
            try:
                value = int(self.mmr)
            except (TypeError, ValueError):
                raise ValueError("MMR должен быть целым числом.")
            if value < 0 or value > 9500:
                raise ValueError("MMR должен быть от 0 до 9500.")
            return value


    class SteamRebindBody(BaseModel):
        steam: str = Field(min_length=1, max_length=300)


    class AdminAddBody(BaseModel):
        telegram_id: int = Field(ge=1)


    @router.get("/health")
    async def health():
        return {"ok": True, "service": "TI:IT TOP Mini App"}

    @router.get("/me")
    async def me(auth_data=Depends(auth)):
        tg, user = auth_data
        async with session_scope() as session:
            profile = await ProfileRepository(session).get_by_user_id(user.id)
            team = await TeamRepository(session).get_for_user(user.id)
            return {
                "telegram_id": tg.id,
                "username": tg.username,
                "first_name": tg.first_name,
                "last_name": tg.last_name,
                "registered": bool(user.is_registered),
                "profile": _profile_json(profile),
                "team_id": team.id if team else None,
            }

    @router.get("/bootstrap")
    async def bootstrap(auth_data=Depends(auth)):
        tg, user = auth_data
        async with session_scope() as session:
            profile = await ProfileRepository(session).get_by_user_id(user.id)
            team = await TeamRepository(session).get_for_user(user.id)
            team_json = await _team_json(session, team, user.id) if team else None
            if team_json and team_json.get("avatar_url"):
                team_json["avatar_url"] = f"/api/team/avatar?v={int(__import__('time').time())}"
            invites = await TeamInviteRepository(session).list_pending_for_user(user.id)
            invite_json = []
            for inv in invites:
                t = await TeamRepository(session).get_by_id(inv.team_id)
                invite_json.append({
                    "id": inv.id,
                    "team_id": inv.team_id,
                    "team_name": t.name if t else "Команда",
                    "expires_at": _iso(inv.expires_at),
                })
            outgoing_invites = []
            if team and team.captain_user_id == user.id:
                now = datetime.now(team.created_at.tzinfo) if team.created_at and team.created_at.tzinfo else datetime.utcnow()
                res = await session.execute(
                    select(TeamInvite, User).join(User, User.id == TeamInvite.invitee_user_id).where(
                        TeamInvite.team_id == team.id,
                        TeamInvite.status == TeamInviteStatus.PENDING,
                        TeamInvite.expires_at > now,
                    ).order_by(TeamInvite.id.desc()).limit(20)
                )
                for inv, invitee in res.all():
                    outgoing_invites.append({
                        "id": inv.id,
                        "user_id": invitee.id,
                        "name": " ".join(x for x in [invitee.first_name, invitee.last_name] if x) or invitee.username or f"Игрок {invitee.id}",
                        "username": invitee.username,
                        "expires_at": _iso(inv.expires_at),
                    })
            tournaments = await TournamentService(session).public_list()
            apps = []
            if team:
                res = await session.execute(select(TournamentApplication).where(TournamentApplication.team_id == team.id).order_by(TournamentApplication.id.desc()).limit(5))
                for app in res.scalars().all():
                    t = await session.get(Tournament, app.tournament_id)
                    apps.append({"id": app.id, "tournament_id": app.tournament_id, "tournament_name": t.name if t else "—", "status": app.status.value})
            tournament_json = []
            for t in tournaments:
                approved = await TournamentService(session).registration_slots_left(t)
                item = _tournament_json(t)
                item["slots_left"] = approved
                item["has_team"] = team is not None
                item["my_application"] = next((a["status"] for a in apps if a["tournament_id"] == t.id), None)
                tournament_json.append(item)
            unread = await unread_count(session, user.id)
            return {
                "user": {"telegram_id": tg.id, "db_user_id": user.id, "username": tg.username, "first_name": tg.first_name, "registered": bool(user.is_registered)},
                "profile": _profile_json(profile),
                "team": team_json,
                "invites": invite_json,
                "outgoing_invites": outgoing_invites,
                "tournaments": tournament_json,
                "applications": apps,
                "is_admin": bool(is_admin(tg.id, context.config)),
                "unread_notifications": unread,
            }

    @router.get("/tournaments/{tournament_id}")
    async def tournament_detail(tournament_id: int, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            t = await session.get(Tournament, tournament_id)
            if t is None:
                raise HTTPException(status_code=404, detail="Турнир не найден")
            apps = (await session.execute(
                select(TournamentApplication, Team)
                .join(Team, Team.id == TournamentApplication.team_id)
                .where(TournamentApplication.tournament_id == tournament_id)
                .order_by(TournamentApplication.status.asc(), TournamentApplication.id.asc())
            )).all()
            rows = []
            for app, team in apps:
                captain = await session.get(User, team.captain_user_id)
                rows.append({
                    "id": app.id,
                    "team_id": team.id,
                    "team": team.name,
                    "status": app.status.value,
                    "captain": (captain.username or captain.first_name or f"Игрок {captain.id}") if captain else "—",
                    "roster_count": len(await TeamRepository(session).get_roster(team.id)),
                    "max_roster": MAX_ROSTER_SIZE,
                    "avatar": _has_team_avatar(team),
                    "avatar_url": f"/api/teams/{team.id}/avatar" if _has_team_avatar(team) else None,
                })
            bracket = await get_bracket_public_state(session, t)
            my_team = await TeamRepository(session).get_for_user(user.id)
            return {
                **_tournament_json(t),
                "slots_left": max(0, t.max_teams - sum(1 for app, _ in apps if app.status == ApplicationStatus.APPROVED)),
                "applications": rows,
                "my_application": next((app.status.value for app, team in apps if my_team and team.id == my_team.id), None),
                "bracket": bracket,
            }

    @router.get("/players")
    async def players(q: str = Query(default="", max_length=64), page: int = Query(default=1, ge=1, le=1000), auth_data=Depends(auth)):
        _, user = auth_data
        limit = 10
        offset = (page - 1) * limit
        async with session_scope() as session:
            # Search is deliberately limited to verified free agents, matching the bot's invitation rules.
            stmt = (
                select(User, PlayerProfile)
                .join(PlayerProfile, PlayerProfile.user_id == User.id)
                .outerjoin(TeamMember, TeamMember.user_id == User.id)
                .where(
                    TeamMember.id.is_(None),
                    User.id != user.id,
                    PlayerProfile.exact_mmr.is_not(None),
                    PlayerProfile.mmr_verified.is_(True),
                    PlayerProfile.verification_status == VerificationStatus.VERIFIED,
                )
            )
            if q.strip():
                like = f"%{q.strip().lstrip('@').lower()}%"
                stmt = stmt.where(or_(func.lower(User.username).like(like), func.lower(User.first_name).like(like), func.lower(User.last_name).like(like)))
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = int((await session.scalar(count_stmt)) or 0)
            rows = (await session.execute(stmt.order_by(PlayerProfile.exact_mmr.desc(), User.id).limit(limit).offset(offset))).all()
            return {"page": page, "page_size": limit, "total": total, "items": [_player_json(u, p) for u, p in rows]}

    @router.post("/profile/mmr")
    async def profile_mmr(body: MmrBody, auth_data=Depends(auth)):
        _, user = auth_data
        try:
            mmr = body.normalized()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        from services.registration import RegistrationService
        from keyboards.admin import verification_kb
        from aiogram import Bot
        async with session_scope() as session:
            svc = RegistrationService(session)
            ok, error, rank = await svc.save_exact_mmr(user.id, mmr)
            if not ok:
                raise HTTPException(status_code=400, detail=error)
            profile = await ProfileRepository(session).get_by_user_id(user.id)
            db_user = await UserRepository(session).get_by_id(user.id)
            if profile is None or db_user is None:
                raise HTTPException(status_code=400, detail="Профиль не найден.")
            admin_text = (
                "🔎 <b>Новая проверка MMR</b>\n\n"
                f"👤 Telegram: @{db_user.username or db_user.telegram_id}\n"
                f"🆔 Telegram ID: <code>{db_user.telegram_id}</code>\n"
                f"🎮 Steam ID64: <code>{profile.steam_id64 or '—'}</code>\n"
                f"🎯 Заявленный MMR: <b>{mmr}</b>\n"
                f"🏅 Ранг: <b>{rank.rank_name + ' ' + str(rank.star) if rank else 'диапазон не определён'}</b>\n\n"
                "Проверьте MMR непосредственно на аккаунте и подтвердите или отклоните заявку."
            )
            try:
                bot = _new_telegram_bot(context.config)
                from handlers.helpers import notify_admins
                await notify_admins(bot, context.config, admin_text, reply_markup=verification_kb(profile.id))
                await bot.session.close()
            except Exception:
                # Сохранение MMR не должно откатываться из-за временной ошибки уведомления.
                pass
            return {"ok": True, "mmr": mmr, "status": profile.verification_status.value, **_rank_payload(profile)}

    @router.post("/profile/steam/rebind")
    async def profile_steam_rebind(body: SteamRebindBody, auth_data=Depends(auth)):
        tg, user = auth_data
        raw = body.steam.strip()
        if not raw:
            raise HTTPException(status_code=400, detail="Укажите Steam ID, ссылку профиля или Dota Account ID.")

        services = get_services()
        result = await services.verification.verify_input(raw)
        if result.status == "INVALID_ID" or not result.is_valid_identity:
            raise HTTPException(status_code=400, detail=result.error or "Не удалось подтвердить указанный Steam-аккаунт.")
        if not result.steam_id64 or result.account_id is None:
            raise HTTPException(status_code=400, detail="Не удалось определить Steam ID64 и Dota Account ID.")

        async with session_scope() as session:
            profiles = ProfileRepository(session)
            current = await profiles.get_by_user_id(user.id)
            if current is None:
                raise HTTPException(status_code=400, detail="Профиль игрока не найден.")

            existing = await profiles.find_by_steam_id64(result.steam_id64)
            if existing is not None and existing.user_id != user.id:
                raise HTTPException(status_code=409, detail="Этот Steam-аккаунт уже привязан к другому профилю Telegram.")

            if current.steam_id64 == result.steam_id64:
                return {
                    "ok": True,
                    "changed": False,
                    "status": current.verification_status.value,
                    "steam_id64": current.steam_id64,
                    "account_id": current.account_id,
                    "message": "Этот Steam-аккаунт уже привязан к вашему профилю.",
                }

            from keyboards.admin import verification_kb
            from handlers.helpers import notify_admins

            async def _notify(card: str) -> None:
                bot = None
                try:
                    bot = _new_telegram_bot(context.config)
                    profile_for_button = await profiles.get_by_user_id(user.id)
                    await notify_admins(
                        bot,
                        context.config,
                        card,
                        reply_markup=verification_kb(profile_for_button.id) if profile_for_button else None,
                    )
                finally:
                    if bot is not None:
                        try:
                            await bot.session.close()
                        except Exception:
                            pass

            status = await RegistrationService(session).apply_verification(
                user_id=user.id,
                telegram_id=tg.id,
                username=tg.username,
                result=result,
                notify_admins_callback=_notify,
                force_pending=True,
            )
            user.is_registered = True
            await add_event(
                session,
                kind="steam_rebind",
                title="Перепривязан Steam-аккаунт",
                body=f"Telegram #{user.id}: {result.steam_id64}",
                actor_user_id=user.id,
            )
            await session.commit()
            return {
                "ok": True,
                "changed": True,
                "status": status.value,
                "steam_id64": result.steam_id64,
                "account_id": result.account_id,
                "nickname": result.nickname,
                "source": result.source,
                "message": "Steam-аккаунт перепривязан. Новый MMR снова нужно подтвердить организатору.",
            }

    @router.post("/team/create")
    async def create_team(body: TeamCreateBody, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            try:
                team = await TeamService(session).create_team(user.id, body.name)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            await add_event(session, kind="team_created", title="Создана команда", body=f"{team.name}", actor_user_id=user.id)
            await session.commit()
            return {"ok": True, "team": {"id": team.id, "name": team.name}}

    @router.post("/team/invite")
    async def invite(body: InviteBody, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            team = await TeamRepository(session).get_for_user(user.id)
            if team is None or team.captain_user_id != user.id:
                raise HTTPException(status_code=403, detail="Только капитан может приглашать игроков")
            try:
                inv = await TeamService(session).create_invite(team.id, user.id, body.user_id)
                await add_event(session, kind="team_invite", title="Новое приглашение в команду", body=f"Вас приглашают в команду #{team.id}", actor_user_id=user.id, target_user_id=body.user_id)
                await session.commit()
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {"ok": True, "invite_id": inv.id}

    @router.post("/invites/{invite_id}/accept")
    async def accept_invite(invite_id: int, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            try:
                team = await TeamService(session).accept_invite(invite_id, user.id)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            await add_event(session, kind="invite_accepted", title="Приглашение принято", body=f"Команда: {team.name}", actor_user_id=user.id)
            await session.commit()
            return {"ok": True, "team_id": team.id, "team_name": team.name}

    @router.post("/invites/{invite_id}/decline")
    async def decline_invite(invite_id: int, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            try:
                await TeamService(session).decline_invite(invite_id, user.id)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            await add_event(session, kind="invite_declined", title="Приглашение отклонено", actor_user_id=user.id)
            await session.commit()
            return {"ok": True}

    @router.post("/team/position")
    async def set_position(body: PositionBody, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            team = await TeamRepository(session).get_for_user(user.id)
            if team is None:
                raise HTTPException(status_code=400, detail="Вы не состоите в команде")
            try:
                await TeamService(session).set_position(team.id, user.id, body.normalized(), user.id)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {"ok": True}

    @router.post("/team/members/{target_id}/position")
    async def set_member_position(target_id: int, body: PositionBody, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            team = await TeamRepository(session).get_for_user(user.id)
            if team is None:
                raise HTTPException(status_code=400, detail="Вы не состоите в команде")
            try:
                await TeamService(session).set_position(team.id, target_id, body.normalized(), user.id)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {"ok": True}

    @router.post("/team/delete")
    async def delete_team(auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            team = await TeamRepository(session).get_for_user(user.id)
            if team is None:
                raise HTTPException(status_code=404, detail="Команда не найдена")
            team_name = team.name
            try:
                await TeamService(session).delete_team(team.id, user.id)
                await add_event(
                    session,
                    kind="team_deleted",
                    title="Команда расформирована",
                    body=f"Команда «{team_name}» удалена капитаном.",
                    actor_user_id=user.id,
                )
                await session.commit()
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {"ok": True, "team_name": team_name}

    @router.post("/team/role-mode")
    async def role_mode(body: RoleModeBody, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            team = await TeamRepository(session).get_for_user(user.id)
            if team is None:
                raise HTTPException(status_code=400, detail="Вы не состоите в команде")
            try:
                await TeamService(session).set_role_mode(team.id, user.id, body.mode)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {"ok": True}

    @router.post("/profile/positions")
    async def profile_positions(body: PositionsBody, auth_data=Depends(auth)):
        _, user = auth_data
        clean = sorted({p for p in body.positions if p in POSITIONS})
        if len(clean) > 5:
            raise HTTPException(status_code=400, detail="Слишком много позиций")
        async with session_scope() as session:
            await TeamService(session).set_preferred_positions(user.id, clean)
            return {"ok": True, "positions": clean}

    @router.post("/tournaments/{tournament_id}/apply")
    async def apply(tournament_id: int, auth_data=Depends(auth)):
        tg, user = auth_data
        async with session_scope() as session:
            try:
                app = await ApplicationService(session).submit(user.id, tournament_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            tournament = await session.get(Tournament, tournament_id)
            team = await session.get(Team, app.team_id)
            await add_event(
                session,
                kind="application_submitted",
                title="Новая заявка на турнир",
                body=f"{team.name if team else 'Команда'} → {tournament.name if tournament else 'Турнир'} · заявка #{app.id}",
                actor_user_id=user.id,
            )
            await session.commit()

        # Важное: подача из Mini App должна давать тот же сигнал админам,
        # что и подача через Telegram-бота. Ошибка уведомления не отменяет заявку.
        try:
            from aiogram import Bot
            from handlers.helpers import notify_admins

            bot = _new_telegram_bot(context.config)
            captain_label = f"@{tg.username}" if tg.username else f"<code>{tg.id}</code>"
            admin_text = (
                f"📋 <b>НОВАЯ ЗАЯВКА #{app.id}</b>\n\n"
                f"<b>Турнир:</b> {tournament.name if tournament else '?'}\n"
                f"<b>Команда:</b> {team.name if team else '?'}\n"
                f"<b>Капитан:</b> {captain_label}"
            )
            await notify_admins(bot, context.config, admin_text)
            await bot.session.close()
        except Exception:
            try:
                await bot.session.close()
            except Exception:
                pass

        return {"ok": True, "application_id": app.id, "status": app.status.value}

    class SupportCreateBody(BaseModel):
        message: str = Field(min_length=1, max_length=4000)

    @router.get("/support")
    async def support_list(auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            tickets = (await session.execute(select(SupportTicket).where(SupportTicket.user_id == user.id).order_by(SupportTicket.id.desc()).limit(20))).scalars().all()
            return {"items":[{"id":t.id,"status":t.status.value,"message":t.message,"reply":t.admin_reply,"created_at":_iso(t.created_at)} for t in tickets]}

    @router.post("/support")
    async def support_create(body: SupportCreateBody, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            try:
                ticket = await SupportService(session).create_ticket(user.id, body.message)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {"ok":True,"id":ticket.id,"status":ticket.status.value}

    async def _serve_team_avatar(team, *, private: bool = True):
        """Отдаёт только локальную копию аватара.

        Раньше при отсутствии локального файла здесь вызывался bot.get_file()
        (api.telegram.org) прямо во время загрузки страницы; при «Connection
        reset by peer» страница зависала. Копии из Telegram теперь переносятся
        отдельным админским действием (Mini App 5.0 → «Аватары из Telegram»).
        """
        local = _local_avatar_path(team.id)
        if local.is_file():
            cache = "private,max-age=300" if private else "public,max-age=300"
            return FileResponse(local, media_type="image/jpeg", headers={"Cache-Control": cache})
        raise HTTPException(status_code=404, detail="Аватар команды не найден")

    @router.get("/team/avatar")
    async def team_avatar(auth_data=Depends(auth)):
        """Отдаёт аватар собственной команды."""
        from io import BytesIO
        _, user = auth_data
        async with session_scope() as session:
            team = await TeamRepository(session).get_for_user(user.id)
            if team is None:
                raise HTTPException(status_code=404, detail="Команда не найдена")
            return await _serve_team_avatar(team, private=True)

    @router.get("/teams/{team_id}/avatar")
    async def public_team_avatar(team_id: int, auth_data=Depends(auth)):
        """Публичный для авторизованных пользователей аватар любой команды."""
        async with session_scope() as session:
            team = await session.get(Team, team_id)
            if team is None:
                raise HTTPException(status_code=404, detail="Команда не найдена")
            return await _serve_team_avatar(team, private=False)

    def _admin_guard(auth_data):
        tg, _ = auth_data
        if not is_admin(tg.id, context.config):
            raise HTTPException(status_code=403, detail="Нет прав администратора")
        return tg

    class AdminDecisionBody(BaseModel):
        decision: str
        mmr: int | str | None = None
        comment: str | None = None

    class AdminStatusBody(BaseModel):
        status: str

    class AdminTournamentCreateBody(BaseModel):
        name: str = Field(min_length=1, max_length=128)
        description: str | None = None
        max_teams: int = Field(default=16, ge=2, le=1024)
        open_registration: bool = False
        start_date: str | None = None

    # ------------------------------------------------------------------
    # Чат, лента событий и уведомления
    # ------------------------------------------------------------------
    @router.get("/chat/messages")
    async def chat_messages(before_id: int | None = Query(default=None, ge=1), limit: int = Query(default=50, ge=1, le=100), auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            stmt = select(ChatMessage, User).join(User, User.id == ChatMessage.user_id).where(ChatMessage.is_deleted.is_(False))
            if before_id is not None:
                stmt = stmt.where(ChatMessage.id < before_id)
            rows = (await session.execute(stmt.order_by(ChatMessage.id.desc()).limit(limit))).all()
            rows.reverse()
            return {"items":[{"id":m.id,"user_id":m.user_id,"name":u.first_name or u.username or str(u.telegram_id),"username":u.username,"text":m.text,"created_at":_iso(m.created_at),"mine":m.user_id==user.id} for m,u in rows], "has_more": len(rows) == limit}

    @router.post("/chat/messages")
    async def chat_send(body: ChatBody, auth_data=Depends(auth)):
        _, user = auth_data
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Сообщение пустое")
        async with session_scope() as session:
            last = await session.scalar(select(ChatMessage).where(ChatMessage.user_id == user.id).order_by(ChatMessage.id.desc()).limit(1))
            if last and last.created_at:
                age = (datetime.now(last.created_at.tzinfo) - last.created_at).total_seconds() if last.created_at.tzinfo else (datetime.utcnow() - last.created_at).total_seconds()
                if age < 1.0:
                    raise HTTPException(status_code=429, detail="Не так быстро — подожди секунду")
            msg = ChatMessage(user_id=user.id, text=text)
            session.add(msg)
            await session.flush()
            await session.commit()
            return {"ok":True,"id":msg.id}

    @router.delete("/chat/messages/{message_id}")
    async def chat_delete(message_id:int, auth_data=Depends(auth)):
        tg = _admin_guard(auth_data)
        async with session_scope() as session:
            msg = await session.get(ChatMessage, message_id)
            if not msg or msg.is_deleted:
                raise HTTPException(status_code=404, detail="Сообщение не найдено")
            msg.is_deleted = True
            msg.deleted_by = tg.id
            await session.commit()
            return {"ok":True}

    @router.get("/events")
    async def events(before_id:int|None=Query(default=None,ge=1), limit:int=Query(default=50,ge=1,le=100), auth_data=Depends(auth)):
        _tg, _user = auth_data
        async with session_scope() as session:
            # Ответы поддержки (текст приватной переписки) и исключения из команд —
            # только в личных уведомлениях, не в публичной ленте.
            stmt=select(ActivityEvent).where(ActivityEvent.kind.not_in(("chat_message", "support_reply", "team_member_removed")))
            if before_id is not None: stmt=stmt.where(ActivityEvent.id < before_id)
            rows=(await session.execute(stmt.order_by(ActivityEvent.id.desc()).limit(limit))).scalars().all()
            rows.reverse()
            return {"items":[{"id":e.id,"kind":e.kind,"title":e.title,"body":e.body,"created_at":_iso(e.created_at)} for e in rows],"has_more":len(rows)==limit}

    @router.get("/notifications")
    async def notifications(unread_only:bool=False, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            stmt=select(Notification).where(Notification.user_id==user.id)
            if unread_only: stmt=stmt.where(Notification.is_read.is_(False))
            rows=(await session.execute(stmt.order_by(Notification.id.desc()).limit(100))).scalars().all()
            return {"items":[{"id":n.id,"kind":n.kind,"title":n.title,"body":n.body,"read":bool(n.is_read),"created_at":_iso(n.created_at)} for n in rows],"unread":sum(1 for n in rows if not n.is_read)}

    @router.post("/notifications/read-all")
    async def notifications_read_all(auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            rows=(await session.execute(select(Notification).where(Notification.user_id==user.id, Notification.is_read.is_(False)))).scalars().all()
            for n in rows: n.is_read=True
            await session.commit()
            return {"ok":True,"count":len(rows)}

    @router.post("/notifications/{notification_id}/read")
    async def notification_read(notification_id:int, auth_data=Depends(auth)):
        _, user = auth_data
        async with session_scope() as session:
            n=await session.get(Notification,notification_id)
            if not n or n.user_id != user.id: raise HTTPException(status_code=404,detail="Уведомление не найдено")
            n.is_read=True
            await session.commit()
            return {"ok":True}

    # ------------------------------------------------------------------
    # AI-помощник администратора
    # ------------------------------------------------------------------
    _ai_last_request: dict[int, datetime] = {}

    @router.get("/admin/ai/status")
    async def admin_ai_status(auth_data=Depends(auth)):
        _admin_guard(auth_data)
        providers = []
        if (context.config.ai_api_key or "").strip():
            providers.append({"name":"OpenRouter primary","model":context.config.ai_model,"configured":True})
            if (context.config.ai_fallback_model or "").strip() and context.config.ai_fallback_model != context.config.ai_model:
                providers.append({"name":"OpenRouter free fallback","model":context.config.ai_fallback_model,"configured":True})
        providers.append({"name":"Groq backup","model":context.config.ai_backup_model,"configured":bool((context.config.ai_backup_api_key or "").strip())})
        return {
            "configured": bool(providers and any(x["configured"] for x in providers)),
            "model": context.config.ai_model,
            "endpoint": context.config.ai_base_url,
            "tool_count": len(TOOL_DEFINITIONS),
            "max_tool_rounds": context.config.ai_max_tool_rounds,
            "proxy_configured": bool((context.config.http_proxy_url or "").strip()),
            "providers": providers,
        }

    @router.post("/admin/ai/ask")
    async def admin_ai_ask(body: AIAskBody = Body(...), auth_data=Depends(auth)):
        tg = _admin_guard(auth_data)
        now = datetime.utcnow()
        last = _ai_last_request.get(tg.id)
        if last is not None and (now - last).total_seconds() < 2.0:
            raise HTTPException(status_code=429, detail="Подожди пару секунд перед следующим запросом")
        _ai_last_request[tg.id] = now
        try:
            async with session_scope() as session:
                answer, telemetry = await ask_ai_agent(context.config, session, body.question, body.history)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Не удалось получить ответ AI: {type(exc).__name__}: {str(exc)[:700]}") from exc
        return {"ok": True, "answer": answer, **telemetry}

    # ------------------------------------------------------------------
    # Администраторы
    # ------------------------------------------------------------------
    @router.get("/admin/admins")
    async def admin_admins(auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            rows=(await session.execute(select(AdminUser).where(AdminUser.active.is_(True)).order_by(AdminUser.id))).scalars().all()
            items=[]
            for a in rows:
                u=await session.scalar(select(User).where(User.telegram_id==a.telegram_id))
                items.append({"id":a.id,"telegram_id":a.telegram_id,"name":(u.first_name or u.username or str(a.telegram_id)) if u else str(a.telegram_id),"username":u.username if u else None,"created_at":_iso(a.created_at)})
            return {"items":items}

    @router.post("/admin/admins")
    async def admin_add(body:AdminAddBody, auth_data=Depends(auth)):
        tg=_admin_guard(auth_data)
        async with session_scope() as session:
            row=await session.scalar(select(AdminUser).where(AdminUser.telegram_id==body.telegram_id))
            if row is not None:
                row.active=True
            else:
                row=AdminUser(telegram_id=body.telegram_id,added_by=tg.id,active=True)
                session.add(row)
            await session.commit()
            context.config.admin_ids.add(body.telegram_id)
            from config import GLOBAL_CONFIG
            GLOBAL_CONFIG.admin_ids.add(body.telegram_id)
            return {"ok":True,"telegram_id":body.telegram_id}

    @router.delete("/admin/admins/{telegram_id}")
    async def admin_remove(telegram_id:int, auth_data=Depends(auth)):
        tg=_admin_guard(auth_data)
        if telegram_id == tg.id:
            raise HTTPException(status_code=400, detail="Нельзя удалить самого себя. Передай права другому админу.")
        async with session_scope() as session:
            row=await session.scalar(select(AdminUser).where(AdminUser.telegram_id==telegram_id,AdminUser.active.is_(True)))
            if row is None: raise HTTPException(status_code=404,detail="Администратор не найден")
            active_count=int((await session.scalar(select(func.count()).select_from(AdminUser).where(AdminUser.active.is_(True)))) or 0)
            if active_count <= 1: raise HTTPException(status_code=400,detail="Должен остаться хотя бы один администратор")
            row.active=False
            await session.commit()
            context.config.admin_ids.discard(telegram_id)
            from config import GLOBAL_CONFIG
            GLOBAL_CONFIG.admin_ids.discard(telegram_id)
            return {"ok":True}

    @router.get("/admin/overview")
    async def admin_overview(auth_data=Depends(auth)):
        _admin_guard(auth_data)
        # Раньше здесь было N+1: отдельный SELECT пользователя на каждый профиль,
        # COUNT на каждый турнир, COUNT + SELECT капитана на каждую команду.
        # Теперь 4 агрегированных запроса; формат ответа не изменился.
        async with session_scope() as session:
            pending_rows = (await session.execute(
                select(PlayerProfile, User)
                .outerjoin(User, User.id == PlayerProfile.user_id)
                .where(PlayerProfile.verification_status == VerificationStatus.PENDING)
                .order_by(PlayerProfile.updated_at)
                .limit(50)
            )).all()
            pending = []
            for p, u in pending_rows:
                pending.append({
                    "id": p.id, "user_id": p.user_id,
                    "name": " ".join(x for x in [u.first_name, u.last_name] if x) if u else f"Игрок {p.user_id}",
                    "username": u.username if u else None,
                    "steam_nickname": p.steam_nickname, "steam_id64": p.steam_id64,
                    "account_id": p.account_id, "exact_mmr": p.exact_mmr,
                    "rank": _rank_label(p), **_rank_payload(p),
                    "matches": p.matches, "winrate": p.winrate,
                    "source": p.verification_source, "updated_at": _iso(p.updated_at),
                })

            tournaments = await TournamentRepository(session).list_all(limit=50)
            approved_by_tournament = {
                int(tid): int(n)
                for tid, n in (await session.execute(
                    select(TournamentApplication.tournament_id, func.count(TournamentApplication.id))
                    .where(TournamentApplication.status == ApplicationStatus.APPROVED)
                    .group_by(TournamentApplication.tournament_id)
                )).all()
            }
            tours = [
                {"id": t.id, "name": t.name, "status": t.status.value, "registration_open": bool(t.registration_open),
                 "max_teams": t.max_teams, "start_date": _iso(t.start_date),
                 "slots_left": max(0, int(t.max_teams) - approved_by_tournament.get(int(t.id), 0))}
                for t in tournaments
            ]

            member_counts = (
                select(TeamMember.team_id.label("team_id"), func.count(TeamMember.id).label("members"))
                .group_by(TeamMember.team_id)
                .subquery()
            )
            team_rows = []
            for team, cap, members in (await session.execute(
                select(Team, User, func.coalesce(member_counts.c.members, 0))
                .outerjoin(User, User.id == Team.captain_user_id)
                .outerjoin(member_counts, member_counts.c.team_id == Team.id)
                .order_by(Team.id.desc())
                .limit(50)
            )).all():
                team_rows.append({"id": team.id, "name": team.name, "members": int(members or 0),
                                  "captain": (cap.username or cap.first_name) if cap else None,
                                  "avatar": _has_team_avatar(team)})

            users_n, teams_n, tournaments_n, pending_n, verified_n = (await session.execute(select(
                select(func.count()).select_from(User).scalar_subquery(),
                select(func.count()).select_from(Team).scalar_subquery(),
                select(func.count()).select_from(Tournament).scalar_subquery(),
                select(func.count()).select_from(PlayerProfile).where(PlayerProfile.verification_status == VerificationStatus.PENDING).scalar_subquery(),
                select(func.count()).select_from(PlayerProfile).where(PlayerProfile.verification_status == VerificationStatus.VERIFIED).scalar_subquery(),
            ))).one()
            return {"stats":{"users":int(users_n or 0),"teams":int(teams_n or 0),"tournaments":int(tournaments_n or 0),"pending_players":int(pending_n or 0),"verified_players":int(verified_n or 0)},"pending":pending,"tournaments":tours,"teams":team_rows,"mmr_range_hard_block":bool(context.config.mmr_rank_range_hard_block)}

    @router.post("/admin/verification/{profile_id}")
    async def admin_verification(profile_id:int, body:AdminDecisionBody, auth_data=Depends(auth)):
        tg=_admin_guard(auth_data)
        decision=body.decision.strip().lower()
        if decision not in {"approve","reject"}:
            raise HTTPException(status_code=400, detail="decision должен быть approve или reject")
        async with session_scope() as session:
            profile=await session.get(PlayerProfile, profile_id)
            if profile is None:
                raise HTTPException(status_code=404, detail="Профиль не найден")
            if body.mmr is not None:
                try: mmr=int(body.mmr)
                except (TypeError,ValueError): raise HTTPException(status_code=400, detail="MMR должен быть числом")
                if not 0 <= mmr <= 9500: raise HTTPException(status_code=400, detail="MMR должен быть от 0 до 9500")
                profile.exact_mmr=mmr
            if decision=="approve" and profile.exact_mmr is None:
                raise HTTPException(status_code=400, detail="Для подтверждения нужен точный MMR")
            await RegistrationService(session).set_mmr_decision(profile, verified=decision=="approve", admin_id=tg.id, comment=body.comment)
            title = "MMR подтверждён" if decision == "approve" else "MMR отклонён"
            await add_event(session, kind="mmr_decision", title=title, body=f"Заявка профиля #{profile.id}", actor_user_id=None, target_user_id=profile.user_id)
            await session.commit()
            return {"ok":True,"status":profile.verification_status.value,"exact_mmr":profile.exact_mmr}

    @router.post("/admin/tournaments/{tournament_id}/status")
    async def admin_tournament_status(tournament_id:int, body:AdminStatusBody, auth_data=Depends(auth)):
        _admin_guard(auth_data)
        try: status=TournamentStatus(body.status.upper())
        except ValueError: raise HTTPException(status_code=400, detail="Неизвестный статус турнира")
        async with session_scope() as session:
            try: t=await TournamentService(session).set_status(tournament_id,status)
            except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc))
            await add_event(session, kind="tournament_status", title="Изменён статус турнира", body=f"{t.name}: {t.status.value}")
            await session.commit()
            return {"ok":True,"id":t.id,"status":t.status.value,"registration_open":bool(t.registration_open)}

    @router.post("/admin/tournaments")
    async def admin_tournament_create(body:AdminTournamentCreateBody, auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            start_date = None
            if body.start_date:
                try:
                    start_date = datetime.fromisoformat(body.start_date.replace('Z', '+00:00'))
                except ValueError:
                    raise HTTPException(status_code=400, detail="Неверная дата начала турнира")
            t=await TournamentService(session).create(body.name, body.description, body.max_teams, start_date, body.open_registration)
            await add_event(session, kind="tournament_created", title="Создан турнир", body=t.name)
            await session.commit()
            return {"ok":True,"id":t.id,"name":t.name,"status":t.status.value}


    @router.get("/admin/tournaments/{tournament_id}/bracket")
    async def admin_tournament_bracket(tournament_id: int, auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            t = await session.get(Tournament, tournament_id)
            if t is None:
                raise HTTPException(status_code=404, detail="Турнир не найден")
            return await get_bracket_admin_state(session, t)

    class AdminBracketGenerateBody(BaseModel):
        # New API: the two teams that must play Match 1.
        opening_team_a: int | None = None
        opening_team_b: int | None = None
        # Legacy aliases accepted so an older Mini App can still generate a grid.
        manual_seed_1: int | None = None
        manual_seed_2: int | None = None
        lock: bool = False

    @router.post("/admin/tournaments/{tournament_id}/bracket/generate")
    async def admin_tournament_bracket_generate(
        tournament_id: int,
        body: AdminBracketGenerateBody,
        auth_data=Depends(auth),
    ):
        tg = _admin_guard(auth_data)
        async with session_scope() as session:
            t = await session.get(Tournament, tournament_id)
            if t is None:
                raise HTTPException(status_code=404, detail="Турнир не найден")
            opening_a = body.opening_team_a if body.opening_team_a is not None else body.manual_seed_1
            opening_b = body.opening_team_b if body.opening_team_b is not None else body.manual_seed_2
            if opening_a is None or opening_b is None:
                raise HTTPException(status_code=400, detail="Для первого матча выберите две команды.")
            try:
                result = await generate_bracket(session, t, opening_a, opening_b, body.lock)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            await add_event(
                session,
                kind="tournament_bracket",
                title="Сетка турнира сгенерирована" if not body.lock else "Сетка турнира зафиксирована",
                body=f"{t.name}: открывающий матч #{opening_a} vs #{opening_b}",
                actor_user_id=None,
            )
            await session.commit()
            return {"ok": True, **result}

    @router.post("/admin/tournaments/{tournament_id}/bracket/lock")
    async def admin_tournament_bracket_lock(tournament_id: int, auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            t = await session.get(Tournament, tournament_id)
            if t is None:
                raise HTTPException(status_code=404, detail="Турнир не найден")
            try:
                result = await lock_bracket(session, t)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            await add_event(session, kind="tournament_bracket", title="Сетка турнира зафиксирована", body=t.name)
            await session.commit()
            return {"ok": True, **result}

    # ------------------------------------------------------------------
    # Full Mini App admin console
    # ------------------------------------------------------------------
    @router.get("/admin/users")
    async def admin_users(q: str = Query(default="", max_length=64), page: int = Query(default=1, ge=1, le=1000), auth_data=Depends(auth)):
        _admin_guard(auth_data)
        limit = 30
        offset = (page - 1) * limit
        async with session_scope() as session:
            stmt = select(User).order_by(User.id.desc())
            if q.strip():
                like = f"%{q.strip().lstrip('@').lower()}%"
                stmt = stmt.where(or_(func.lower(User.username).like(like), func.lower(User.first_name).like(like), func.lower(User.last_name).like(like), func.cast(User.telegram_id, String).like(like)))
            total = int((await session.scalar(select(func.count()).select_from(stmt.subquery()))) or 0)
            users = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
            items=[]
            for u in users:
                p=await ProfileRepository(session).get_by_user_id(u.id)
                team=await TeamRepository(session).get_for_user(u.id)
                items.append({"id":u.id,"telegram_id":u.telegram_id,"username":u.username,"name":" ".join(x for x in [u.first_name,u.last_name] if x) or u.username or f"Игрок {u.id}","registered":bool(u.is_registered),"team":team.name if team else None,"status":p.verification_status.value if p else "UNVERIFIED","mmr":p.exact_mmr if p else None})
            return {"page":page,"page_size":limit,"total":total,"items":items}

    @router.get("/admin/users/{user_id}")
    async def admin_user_detail(user_id:int, auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            u=await session.get(User,user_id)
            if not u: raise HTTPException(status_code=404,detail="Пользователь не найден")
            p=await ProfileRepository(session).get_by_user_id(user_id)
            team=await TeamRepository(session).get_for_user(user_id)
            return {"id":u.id,"telegram_id":u.telegram_id,"username":u.username,"first_name":u.first_name,"last_name":u.last_name,"registered":bool(u.is_registered),"created_at":_iso(u.created_at),"team":(await _team_json(session,team,user_id)) if team else None,"profile":_profile_json(p)}

    @router.get("/admin/teams")
    async def admin_teams(q: str = Query(default="", max_length=64), auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            stmt=select(Team).order_by(Team.id.desc())
            if q.strip(): stmt=stmt.where(func.lower(Team.name).like(f"%{q.strip().lower()}%"))
            teams=(await session.execute(stmt.limit(100))).scalars().all()
            rows=[]
            for t in teams:
                roster=(await session.execute(select(TeamMember,User,PlayerProfile).join(User,User.id==TeamMember.user_id).outerjoin(PlayerProfile,PlayerProfile.user_id==User.id).where(TeamMember.team_id==t.id).order_by(TeamMember.id))).all()
                mmrs=[p.exact_mmr for _,_,p in roster if p and p.exact_mmr is not None and p.mmr_verified]
                cap=await session.get(User,t.captain_user_id)
                rows.append({"id":t.id,"name":t.name,"captain":(cap.username or cap.first_name or str(cap.telegram_id)) if cap else "—","captain_user_id":t.captain_user_id,"members":len(roster),"max_members":MAX_ROSTER_SIZE,"over_limit":len(roster)>MAX_ROSTER_SIZE,"avg_mmr":round(sum(mmrs)/len(mmrs)) if mmrs else None,"avatar":_has_team_avatar(t),"avatar_url":f"/api/admin/teams/{t.id}/avatar" if _has_team_avatar(t) else None,"role_mode":t.role_mode})
            return {"items":rows}

    @router.get("/admin/teams/{team_id}")
    async def admin_team_detail(team_id:int, auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            t=await session.get(Team,team_id)
            if not t: raise HTTPException(status_code=404,detail="Команда не найдена")
            data=await _team_json(session,t,t.captain_user_id)
            data["avatar_url"]=f"/api/admin/teams/{team_id}/avatar?v={int(__import__('time').time())}" if _has_team_avatar(t) else None
            return data

    @router.get("/admin/teams/{team_id}/avatar")
    async def admin_team_avatar(team_id:int, auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            t=await session.get(Team,team_id)
            if not t: raise HTTPException(status_code=404,detail="Команда не найдена")
            # Только локальный файл: Telegram API не вызывается при загрузке страницы.
            return await _serve_team_avatar(t, private=True)

    @router.post("/team/avatar/upload")
    async def upload_team_avatar(file: UploadFile = File(...), auth_data=Depends(auth)):
        _, user=auth_data
        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(status_code=400,detail="Аватар должен быть изображением")
        raw=await file.read()
        if not raw or len(raw)>8*1024*1024:
            raise HTTPException(status_code=400,detail="Изображение должно быть от 1 байта до 8 МБ")
        async with session_scope() as session:
            team=await TeamRepository(session).get_for_user(user.id)
            if not team or team.captain_user_id!=user.id: raise HTTPException(status_code=403,detail="Только капитан может менять аватар")

            try:
                normalized = _prepare_avatar(raw)
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Не удалось прочитать изображение. Используй JPG, PNG, WebP или GIF.") from exc

            TEAM_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            local_path = _local_avatar_path(team.id)
            tmp_path = local_path.with_suffix(".tmp")
            tmp_path.write_bytes(normalized)
            tmp_path.replace(local_path)

            # Веб-аватарка уже сохранена локально. Telegram file_id — дополнительный
            # канал для отображения аватарки в сообщениях бота, а не обязательная часть upload.
            telegram_file_id = None
            from aiogram.types import BufferedInputFile
            bot=_new_telegram_bot(context.config)
            try:
                sent=await bot.send_photo(
                    user.telegram_id,
                    BufferedInputFile(normalized, filename="team_avatar.jpg"),
                )
                telegram_file_id=sent.photo[-1].file_id
                try: await bot.delete_message(user.telegram_id,sent.message_id)
                except Exception: pass
            except Exception as exc:
                import logging
                logging.getLogger("MINIAPP").warning("[TEAM AVATAR] Telegram upload failed; local avatar kept: %s", exc)
            finally:
                await bot.session.close()

            await TeamService(session).set_avatar(team.id,user.id,telegram_file_id,"photo")
            return {"ok":True,"avatar_url":f"/api/team/avatar?v={int(__import__('time').time())}","telegram_uploaded":bool(telegram_file_id)}

    @router.delete("/admin/teams/{team_id}/members/{user_id}")
    async def admin_remove_team_member(team_id: int, user_id: int, auth_data=Depends(auth)):
        tg = _admin_guard(auth_data)
        async with session_scope() as session:
            team = await session.get(Team, team_id)
            if team is None:
                raise HTTPException(status_code=404, detail="Команда не найдена")
            member = await session.execute(select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id))
            member = member.scalars().first()
            if member is None:
                raise HTTPException(status_code=404, detail="Игрок не найден в составе")
            if member.user_id == team.captain_user_id:
                raise HTTPException(status_code=400, detail="Нельзя удалить капитана. Сначала смените капитана или расформируйте команду.")
            await session.delete(member)
            await session.commit()
            return {"ok": True, "team_id": team_id, "removed_user_id": user_id}

    class AdminApplicationDecisionBody(BaseModel):
        status: str
        comment: str | None = None

    @router.get("/admin/applications")
    async def admin_applications(status: str = Query(default="ALL"), auth_data=Depends(auth)):
        _admin_guard(auth_data)
        requested = status.strip().upper()
        if requested not in {"ALL", "DRAFT", "PENDING", "APPROVED", "REJECTED", "CANCELLED"}:
            raise HTTPException(status_code=400, detail="Неизвестный статус заявки")
        async with session_scope() as session:
            # Загружаем заявки без SQL-фильтра по Enum. Это намеренно делает
            # административный список устойчивым к старым SQLite/SQLAlchemy
            # представлениям Enum и гарантирует, что статус строки берётся
            # непосредственно из сохранённой записи.
            stmt = (
                select(TournamentApplication)
                .order_by(TournamentApplication.id.desc())
                .limit(500)
            )
            apps = list((await session.execute(stmt)).scalars().all())
            counts = {key: 0 for key in ("DRAFT", "PENDING", "APPROVED", "REJECTED", "CANCELLED")}
            for a in apps:
                key = a.status.value
                counts[key] = counts.get(key, 0) + 1

            if requested != "ALL":
                apps = [a for a in apps if a.status.value == requested]

            out=[]
            for a in apps:
                t=await session.get(Tournament,a.tournament_id)
                team=await session.get(Team,a.team_id)
                cap=await session.get(User,a.created_by)
                out.append({
                    "id":a.id,
                    "status":a.status.value,
                    "comment":a.admin_comment,
                    "tournament_id":a.tournament_id,
                    "tournament":t.name if t else "—",
                    "team_id":a.team_id,
                    "team":team.name if team else "—",
                    "captain":(cap.username or cap.first_name or str(cap.telegram_id)) if cap else "—",
                    "created_at":_iso(a.created_at),
                    "decided_at":_iso(a.decided_at) if a.decided_at else None,
                })
            return {"items":out, "counts":counts, "total":sum(counts.values())}

    @router.post("/admin/applications/{app_id}/decision")
    async def admin_application_decision(app_id:int, body:AdminApplicationDecisionBody, auth_data=Depends(auth)):
        tg,_=_admin_guard(auth_data), auth_data[1]
        try: status=ApplicationStatus(body.status.upper())
        except ValueError: raise HTTPException(status_code=400,detail="Неизвестный статус")
        if status not in {ApplicationStatus.APPROVED,ApplicationStatus.REJECTED,ApplicationStatus.CANCELLED}:
            raise HTTPException(status_code=400,detail="Допустимы только APPROVED, REJECTED, CANCELLED")
        async with session_scope() as session:
            app=await session.get(TournamentApplication,app_id)
            if not app: raise HTTPException(status_code=404,detail="Заявка не найдена")
            if app.status not in {ApplicationStatus.PENDING,ApplicationStatus.DRAFT}:
                raise HTTPException(status_code=400,detail="Заявка уже обработана")
            await ApplicationRepository(session).decide(app,status,tg.id,body.comment)
            cap=await session.get(User,app.created_by)
            decision_title = "Заявка одобрена" if status == ApplicationStatus.APPROVED else ("Заявка отклонена" if status == ApplicationStatus.REJECTED else "Заявка отменена")
            await add_event(session, kind="application_decision", title=decision_title, body=body.comment or f"Заявка #{app.id}", target_user_id=cap.id if cap else None)
            await session.commit()
            if cap:
                try:
                    from aiogram import Bot
                    bot=_new_telegram_bot(context.config)
                    text="✅ Заявка команды одобрена." if status==ApplicationStatus.APPROVED else ("❌ Заявка команды отклонена." if status==ApplicationStatus.REJECTED else "⛔ Заявка отменена.")
                    if body.comment: text += f"\nКомментарий: {body.comment}"
                    await bot.send_message(cap.telegram_id,text)
                    await bot.session.close()
                except Exception: pass
            return {"ok":True,"status":app.status.value}

    @router.get("/admin/support")
    async def admin_support(status: str = Query(default="OPEN"), auth_data=Depends(auth)):
        _admin_guard(auth_data)
        try: wanted=TicketStatus(status.upper())
        except ValueError: raise HTTPException(status_code=400,detail="Неизвестный статус")
        async with session_scope() as session:
            tickets=(await session.execute(select(SupportTicket).where(SupportTicket.status==wanted).order_by(SupportTicket.id.desc()).limit(100))).scalars().all()
            out=[]
            for t in tickets:
                u=await session.get(User,t.user_id)
                out.append({"id":t.id,"status":t.status.value,"message":t.message,"admin_reply":t.admin_reply,"user_id":t.user_id,"user":(u.username or u.first_name or str(u.telegram_id)) if u else "—","telegram_id":u.telegram_id if u else None,"created_at":_iso(t.created_at)})
            return {"items":out}

    class AdminSupportReplyBody(BaseModel):
        reply: str = Field(min_length=1,max_length=4000)

    @router.post("/admin/support/{ticket_id}/reply")
    async def admin_support_reply(ticket_id:int, body:AdminSupportReplyBody, auth_data=Depends(auth)):
        tg,_=_admin_guard(auth_data), auth_data[1]
        async with session_scope() as session:
            pair=await SupportService(session).get_for_admin(ticket_id)
            if not pair: raise HTTPException(status_code=404,detail="Тикет не найден")
            ticket,user=pair
            try: await SupportService(session).answer(ticket_id,body.reply,tg.id)
            except ValueError as exc: raise HTTPException(status_code=400,detail=str(exc))
            await add_event(session, kind="support_reply", title="Ответ поддержки", body=body.reply, target_user_id=user.id)
            await session.commit()
            try:
                from aiogram import Bot
                bot=_new_telegram_bot(context.config)
                await bot.send_message(user.telegram_id,f"💬 Ответ поддержки:\n\n{body.reply}")
                await bot.session.close()
            except Exception: pass
            return {"ok":True}

    @router.get("/admin/diagnostics")
    async def admin_diagnostics(auth_data=Depends(auth)):
        _admin_guard(auth_data)
        from services.diagnostics import run_diagnostics
        try:
            result=await run_diagnostics(__import__('services.factory',fromlist=['get_services']).get_services())
            return {"ok":True,"text":result}
        except Exception as exc:
            return {"ok":False,"text":str(exc)}

    class AdminBroadcastBody(BaseModel):
        text: str = Field(min_length=1,max_length=4096)

    async def _broadcast_text(text_value:str, admin_tg_id:int):
        from aiogram import Bot
        from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
        async with session_scope() as session:
            ids=[int(x) for x in (await session.execute(select(User.telegram_id).order_by(User.id))).scalars().all() if x]
        bot=_new_telegram_bot(context.config); sent=blocked=failed=0
        try:
            for tid in ids:
                try:
                    await bot.send_message(tid,text_value); sent+=1
                except TelegramForbiddenError: blocked+=1
                except TelegramRetryAfter as e:
                    import asyncio; await asyncio.sleep(float(e.retry_after)+0.5)
                    try: await bot.send_message(tid,text_value); sent+=1
                    except TelegramForbiddenError: blocked+=1
                    except Exception: failed+=1
                except Exception: failed+=1
                import asyncio; await asyncio.sleep(0.05)
        finally: await bot.session.close()
        async with session_scope() as session:
            session.add(BroadcastLog(admin_user_id=admin_tg_id,content_type="текст",total=len(ids),sent=sent,blocked=blocked,failed=failed)); await session.commit()
        return {"total":len(ids),"sent":sent,"blocked":blocked,"failed":failed}

    @router.post("/admin/broadcast")
    async def admin_broadcast(body:AdminBroadcastBody, auth_data=Depends(auth)):
        tg,_=_admin_guard(auth_data), auth_data[1]
        return {"ok":True,**await _broadcast_text(body.text,tg.id)}

    @router.get("/admin/broadcast/history")
    async def admin_broadcast_history(auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            rows=(await session.execute(select(BroadcastLog).order_by(BroadcastLog.id.desc()).limit(30))).scalars().all()
            return {"items":[{"id":r.id,"type":r.content_type,"total":r.total,"sent":r.sent,"blocked":r.blocked,"failed":r.failed,"created_at":_iso(r.created_at)} for r in rows]}

    class AdminTournamentEditBody(BaseModel):
        name: str | None = Field(default=None,max_length=128)
        description: str | None = Field(default=None,max_length=4000)
        max_teams: int | None = Field(default=None,ge=2,le=1024)
        start_date: str | None = None

    @router.get("/admin/tournaments/{tournament_id}")
    async def admin_tournament_detail(tournament_id:int, auth_data=Depends(auth)):
        _admin_guard(auth_data)
        async with session_scope() as session:
            t=await session.get(Tournament,tournament_id)
            if not t: raise HTTPException(status_code=404,detail="Турнир не найден")
            apps=(await session.execute(select(TournamentApplication).where(TournamentApplication.tournament_id==t.id).order_by(TournamentApplication.id.desc()))).scalars().all()
            return {"id":t.id,"name":t.name,"description":t.description,"status":t.status.value,"registration_open":bool(t.registration_open),"max_teams":t.max_teams,"start_date":_iso(t.start_date),"applications":[{"id":a.id,"team_id":a.team_id,"status":a.status.value} for a in apps]}

    @router.patch("/admin/tournaments/{tournament_id}")
    async def admin_tournament_edit(tournament_id:int, body:AdminTournamentEditBody, auth_data=Depends(auth)):
        _admin_guard(auth_data)
        from datetime import datetime
        async with session_scope() as session:
            t=await session.get(Tournament,tournament_id)
            if not t: raise HTTPException(status_code=404,detail="Турнир не найден")
            if body.name is not None: t.name=body.name.strip() or t.name
            if body.description is not None: t.description=body.description.strip() or None
            if body.max_teams is not None: t.max_teams=body.max_teams
            if body.start_date is not None:
                try: t.start_date=datetime.fromisoformat(body.start_date.replace('Z','+00:00')) if body.start_date else None
                except ValueError: raise HTTPException(status_code=400,detail="Неверная дата")
            t.updated_at=datetime.now(t.updated_at.tzinfo) if t.updated_at and t.updated_at.tzinfo else datetime.utcnow()
            await session.commit()
            return {"ok":True}

    @router.get("/admin/stats")
    async def admin_stats(auth_data=Depends(auth)):
        tg, user = auth_data
        if not is_admin(tg.id, context.config):
            raise HTTPException(status_code=403, detail="Нет прав администратора")
        async with session_scope() as session:
            users_n = int((await session.scalar(select(func.count()).select_from(User))) or 0)
            teams_n = int((await session.scalar(select(func.count()).select_from(Team))) or 0)
            tournaments_n = int((await session.scalar(select(func.count()).select_from(Tournament))) or 0)
            pending = int((await session.scalar(select(func.count()).select_from(PlayerProfile).where(PlayerProfile.verification_status == VerificationStatus.PENDING))) or 0)
            verified = int((await session.scalar(select(func.count()).select_from(PlayerProfile).where(PlayerProfile.verification_status == VerificationStatus.VERIFIED))) or 0)
            return {"users": users_n, "teams": teams_n, "tournaments": tournaments_n, "pending_players": pending, "verified_players": verified}

    return router


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _profile_json(p: PlayerProfile | None) -> dict | None:
    if p is None:
        return None
    return {
        "steam_nickname": p.steam_nickname,
        "steam_id64": p.steam_id64,
        "account_id": p.account_id,
        "status": p.verification_status.value if p.verification_status else None,
        "exact_mmr": p.exact_mmr,
        "approved_mmr": p.approved_mmr,
        "mmr_verified": bool(p.mmr_verified),
        "rank_name": _rank_label(p),
        **_rank_payload(p),
        "matches": p.matches,
        "winrate": p.winrate,
        "hours_total": p.hours_total,
        "preferred_positions": _positions(p.preferred_positions),
        "source": p.verification_source,
        "admin_comment": p.admin_comment,
    }


def _player_json(user: User, profile: PlayerProfile) -> dict:
    return {
        "user_id": user.id,
        "username": user.username,
        "name": " ".join(x for x in [user.first_name, user.last_name] if x) or user.username or f"Игрок {user.id}",
        "mmr": profile.exact_mmr,
        "approved_mmr": profile.approved_mmr,
        "rank": _rank_label(profile),
        **_rank_payload(profile),
        "positions": _positions(profile.preferred_positions),
    }


async def _team_json(session, team, current_user_id: int) -> dict:
    members = (await session.execute(select(TeamMember, User, PlayerProfile).join(User, User.id == TeamMember.user_id).outerjoin(PlayerProfile, PlayerProfile.user_id == User.id).where(TeamMember.team_id == team.id).order_by(TeamMember.id))).all()
    mmrs = [p.exact_mmr for _, _, p in members if p and p.exact_mmr is not None and p.mmr_verified]
    roster = []
    for member, user, profile in members:
        roster.append({
            "user_id": user.id,
            "username": user.username,
            "name": " ".join(x for x in [user.first_name, user.last_name] if x) or user.username or f"Игрок {user.id}",
            "role": member.role.value,
            "position": member.position,
            "mmr": profile.exact_mmr if profile else None,
            "approved_mmr": profile.approved_mmr if profile else None,
            "mmr_verified": bool(profile and profile.mmr_verified),
            "rank": _rank_label(profile),
            **_rank_payload(profile),
        })
    applications = await ApplicationRepository(session).get_for_team(team.id)
    has_approved = any(a.status == ApplicationStatus.APPROVED for a in applications)
    is_captain = team.captain_user_id == current_user_id
    return {
        "id": team.id,
        "name": team.name,
        "captain_user_id": team.captain_user_id,
        "is_captain": is_captain,
        "can_delete": bool(is_captain and not has_approved),
        "delete_block_reason": (
            "Команда уже допущена к турниру — для расформирования обратитесь к организатору."
            if is_captain and has_approved else None
        ),
        "role_mode": team.role_mode,
        "avatar": _has_team_avatar(team),
        "avatar_url": f"/api/team/avatar?v={int(__import__('time').time())}" if _has_team_avatar(team) else None,
        "roster_count": len(roster),
        "max_roster": MAX_ROSTER_SIZE,
        "over_limit": len(roster) > MAX_ROSTER_SIZE,
        "avg_mmr": round(sum(mmrs) / len(mmrs)) if mmrs else None,
        "roster": roster,
    }


def _tournament_json(t: Tournament) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "status": t.status.value,
        "registration_open": bool(t.registration_open),
        "max_teams": t.max_teams,
        "start_date": _iso(t.start_date),
    }
