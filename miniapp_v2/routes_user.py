"""Пользовательский API Mini App 5.0 (/api/v2/...).

Каждый раздел загружается отдельным запросом. Обычные GET-запросы не
обращаются к Telegram API и внешним сервисам. Внешняя проверка Steam — только
по явному действию пользователя и с общим таймаутом.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select

from database.engine import session_scope
from database.models import (
    TeamRole,
    ActivityEvent,
    ApplicationStatus,
    ChatMessage,
    Notification,
    PlayerProfile,
    RankRange,
    SupportTicket,
    Team,
    TeamInvite,
    TeamInviteStatus,
    TeamMember,
    Tournament,
    TournamentApplication,
    TournamentStatus,
    User,
    VerificationStatus,
    utcnow,
)
from database.repositories import ProfileRepository, TeamRepository
from miniapp_v2 import VERSION, notify
from miniapp_v2.common import (
    POSITION_NAMES,
    TEAM_AVATAR_DIR,
    SafeRoute,
    Viewer,
    as_utc,
    avatar_url,
    display_name,
    get_config,
    html_escape,
    invalidate_public,
    iso,
    medal_facts,
    mmr_facts,
    page_params,
    page_payload,
    parse_positions,
    parse_telegram_user,
    PLAYER_AVATAR_DIR,
    player_avatar_url,
    player_brief,
    public_cache,
    resolve_viewer,
    stats_facts,
    verification_state,
    viewer_dep,
)
from miniapp_v2.queries import (
    application_counts_by_tournament,
    confirmed_mmr_expr,
    load_roster,
    outgoing_invites_for_team,
    pending_invites_for_user,
    read_team_description,
    roster_summary,
    team_applications,
    team_list_statement,
    team_row_json,
    tournament_json,
)
from services.activity import add_event
from services.applications import MIN_PLAYERS_TO_APPLY, ApplicationService
from services.registration import RegistrationService
from services.support import SupportService
from services.roster import RosterFrozen
from services.teams import MAX_ROSTER_SIZE, POSITIONS, ROLE_MODES, TeamService

logger = logging.getLogger("MINIAPP_V2")

PRIVATE_EVENT_KINDS = ("chat_message", "support_reply", "team_member_removed")

STEAM_CHECK_TIMEOUT = 45.0
AVATAR_MAX_BYTES = 8 * 1024 * 1024
AVATAR_MAX_SIDE = 512
PUBLIC_TTL = 20.0

_rate: dict[tuple[str, int], float] = {}


def _rate_limit(kind: str, user_id: int, seconds: float, message: str) -> None:
    now = time.monotonic()
    key = (kind, int(user_id))
    last = _rate.get(key)
    if last is not None and now - last < seconds:
        raise HTTPException(status_code=429, detail=message)
    if len(_rate) > 5000:
        _rate.clear()
    _rate[key] = now


def _int_body(value: Any, field: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Поле «{field}» должно быть целым числом.") from None


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc) or "Недостаточно прав.")
    return HTTPException(status_code=400, detail=str(exc) or "Действие невозможно.")


def prepare_avatar(raw: bytes) -> bytes:
    """Проверяет изображение и нормализует в JPEG <= 512 px (как в Mini App 3.4)."""
    from io import BytesIO

    from PIL import Image, ImageOps

    with Image.open(BytesIO(raw)) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "black")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        image.thumbnail((AVATAR_MAX_SIDE, AVATAR_MAX_SIDE), Image.Resampling.LANCZOS)
        out = BytesIO()
        image.save(out, format="JPEG", quality=90, optimize=True, progressive=True)
        return out.getvalue()


def save_avatar_file(team_id: int, data: bytes) -> None:
    TEAM_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    path = TEAM_AVATAR_DIR / f"team_{int(team_id)}.jpg"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


async def _my_team(session, user_id: int) -> Team | None:
    return await TeamRepository(session).get_for_user(user_id)


def build_user_router() -> APIRouter:
    router = APIRouter(prefix="/api/v2", route_class=SafeRoute)

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    @router.get("/health")
    async def health():
        return {"ok": True, "service": "TI:IT TOP Mini App", "version": VERSION}

    @router.get("/me")
    async def me(x_telegram_init_data: str | None = Header(default=None)):
        """Минимальный стартовый запрос: кто я, есть ли команда, счётчики."""
        tg = parse_telegram_user(x_telegram_init_data or "")
        viewer = await resolve_viewer(tg, refresh=True)
        now = utcnow()
        async with session_scope() as session:
            profile = await ProfileRepository(session).get_by_user_id(viewer.user_id)
            team = await _my_team(session, viewer.user_id)
            unread = int(await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == viewer.user_id, Notification.is_read.is_(False)
                )
            ) or 0)
            invites = int(await session.scalar(
                select(func.count(TeamInvite.id)).where(
                    TeamInvite.invitee_user_id == viewer.user_id,
                    TeamInvite.status == TeamInviteStatus.PENDING,
                    TeamInvite.expires_at > now,
                )
            ) or 0)
        from miniapp_v2.routes_records import count_records as _records_count
        from services.audience import TWITCH_CHANNEL, TWITCH_URL, follows as _follows, kind_of

        async with session_scope() as _s:
            _kind, _chosen = await kind_of(_s, viewer.user_id)
            _fl = await _follows(_s, viewer.user_id)
        return {
            "version": VERSION,
            "user": {
                "telegram_id": viewer.telegram_id,
                "user_id": viewer.user_id,
                "username": viewer.username,
                "first_name": viewer.first_name,
                "last_name": viewer.last_name,
                "name": display_name(viewer),
                "avatar_url": player_avatar_url(viewer.user_id),
            },
            "kind": _kind or "PLAYER",
            "kind_chosen": bool(_chosen) or viewer.is_admin,
            "follows": _fl,
            "stream": {"channel": TWITCH_CHANNEL, "url": TWITCH_URL, "records": _records_count()},
            "registered": viewer.registered,
            "is_admin": viewer.is_admin,
            "steam_linked": bool(profile and profile.steam_id64),
            "verification": verification_state(profile),
            "mmr": mmr_facts(profile),
            "team": {
                "id": int(team.id),
                "name": team.name,
                "is_captain": int(team.captain_user_id) == viewer.user_id,
            } if team else None,
            "counts": {"unread_notifications": unread, "pending_invites": invites},
        }

    # ------------------------------------------------------------------
    # Профиль / Steam / MMR
    # ------------------------------------------------------------------
    @router.get("/profile")
    async def profile(viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            p = await ProfileRepository(session).get_by_user_id(viewer.user_id)
            team = await _my_team(session, viewer.user_id)
            rank_hint = None
            if p is not None and p.medal_rank is not None:
                rr = await session.scalar(select(RankRange).where(RankRange.rank_tier == int(p.medal_rank)))
                if rr is not None:
                    rank_hint = {"mmr_min": rr.mmr_min, "mmr_max": rr.mmr_max}
        steam_id = p.steam_id64 if p else None
        account_id = p.account_id if p else None
        from services.player_checks import latest_checks

        check = (await latest_checks([viewer.user_id], admin=False)).get(viewer.user_id)
        return {
            "user": {"user_id": viewer.user_id, "telegram_id": viewer.telegram_id, "name": display_name(viewer),
                     "username": viewer.username, "avatar_url": player_avatar_url(viewer.user_id)},
            "medal_check": check,
            "registered": viewer.registered,
            "steam": {
                "linked": bool(steam_id),
                "steam_id64": steam_id,
                "account_id": account_id,
                "nickname": p.steam_nickname if p else None,
                "links": {
                    "steam": f"https://steamcommunity.com/profiles/{steam_id}" if steam_id else None,
                    "dotabuff": f"https://www.dotabuff.com/players/{account_id}" if account_id else None,
                    "opendota": f"https://www.opendota.com/players/{account_id}" if account_id else None,
                    "stratz": f"https://stratz.com/players/{account_id}" if account_id else None,
                },
            },
            "verification": {
                "status": verification_state(p),
                "source": p.verification_source if p else None,
                "verified_at": iso(p.verified_at) if p else None,
                "admin_comment": p.admin_comment if p else None,
            },
            "mmr": mmr_facts(p),
            "mmr_limits": {"min": 0, "max": 9500},
            "medal": medal_facts(p),
            "medal_mmr_range": rank_hint,
            "stats": stats_facts(p),
            "positions": parse_positions(p.preferred_positions if p else None),
            "team": {"id": int(team.id), "name": team.name, "is_captain": int(team.captain_user_id) == viewer.user_id} if team else None,
        }

    async def _require_player(viewer: Viewer) -> None:
        """5.9: действия участника недоступны зрителю."""
        from services.audience import VIEWER_BLOCK, is_viewer

        if not viewer.is_admin and await is_viewer(viewer.user_id):
            raise HTTPException(status_code=403, detail=VIEWER_BLOCK)

    @router.post("/me/kind")
    async def me_kind(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        from services.audience import set_kind

        async with session_scope() as session:
            try:
                kind = await set_kind(session, viewer.user_id, str(payload.get("kind") or ""))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            await session.commit()
        return {"ok": True, "kind": kind}

    @router.post("/teams/{team_id}/follow")
    async def team_follow(team_id: int, payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        from services.audience import set_follow

        async with session_scope() as session:
            if await session.get(Team, int(team_id)) is None:
                raise HTTPException(status_code=404, detail="Команда не найдена.")
            await set_follow(session, viewer.user_id, int(team_id), bool(payload.get("follow")))
            await session.commit()
        return {"ok": True, "follow": bool(payload.get("follow"))}

    @router.post("/profile/mmr")
    async def profile_mmr(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        await _require_player(viewer)
        mmr = _int_body(payload.get("mmr"), "MMR")
        if not 0 <= mmr <= 9500:
            raise HTTPException(status_code=400, detail="MMR должен быть от 0 до 9500.")
        _rate_limit("mmr", viewer.user_id, 5, "Подождите несколько секунд перед повторной отправкой MMR.")
        async with session_scope() as session:
            existing = await ProfileRepository(session).get_by_user_id(viewer.user_id)
            if existing is None or not existing.steam_id64:
                raise HTTPException(status_code=400, detail="Сначала привяжите Steam-аккаунт.")
            ok, error, rank = await RegistrationService(session).save_exact_mmr(viewer.user_id, mmr)
            if not ok:
                raise HTTPException(status_code=400, detail=error or "Не удалось сохранить MMR.")
            p = await ProfileRepository(session).get_by_user_id(viewer.user_id)
            profile_id = int(p.id)
            steam_id = p.steam_id64
        rank_text = f"{rank.rank_name} {rank.star} ({rank.mmr_min}–{rank.mmr_max})" if rank else "диапазон не определён"
        admin_text = (
            "🔎 <b>Новая проверка MMR (Mini App)</b>\n\n"
            f"👤 Telegram: @{html_escape(viewer.username or viewer.telegram_id)}\n"
            f"🆔 Telegram ID: <code>{viewer.telegram_id}</code>\n"
            f"🎮 Steam ID64: <code>{html_escape(steam_id or '—')}</code>\n"
            f"🎯 Заявленный MMR: <b>{mmr}</b>\n"
            f"🏅 Медаль OpenDota: <b>{html_escape(rank_text)}</b>\n\n"
            "Проверьте MMR на аккаунте и подтвердите или отклоните."
        )
        try:
            from keyboards.admin import verification_kb

            markup = verification_kb(profile_id)
        except Exception:  # noqa: BLE001
            markup = None
        notify.notify_admins(get_config(), admin_text, markup, label="mmr_submitted")
        try:
            from services.player_checks import schedule_player_check

            schedule_player_check(get_config(), viewer.user_id, trigger="miniapp_mmr", use_ai=False)
        except Exception:  # noqa: BLE001
            logger.exception("[MINIAPP_V2] player check was not scheduled")
        return {"ok": True, "mmr": mmr, "message": "MMR отправлен на проверку организатору."}

    @router.post("/profile/positions")
    async def profile_positions(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        raw = payload.get("positions")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="Передайте список позиций.")
        clean = sorted({_int_body(x, "позиция") for x in raw})
        if any(x not in POSITIONS for x in clean):
            raise HTTPException(status_code=400, detail="Позиции должны быть от 1 до 5.")
        async with session_scope() as session:
            await TeamService(session).set_preferred_positions(viewer.user_id, clean)
        invalidate_public("teams:", "team:")
        return {"ok": True, "positions": clean}

    @router.post("/profile/sources/refresh")
    async def profile_sources_refresh(viewer: Viewer = Depends(viewer_dep)):
        """Обновить медаль по источникам (OpenDota/STRATZ) и аватар. Без AI, раз в 10 минут."""
        from services.player_checks import run_player_check

        _rate_limit("sources", viewer.user_id, 600, "Данные источников можно обновлять раз в 10 минут.")
        try:
            result = await asyncio.wait_for(
                run_player_check(get_config(), viewer.user_id, trigger="user", use_ai=False), timeout=40
            )
        except ValueError as exc:
            _rate.pop(("sources", viewer.user_id), None)
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Источники не ответили вовремя. Попробуйте позже.") from None
        public = {k: result.get(k) for k in ("id", "created_at", "sources", "medal")}
        return {"ok": True, "medal_check": public, "avatar_url": player_avatar_url(viewer.user_id)}

    @router.post("/profile/steam")
    async def profile_steam(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        """Привязка или перепривязка Steam (тот же каскад проверок, что и в боте)."""
        raw = str(payload.get("steam") or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="Укажите Steam ID64, ссылку на профиль или Dota Account ID.")
        if len(raw) > 300:
            raise HTTPException(status_code=400, detail="Слишком длинная строка.")
        _rate_limit("steam", viewer.user_id, 15, "Проверка Steam уже выполнялась недавно. Подождите 15 секунд.")

        from services.factory import get_services

        try:
            result = await asyncio.wait_for(get_services().verification.verify_input(raw), timeout=STEAM_CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Сервисы проверки Steam не ответили вовремя. Попробуйте позже.") from None
        if result.status == "INVALID_ID" or not result.is_valid_identity:
            raise HTTPException(status_code=400, detail=result.error or "Не удалось подтвердить указанный Steam-аккаунт.")
        if not result.steam_id64 or result.account_id is None:
            raise HTTPException(status_code=400, detail="Не удалось определить Steam ID64 и Dota Account ID. Укажите Steam ID64 или ссылку /profiles/.")

        config = get_config()
        async with session_scope() as session:
            profiles = ProfileRepository(session)
            current = await profiles.get_by_user_id(viewer.user_id)
            other = await session.scalar(
                select(PlayerProfile.id).where(
                    PlayerProfile.steam_id64 == result.steam_id64,
                    PlayerProfile.user_id != viewer.user_id,
                ).limit(1)
            )
            if other is not None:
                raise HTTPException(status_code=409, detail="Этот Steam-аккаунт уже привязан к другому профилю Telegram.")
            if current is not None and current.steam_id64 == result.steam_id64:
                return {"ok": True, "changed": False, "message": "Этот Steam-аккаунт уже привязан к вашему профилю."}
            first_link = current is None or not current.steam_id64

            async def _notify(card: str) -> None:
                try:
                    from keyboards.admin import verification_kb

                    prof = await ProfileRepository(session).get_by_user_id(viewer.user_id)
                    markup = verification_kb(prof.id) if prof else None
                except Exception:  # noqa: BLE001
                    markup = None
                notify.notify_admins(config, card, markup, label="steam_linked")

            status = await RegistrationService(session).apply_verification(
                user_id=viewer.user_id,
                telegram_id=viewer.telegram_id,
                username=viewer.username,
                result=result,
                notify_admins_callback=_notify,
                force_pending=True,
            )
            user = await session.get(User, viewer.user_id)
            if user is not None:
                user.is_registered = True
            await add_event(
                session,
                kind="steam_linked" if first_link else "steam_rebind",
                title="Привязан Steam-аккаунт" if first_link else "Перепривязан Steam-аккаунт",
                body=display_name(viewer),
                actor_user_id=viewer.user_id,
            )
            await session.commit()
        try:
            from services.player_checks import schedule_player_check

            schedule_player_check(config, viewer.user_id, trigger="miniapp_steam", use_ai=False, notify_admins=False)
        except Exception:  # noqa: BLE001
            logger.exception("[MINIAPP_V2] player check was not scheduled")
        return {
            "ok": True,
            "changed": True,
            "first_link": first_link,
            "status": status.value,
            "steam_id64": result.steam_id64,
            "account_id": result.account_id,
            "nickname": result.nickname,
            "message": (
                "Steam привязан. Теперь укажите точный MMR — его подтвердит организатор."
                if first_link else
                "Steam перепривязан. MMR нужно указать и подтвердить заново."
            ),
        }

    # ------------------------------------------------------------------
    # Моя команда
    # ------------------------------------------------------------------
    @router.get("/team")
    async def my_team(viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            incoming = await pending_invites_for_user(session, viewer.user_id)
            team = await _my_team(session, viewer.user_id)
            if team is None:
                return {"team": None, "incoming_invites": incoming, "max_roster": MAX_ROSTER_SIZE}
            roster = await load_roster(session, team, viewer.user_id)
            applications = await team_applications(session, team.id)
            is_captain = int(team.captain_user_id) == viewer.user_id
            outgoing = await outgoing_invites_for_team(session, team.id) if is_captain else []
            team_id, team_name, role_mode, captain_id = int(team.id), team.name, team.role_mode, int(team.captain_user_id)
            from services.roster import frozen_tournament

            frozen = await frozen_tournament(session, team_id)
            frozen_name = frozen.name if frozen else None
        description = await read_team_description(team_id)
        has_approved = any(a["status"] == ApplicationStatus.APPROVED.value for a in applications)
        if role_mode == "CAPTAIN_CHOICE":
            positions_edit = "all" if is_captain else "none"
        else:
            positions_edit = "self"
        return {
            "team": {
                "id": team_id,
                "name": team_name,
                "description": description,
                "captain_user_id": captain_id,
                "is_captain": is_captain,
                "role_mode": role_mode,
                "positions_edit": positions_edit,
                "avatar_url": avatar_url(team_id),
                "roster": roster,
                **roster_summary(roster),
                "min_to_apply": MIN_PLAYERS_TO_APPLY,
                "applications": applications,
                "outgoing_invites": outgoing,
                "can_delete": bool(is_captain and not has_approved),
                "delete_block_reason": (
                    "Команда допущена к турниру — для расформирования обратитесь к организатору."
                    if is_captain and has_approved else None
                ),
                "can_leave": not is_captain,
                "can_rename": bool(is_captain and not has_approved),
                "roster_frozen": (f"Регистрация на «{frozen_name}» закрыта — состав заморожен. Вход, уход и исключение игроков "
                                  "идут через организаторов: запрос отправится им автоматически.") if frozen_name else None,
            },
            "incoming_invites": incoming,
            "max_roster": MAX_ROSTER_SIZE,
        }

    @router.post("/team")
    async def create_team(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        await _require_player(viewer)
        name = str(payload.get("name") or "")
        description = payload.get("description")
        async with session_scope() as session:
            try:
                team = await TeamService(session).create_team(viewer.user_id, name)
                if description:
                    await TeamService(session).update_info(team.id, viewer.user_id, description=str(description))
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
            await add_event(session, kind="team_created", title="Создана команда", body=team.name, actor_user_id=viewer.user_id)
            await session.commit()
            result = {"ok": True, "team": {"id": int(team.id), "name": team.name}}
        invalidate_public("teams:", "team:")
        return result

    @router.patch("/team")
    async def edit_team(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        name = payload.get("name")
        description = payload.get("description")
        if name is None and description is None:
            raise HTTPException(status_code=400, detail="Нечего сохранять.")
        async with session_scope() as session:
            team = await _my_team(session, viewer.user_id)
            if team is None:
                raise HTTPException(status_code=404, detail="Вы не состоите в команде.")
            old_name = team.name
            try:
                team = await TeamService(session).update_info(
                    team.id, viewer.user_id,
                    name=str(name) if name is not None else None,
                    description=str(description) if description is not None else None,
                )
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
            if team.name != old_name:
                await add_event(session, kind="team_renamed", title="Команда переименована", body=f"{old_name} → {team.name}", actor_user_id=viewer.user_id)
                await session.commit()
            result = {"ok": True, "team": {"id": int(team.id), "name": team.name}}
        invalidate_public("teams:", "team:", "tournaments:")
        return result

    @router.post("/team/role-mode")
    async def role_mode(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        mode = str(payload.get("mode") or "")
        if mode not in ROLE_MODES:
            raise HTTPException(status_code=400, detail="Неизвестный режим назначения позиций.")
        async with session_scope() as session:
            team = await _my_team(session, viewer.user_id)
            if team is None:
                raise HTTPException(status_code=404, detail="Вы не состоите в команде.")
            try:
                await TeamService(session).set_role_mode(team.id, viewer.user_id, mode)
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
        invalidate_public("teams:", "team:")
        return {"ok": True, "mode": mode}

    @router.post("/team/members/{target_id}/position")
    async def member_position(target_id: int, payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        position = _int_body(payload.get("position"), "позиция")
        async with session_scope() as session:
            team = await _my_team(session, viewer.user_id)
            if team is None:
                raise HTTPException(status_code=404, detail="Вы не состоите в команде.")
            try:
                await TeamService(session).set_position(team.id, int(target_id), position, viewer.user_id)
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
        invalidate_public("team:")
        return {"ok": True, "user_id": int(target_id), "position": position}

    @router.post("/team/members/{target_id}/coach")
    async def member_coach(target_id: int, payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        """5.5: капитан назначает тренера (один на команду) или возвращает его в игроки."""
        make_coach = bool(payload.get("coach"))
        async with session_scope() as session:
            team = await _my_team(session, viewer.user_id)
            if team is None:
                raise HTTPException(status_code=404, detail="Вы не состоите в команде.")
            if int(team.captain_user_id) != viewer.user_id:
                raise HTTPException(status_code=403, detail="Тренера назначает капитан.")
            if int(target_id) == int(team.captain_user_id):
                raise HTTPException(status_code=400, detail="Капитан не может быть тренером.")
            member = await session.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == int(target_id)))
            if member is None:
                raise HTTPException(status_code=404, detail="Игрок не в вашей команде.")
            if make_coach:
                other = await session.scalar(select(TeamMember).where(
                    TeamMember.team_id == team.id, TeamMember.role == TeamRole.COACH, TeamMember.user_id != int(target_id)))
                if other is not None:
                    raise HTTPException(status_code=400, detail="В команде уже есть тренер — сначала верните его в игроки.")
                member.role, member.position = TeamRole.COACH, None
            else:
                players = int(await session.scalar(select(func.count(TeamMember.id)).where(
                    TeamMember.team_id == team.id, TeamMember.role != TeamRole.COACH)) or 0)
                if players >= MAX_ROSTER_SIZE:
                    raise HTTPException(status_code=400, detail=f"В составе уже {MAX_ROSTER_SIZE} игроков — тренер не может стать игроком.")
                member.role = TeamRole.PLAYER
            await add_event(session, kind="team_coach", title="Тренер команды" if make_coach else "Тренер снова игрок",
                            body=team.name, target_user_id=int(target_id))
            await session.commit()
        invalidate_public("team:", "teams:", "tournaments:")
        return {"ok": True, "user_id": int(target_id), "coach": make_coach}

    @router.delete("/team/members/{target_id}")
    async def kick_member(target_id: int, viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            team = await _my_team(session, viewer.user_id)
            if team is None:
                raise HTTPException(status_code=404, detail="Вы не состоите в команде.")
            team_name = team.name
            try:
                await TeamService(session).kick_member(team.id, viewer.user_id, int(target_id))
            except RosterFrozen as exc:
                return {"ok": True, "pending": True, "message": str(exc)}
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
            # Только личное уведомление: исключение — не публичное событие ленты.
            session.add(Notification(
                user_id=int(target_id), kind="team_member_removed", title="Вы исключены из команды",
                body=f"Капитан исключил вас из команды «{team_name}».",
            ))
            await session.commit()
        invalidate_public("teams:", "team:")
        return {"ok": True}

    @router.post("/team/leave")
    async def leave_team(viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            try:
                team = await TeamService(session).leave_team(viewer.user_id)
            except RosterFrozen as exc:
                return {"ok": True, "pending": True, "message": str(exc)}
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
            captain = await session.get(User, team.captain_user_id)
            await add_event(
                session, kind="team_member_left", title="Игрок покинул команду",
                body=f"{display_name(viewer)} покинул команду «{team.name}».",
                actor_user_id=viewer.user_id, target_user_id=int(team.captain_user_id),
            )
            await session.commit()
            captain_tg = captain.telegram_id if captain else None
            team_name = team.name
        notify.notify_user(
            get_config(), captain_tg,
            f"👋 Игрок {html_escape(display_name(viewer))} покинул команду <b>{html_escape(team_name)}</b>.",
            label="team_leave",
        )
        invalidate_public("teams:", "team:")
        return {"ok": True, "team_name": team_name}

    @router.post("/team/delete")
    async def delete_team(payload: dict = Body(default={}), viewer: Viewer = Depends(viewer_dep)):
        if payload.get("confirm") is not True:
            raise HTTPException(status_code=400, detail="Подтвердите расформирование команды.")
        async with session_scope() as session:
            team = await _my_team(session, viewer.user_id)
            if team is None:
                raise HTTPException(status_code=404, detail="Команда не найдена.")
            team_name = team.name
            try:
                member_ids = await TeamService(session).delete_team(team.id, viewer.user_id)
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
            for member_id in member_ids:
                if int(member_id) != viewer.user_id:
                    session.add(Notification(
                        user_id=int(member_id), kind="team_deleted", title="Команда расформирована",
                        body=f"Команда «{team_name}» расформирована капитаном. Вы снова свободный игрок.",
                    ))
            await add_event(session, kind="team_deleted", title="Команда расформирована", body=team_name, actor_user_id=viewer.user_id)
            await session.commit()
        invalidate_public("teams:", "team:", "tournaments:")
        return {"ok": True, "team_name": team_name}

    @router.post("/team/avatar")
    async def upload_avatar(file: UploadFile = File(...), viewer: Viewer = Depends(viewer_dep)):
        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(status_code=400, detail="Аватар должен быть изображением (JPG, PNG, WebP, GIF).")
        raw = await file.read(AVATAR_MAX_BYTES + 1)
        if not raw:
            raise HTTPException(status_code=400, detail="Файл пустой.")
        if len(raw) > AVATAR_MAX_BYTES:
            raise HTTPException(status_code=400, detail="Изображение больше 8 МБ.")
        async with session_scope() as session:
            team = await _my_team(session, viewer.user_id)
            if team is None or int(team.captain_user_id) != viewer.user_id:
                raise HTTPException(status_code=403, detail="Аватар меняет только капитан.")
            team_id = int(team.id)
        try:
            normalized = await asyncio.to_thread(prepare_avatar, raw)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Не удалось прочитать изображение. Используйте JPG, PNG, WebP или GIF.") from None
        await asyncio.to_thread(save_avatar_file, team_id, normalized)
        async with session_scope() as session:
            # file_id сбрасывается: бот покажет локальный файл (handlers/team.py),
            # а загрузка в Telegram не нужна и не задерживает ответ.
            await TeamService(session).set_avatar(team_id, viewer.user_id, None, "local")
        invalidate_public("teams:", "team:", "tournaments:")
        return {"ok": True, "avatar_url": avatar_url(team_id)}

    # ------------------------------------------------------------------
    # Приглашения
    # ------------------------------------------------------------------
    @router.post("/team/invites")
    async def invite(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        target_id = _int_body(payload.get("user_id"), "user_id")
        _rate_limit("invite", viewer.user_id, 1, "Не так быстро.")
        async with session_scope() as session:
            team = await _my_team(session, viewer.user_id)
            if team is None or int(team.captain_user_id) != viewer.user_id:
                raise HTTPException(status_code=403, detail="Приглашать игроков может только капитан.")
            try:
                inv = await TeamService(session).create_invite(team.id, viewer.user_id, target_id)
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
            await add_event(
                session, kind="team_invite", title="Новое приглашение в команду",
                body=f"Команда: {team.name}", actor_user_id=viewer.user_id, target_user_id=target_id,
            )
            await session.commit()
            invitee = await session.get(User, target_id)
            invite_id, team_name = int(inv.id), team.name
            invitee_tg = invitee.telegram_id if invitee else None
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Принять", callback_data=f"team:invite_accept:{invite_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"team:invite_decline:{invite_id}"),
            ]])
        except Exception:  # noqa: BLE001
            markup = None
        notify.notify_user(
            get_config(), invitee_tg,
            f"✉️ <b>Приглашение в команду</b>\n\nКоманда: <b>{html_escape(team_name)}</b>\n"
            "Вас приглашают в состав. Ответить можно здесь или в Mini App.\n\nСрок действия: 24 часа.",
            markup, label="team_invite",
        )
        return {"ok": True, "invite_id": invite_id}

    @router.delete("/team/invites/{invite_id}")
    async def cancel_invite(invite_id: int, viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            try:
                await TeamService(session).cancel_invite(int(invite_id), viewer.user_id)
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
        return {"ok": True}

    async def _invite_answer(invite_id: int, viewer: Viewer, accept: bool) -> dict[str, Any]:
        async with session_scope() as session:
            svc = TeamService(session)
            try:
                if accept:
                    team = await svc.accept_invite(int(invite_id), viewer.user_id)
                else:
                    invite_row = await session.get(TeamInvite, int(invite_id))
                    team = await session.get(Team, invite_row.team_id) if invite_row else None
                    await svc.decline_invite(int(invite_id), viewer.user_id)
            except RosterFrozen as exc:
                return {"ok": True, "pending": True, "message": str(exc)}
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
            captain = await session.get(User, team.captain_user_id) if team else None
            team_name = team.name if team else "команда"
            await add_event(
                session,
                kind="invite_accepted" if accept else "invite_declined",
                title="Приглашение принято" if accept else "Приглашение отклонено",
                body=f"{display_name(viewer)} → «{team_name}»",
                actor_user_id=viewer.user_id,
                target_user_id=int(captain.id) if captain else None,
            )
            await session.commit()
            captain_tg = captain.telegram_id if captain else None
            team_id = int(team.id) if team else None
        who = f"@{viewer.username}" if viewer.username else display_name(viewer)
        notify.notify_user(
            get_config(), captain_tg,
            (f"✅ Игрок {html_escape(who)} принял приглашение в <b>{html_escape(team_name)}</b>."
             if accept else
             f"❌ Игрок {html_escape(who)} отклонил приглашение в <b>{html_escape(team_name)}</b>."),
            label="invite_answer",
        )
        invalidate_public("teams:", "team:")
        return {"ok": True, "team_id": team_id, "team_name": team_name}

    @router.post("/invites/{invite_id}/accept")
    async def accept_invite(invite_id: int, viewer: Viewer = Depends(viewer_dep)):
        await _require_player(viewer)
        return await _invite_answer(invite_id, viewer, True)

    @router.post("/invites/{invite_id}/decline")
    async def decline_invite(invite_id: int, viewer: Viewer = Depends(viewer_dep)):
        return await _invite_answer(invite_id, viewer, False)

    # ------------------------------------------------------------------
    # Поиск свободных игроков / карточка игрока
    # ------------------------------------------------------------------
    @router.get("/players")
    async def players(
        q: str = Query(default="", max_length=64),
        position: int | None = Query(default=None, ge=1, le=5),
        min_mmr: int | None = Query(default=None, ge=0, le=9500),
        max_mmr: int | None = Query(default=None, ge=0, le=9500),
        sort: str = Query(default="mmr_desc"),
        page: int = Query(default=1, ge=1, le=1000),
        limit: int = Query(default=20, ge=1, le=50),
        viewer: Viewer = Depends(viewer_dep),
    ):
        page, limit, offset = page_params(page, limit)
        confirmed = confirmed_mmr_expr()
        async with session_scope() as session:
            # Правила бота: приглашать можно только свободных игроков с подтверждённым MMR.
            stmt = (
                select(User, PlayerProfile)
                .join(PlayerProfile, PlayerProfile.user_id == User.id)
                .outerjoin(TeamMember, TeamMember.user_id == User.id)
                .where(
                    TeamMember.id.is_(None),
                    User.id != viewer.user_id,
                    confirmed.is_not(None),
                    PlayerProfile.verification_status == VerificationStatus.VERIFIED,
                )
            )
            text = q.strip().lstrip("@").lower()
            if text:
                like = f"%{text}%"
                stmt = stmt.where(or_(
                    func.lower(User.username).like(like),
                    func.lower(User.first_name).like(like),
                    func.lower(User.last_name).like(like),
                    func.lower(PlayerProfile.steam_nickname).like(like),
                ))
            if position is not None:
                stmt = stmt.where(PlayerProfile.preferred_positions.like(f"%{int(position)}%"))
            if min_mmr is not None:
                stmt = stmt.where(confirmed >= int(min_mmr))
            if max_mmr is not None:
                stmt = stmt.where(confirmed <= int(max_mmr))
            total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            if sort == "mmr_asc":
                order = (confirmed.asc(), User.id)
            elif sort == "new":
                order = (User.id.desc(),)
            else:
                order = (confirmed.desc(), User.id)
            rows = (await session.execute(stmt.order_by(*order).limit(limit).offset(offset))).all()

            team = await _my_team(session, viewer.user_id)
            invited: set[int] = set()
            captain_team = None
            if team is not None and int(team.captain_user_id) == viewer.user_id:
                captain_team = {"id": int(team.id), "name": team.name}
                invited = {
                    int(x) for x in (await session.execute(
                        select(TeamInvite.invitee_user_id).where(
                            TeamInvite.team_id == team.id,
                            TeamInvite.status == TeamInviteStatus.PENDING,
                            TeamInvite.expires_at > utcnow(),
                        )
                    )).scalars().all()
                }
                roster_count = int(await session.scalar(select(func.count(TeamMember.id)).where(TeamMember.team_id == team.id, TeamMember.role != TeamRole.COACH)) or 0)
                captain_team["roster_count"] = roster_count
                captain_team["can_invite"] = roster_count < MAX_ROSTER_SIZE
        items = []
        for user, profile in rows:
            item = player_brief(user, profile)
            item["invited"] = int(user.id) in invited
            items.append(item)
        payload = page_payload(items, total, page, limit)
        payload["captain_team"] = captain_team
        return payload

    @router.get("/players/{user_id}")
    async def player_card(user_id: int, viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            user = await session.get(User, int(user_id))
            if user is None:
                raise HTTPException(status_code=404, detail="Игрок не найден.")
            profile = await ProfileRepository(session).get_by_user_id(user.id)
            team = await _my_team(session, user.id)
            my_team = await _my_team(session, viewer.user_id)
            can_invite = False
            invited = False
            if (
                my_team is not None
                and int(my_team.captain_user_id) == viewer.user_id
                and team is None
                and int(user.id) != viewer.user_id
                and mmr_facts(profile)["confirmed"] is not None
            ):
                can_invite = True
                invited = bool(await session.scalar(
                    select(TeamInvite.id).where(
                        TeamInvite.team_id == my_team.id,
                        TeamInvite.invitee_user_id == user.id,
                        TeamInvite.status == TeamInviteStatus.PENDING,
                        TeamInvite.expires_at > utcnow(),
                    ).limit(1)
                ))
        from services.player_checks import latest_checks

        check = (await latest_checks([int(user.id)], admin=False)).get(int(user.id))
        card = player_brief(user, profile)
        card.update({
            "medal_check": check,
            "registered": bool(user.is_registered),
            "steam_nickname": profile.steam_nickname if profile else None,
            "stats": stats_facts(profile),
            "verification": verification_state(profile),
            "team": {"id": int(team.id), "name": team.name, "avatar_url": avatar_url(team.id)} if team else None,
            "is_me": int(user.id) == viewer.user_id,
            "can_invite": can_invite,
            "invited": invited,
        })
        return card

    # ------------------------------------------------------------------
    # Команды (публично для участников)
    # ------------------------------------------------------------------
    @router.get("/teams")
    async def teams(
        q: str = Query(default="", max_length=64),
        page: int = Query(default=1, ge=1, le=1000),
        limit: int = Query(default=20, ge=1, le=50),
        viewer: Viewer = Depends(viewer_dep),
    ):
        page, limit, offset = page_params(page, limit)
        key = f"teams:{q.strip().lower()}:{page}:{limit}"
        cached = public_cache.get(key)
        if cached is not None:
            return cached
        async with session_scope() as session:
            stmt = team_list_statement()
            count_stmt = select(func.count(Team.id))
            text = q.strip().lower()
            if text:
                stmt = stmt.where(func.lower(Team.name).like(f"%{text}%"))
                count_stmt = count_stmt.where(func.lower(Team.name).like(f"%{text}%"))
            total = int(await session.scalar(count_stmt) or 0)
            rows = (await session.execute(stmt.order_by(Team.id.desc()).limit(limit).offset(offset))).all()
        items = [team_row_json(*row) for row in rows]
        return public_cache.set(key, page_payload(items, total, page, limit), PUBLIC_TTL)

    @router.get("/teams/{team_id}")
    async def team_card(team_id: int, viewer: Viewer = Depends(viewer_dep)):
        key = f"team:{int(team_id)}"
        cached = public_cache.get(key)
        if cached is None:
            async with session_scope() as session:
                team = await session.get(Team, int(team_id))
                if team is None:
                    raise HTTPException(status_code=404, detail="Команда не найдена.")
                captain = await session.get(User, team.captain_user_id)
                roster = await load_roster(session, team)
                apps = [a for a in await team_applications(session, team.id) if a["status"] == ApplicationStatus.APPROVED.value]
                data = {
                    "id": int(team.id),
                    "name": team.name,
                    "captain": {"user_id": int(captain.id), "name": display_name(captain), "username": captain.username} if captain else None,
                    "role_mode": team.role_mode,
                    "avatar_url": avatar_url(team.id),
                    "roster": roster,
                    **roster_summary(roster),
                    "tournaments": [{"id": a["tournament_id"], "name": a["tournament_name"], "status": a["tournament_status"]} for a in apps],
                }
            data["description"] = await read_team_description(int(team_id))
            cached = public_cache.set(key, data, PUBLIC_TTL)
        result = dict(cached)
        result["roster"] = [dict(m, is_me=m["user_id"] == viewer.user_id) for m in cached["roster"]]
        result["is_my_team"] = any(m["is_me"] for m in result["roster"])
        return result

    @router.get("/players/{user_id}/avatar", include_in_schema=False)
    async def player_avatar(user_id: int):
        """Только локальная копия аватара игрока. Steam/Telegram не вызываются."""
        path: Path = PLAYER_AVATAR_DIR / f"user_{int(user_id)}.jpg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Аватар не загружен.")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    @router.get("/teams/{team_id}/avatar", include_in_schema=False)
    async def team_avatar(team_id: int):
        """Только локальный файл. Telegram API не вызывается."""
        path: Path = TEAM_AVATAR_DIR / f"team_{int(team_id)}.jpg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Аватар не загружен.")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    # ------------------------------------------------------------------
    # Турниры и заявки
    # ------------------------------------------------------------------
    async def _public_tournaments() -> list[dict[str, Any]]:
        cached = public_cache.get("tournaments:list")
        if cached is not None:
            return cached
        async with session_scope() as session:
            rows = (await session.execute(
                select(Tournament).where(Tournament.status != TournamentStatus.DRAFT).order_by(Tournament.id.desc()).limit(100)
            )).scalars().all()
            counts = await application_counts_by_tournament(session)
            items = [tournament_json(t, counts.get(int(t.id))) for t in rows]
        return public_cache.set("tournaments:list", items, PUBLIC_TTL)

    async def _my_application_map(session, user_id: int) -> tuple[Team | None, dict[int, str]]:
        team = await _my_team(session, user_id)
        if team is None:
            return None, {}
        apps = (await session.execute(
            select(TournamentApplication.tournament_id, TournamentApplication.status)
            .where(TournamentApplication.team_id == team.id)
            .order_by(TournamentApplication.id.desc())
        )).all()
        result: dict[int, str] = {}
        for tid, status in apps:
            result.setdefault(int(tid), status.value if hasattr(status, "value") else str(status))
        return team, result

    def _apply_block_reason(t: dict[str, Any], team: Team | None, is_captain: bool, roster_count: int, my_status: str | None) -> str | None:
        if my_status and my_status != ApplicationStatus.CANCELLED.value:
            return None
        if t["status"] != TournamentStatus.REGISTRATION.value or not t["registration_open"]:
            return "Регистрация закрыта."
        if t["slots_left"] <= 0:
            return "Свободных мест нет."
        if team is None:
            return "Чтобы подать заявку, создайте команду или вступите в неё."
        if not is_captain:
            return "Заявку подаёт капитан команды."
        if roster_count < MIN_PLAYERS_TO_APPLY:
            return f"Нужно минимум {MIN_PLAYERS_TO_APPLY} игроков в составе (сейчас {roster_count})."
        if roster_count > MAX_ROSTER_SIZE:
            return f"В составе больше {MAX_ROSTER_SIZE} игроков."
        return None

    @router.get("/tournaments")
    async def tournaments(viewer: Viewer = Depends(viewer_dep)):
        items = await _public_tournaments()
        async with session_scope() as session:
            team, my_apps = await _my_application_map(session, viewer.user_id)
            roster_count = int(await session.scalar(select(func.count(TeamMember.id)).where(TeamMember.team_id == team.id, TeamMember.role != TeamRole.COACH)) or 0) if team else 0
        is_captain = bool(team and int(team.captain_user_id) == viewer.user_id)
        out = []
        for t in items:
            row = dict(t)
            row["my_application"] = my_apps.get(int(t["id"]))
            reason = _apply_block_reason(t, team, is_captain, roster_count, row["my_application"])
            row["can_apply"] = reason is None and row["my_application"] in (None, ApplicationStatus.CANCELLED.value)
            row["apply_block_reason"] = reason
            out.append(row)
        return {
            "items": out,
            "my_team": {"id": int(team.id), "name": team.name, "is_captain": is_captain, "roster_count": roster_count} if team else None,
        }

    @router.get("/tournaments/{tournament_id}")
    async def tournament_detail(tournament_id: int, viewer: Viewer = Depends(viewer_dep)):
        from services.bracket import public_state

        async with session_scope() as session:
            t = await session.get(Tournament, int(tournament_id))
            if t is None or (t.status == TournamentStatus.DRAFT and not viewer.is_admin):
                raise HTTPException(status_code=404, detail="Турнир не найден.")
            counts = (await application_counts_by_tournament(session)).get(int(t.id))
            base = tournament_json(t, counts)
            stats_rows = (await session.execute(
                team_list_statement()
                .join(TournamentApplication, TournamentApplication.team_id == Team.id)
                .where(
                    TournamentApplication.tournament_id == t.id,
                    TournamentApplication.status == ApplicationStatus.APPROVED,
                )
                .order_by(TournamentApplication.decided_at, TournamentApplication.id)
            )).all()
            approved = [team_row_json(*row) for row in stats_rows]
            try:
                bracket = await public_state(session, t)
            except Exception as exc:  # noqa: BLE001 — сетка не должна ломать страницу турнира
                logger.warning("[MINIAPP_V2] bracket state failed for %s: %s", t.id, exc)
                bracket = {"exists": False, "error": "Сетка временно недоступна."}
            team, my_apps = await _my_application_map(session, viewer.user_id)
            roster_count = int(await session.scalar(select(func.count(TeamMember.id)).where(TeamMember.team_id == team.id, TeamMember.role != TeamRole.COACH)) or 0) if team else 0
        if isinstance(bracket, dict) and bracket.get("teams"):
            for bt in bracket["teams"]:
                bt["avatar_url"] = avatar_url(bt.get("id")) if bt.get("id") else None
        is_captain = bool(team and int(team.captain_user_id) == viewer.user_id)
        my_status = my_apps.get(int(t.id))
        reason = _apply_block_reason(base, team, is_captain, roster_count, my_status)
        return {
            **base,
            "teams": approved,
            "bracket": bracket,
            "my_application": my_status,
            "can_apply": reason is None and my_status in (None, ApplicationStatus.CANCELLED.value),
            "apply_block_reason": reason,
            "my_team": {"id": int(team.id), "name": team.name, "is_captain": is_captain, "roster_count": roster_count} if team else None,
        }

    @router.post("/tournaments/{tournament_id}/apply")
    async def apply(tournament_id: int, viewer: Viewer = Depends(viewer_dep)):
        await _require_player(viewer)
        _rate_limit("apply", viewer.user_id, 2, "Заявка уже отправляется.")
        async with session_scope() as session:
            try:
                app = await ApplicationService(session).submit(viewer.user_id, int(tournament_id))
            except (ValueError, PermissionError) as exc:
                raise _service_error(exc) from None
            tournament = await session.get(Tournament, int(tournament_id))
            team = await session.get(Team, app.team_id)
            await add_event(
                session, kind="application_submitted", title="Новая заявка на турнир",
                body=f"{team.name if team else 'Команда'} → {tournament.name if tournament else 'Турнир'}",
                actor_user_id=viewer.user_id,
            )
            await session.commit()
            app_id, status = int(app.id), app.status.value
            t_name, team_name = (tournament.name if tournament else "?"), (team.name if team else "?")
        captain_label = f"@{viewer.username}" if viewer.username else f"<code>{viewer.telegram_id}</code>"
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 Открыть заявку", callback_data=f"adm:application:{app_id}")
            ]])
        except Exception:  # noqa: BLE001
            markup = None
        notify.notify_admins(
            get_config(),
            f"📋 <b>НОВАЯ ЗАЯВКА #{app_id}</b> (Mini App)\n\n<b>Турнир:</b> {html_escape(t_name)}\n"
            f"<b>Команда:</b> {html_escape(team_name)}\n<b>Капитан:</b> {captain_label}",
            markup, label="application_submitted",
        )
        invalidate_public("tournaments:")
        return {"ok": True, "application_id": app_id, "status": status}

    @router.post("/tournaments/{tournament_id}/withdraw")
    async def withdraw(tournament_id: int, payload: dict = Body(default={}), viewer: Viewer = Depends(viewer_dep)):
        """5.13: капитан снимает команду с турнира."""
        if payload.get("confirm") is not True:
            raise HTTPException(status_code=400, detail="Подтвердите снятие команды с турнира.")
        from services import roster

        async with session_scope() as session:
            team = await _my_team(session, viewer.user_id)
            if team is None or int(team.captain_user_id) != viewer.user_id:
                raise HTTPException(status_code=403, detail="Снять команду с турнира может только капитан.")
            try:
                out = await roster.withdraw(session, int(tournament_id), int(team.id), by_user_id=viewer.user_id,
                                            reason="команда снялась сама")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            await add_event(session, kind="team_withdrawn", title="Команда снялась с турнира",
                            body=f"{out['team_name']} → {out['tournament']}", actor_user_id=viewer.user_id)
            await session.commit()
        cfg = get_config()
        for tg, text in out["messages"]:
            notify.notify_user(cfg, tg, text, label="withdraw")
        notify.notify_admins(cfg, out["admin_text"], label="withdraw")
        invalidate_public("tournaments:", "team:", "teams:")
        return {"ok": True, "message": f"Команда «{out['team_name']}» снята с турнира."}

    # ------------------------------------------------------------------
    # Чат, события, уведомления
    # ------------------------------------------------------------------
    def _chat_json(m: ChatMessage, u: User, viewer: Viewer) -> dict[str, Any]:
        return {
            "id": int(m.id),
            "user_id": int(m.user_id),
            "name": display_name(u),
            "username": u.username,
            "avatar_url": player_avatar_url(m.user_id),
            "text": m.text,
            "created_at": iso(m.created_at),
            "mine": int(m.user_id) == viewer.user_id,
        }

    @router.get("/chat")
    async def chat(
        after_id: int | None = Query(default=None, ge=0),
        before_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=40, ge=1, le=100),
        viewer: Viewer = Depends(viewer_dep),
    ):
        async with session_scope() as session:
            stmt = select(ChatMessage, User).join(User, User.id == ChatMessage.user_id).where(ChatMessage.is_deleted.is_(False))
            if after_id is not None:
                rows = (await session.execute(stmt.where(ChatMessage.id > after_id).order_by(ChatMessage.id.asc()).limit(limit))).all()
            else:
                if before_id is not None:
                    stmt = stmt.where(ChatMessage.id < before_id)
                rows = list(reversed((await session.execute(stmt.order_by(ChatMessage.id.desc()).limit(limit))).all()))
            deleted: list[int] = []
            if after_id is not None:
                # Сообщения, удалённые админом после последнего опроса клиента.
                deleted = [int(x) for x in (await session.execute(
                    select(ChatMessage.id).where(ChatMessage.is_deleted.is_(True)).order_by(ChatMessage.id.desc()).limit(50)
                )).scalars().all()]
        return {
            "items": [_chat_json(m, u, viewer) for m, u in rows],
            "has_more": after_id is None and len(rows) == limit,
            "deleted_ids": deleted,
        }

    @router.post("/chat")
    async def chat_send(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Сообщение пустое.")
        if len(text) > 1000:
            raise HTTPException(status_code=400, detail="Сообщение длиннее 1000 символов.")
        _rate_limit("chat", viewer.user_id, 1.0, "Не так быстро — подождите секунду.")
        async with session_scope() as session:
            msg = ChatMessage(user_id=viewer.user_id, text=text)
            session.add(msg)
            await session.commit()
            user = await session.get(User, viewer.user_id)
            return {"ok": True, "item": _chat_json(msg, user, viewer)}

    @router.delete("/chat/{message_id}")
    async def chat_delete(message_id: int, viewer: Viewer = Depends(viewer_dep)):
        if not viewer.is_admin:
            raise HTTPException(status_code=403, detail="Удалять сообщения могут только администраторы.")
        async with session_scope() as session:
            msg = await session.get(ChatMessage, int(message_id))
            if msg is None or msg.is_deleted:
                raise HTTPException(status_code=404, detail="Сообщение не найдено.")
            msg.is_deleted = True
            msg.deleted_by = viewer.telegram_id
            await session.commit()
        return {"ok": True}

    @router.get("/events")
    async def events(
        before_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=30, ge=1, le=100),
        viewer: Viewer = Depends(viewer_dep),
    ):
        async with session_scope() as session:
            # Приватные виды событий (ответы поддержки с текстом, исключения из команд)
            # остаются только в личных уведомлениях и в публичную ленту не попадают.
            stmt = select(ActivityEvent).where(ActivityEvent.kind.not_in(PRIVATE_EVENT_KINDS))
            if before_id is not None:
                stmt = stmt.where(ActivityEvent.id < before_id)
            rows = (await session.execute(stmt.order_by(ActivityEvent.id.desc()).limit(limit))).scalars().all()
        return {
            "items": [
                {"id": int(e.id), "kind": e.kind, "title": e.title, "body": e.body, "created_at": iso(e.created_at)}
                for e in rows
            ],
            "has_more": len(rows) == limit,
        }

    @router.get("/notifications")
    async def notifications(
        before_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=30, ge=1, le=100),
        viewer: Viewer = Depends(viewer_dep),
    ):
        async with session_scope() as session:
            stmt = select(Notification).where(Notification.user_id == viewer.user_id)
            if before_id is not None:
                stmt = stmt.where(Notification.id < before_id)
            rows = (await session.execute(stmt.order_by(Notification.id.desc()).limit(limit))).scalars().all()
            unread = int(await session.scalar(
                select(func.count(Notification.id)).where(Notification.user_id == viewer.user_id, Notification.is_read.is_(False))
            ) or 0)
        return {
            "items": [
                {"id": int(n.id), "kind": n.kind, "title": n.title, "body": n.body, "read": bool(n.is_read), "created_at": iso(n.created_at)}
                for n in rows
            ],
            "unread": unread,
            "has_more": len(rows) == limit,
        }

    @router.post("/notifications/read-all")
    async def notifications_read_all(viewer: Viewer = Depends(viewer_dep)):
        from sqlalchemy import update

        async with session_scope() as session:
            result = await session.execute(
                update(Notification)
                .where(Notification.user_id == viewer.user_id, Notification.is_read.is_(False))
                .values(is_read=True)
            )
            await session.commit()
        return {"ok": True, "count": int(result.rowcount or 0)}

    @router.post("/notifications/{notification_id}/read")
    async def notification_read(notification_id: int, viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            n = await session.get(Notification, int(notification_id))
            if n is None or int(n.user_id) != viewer.user_id:
                raise HTTPException(status_code=404, detail="Уведомление не найдено.")
            n.is_read = True
            await session.commit()
        return {"ok": True}

    # ------------------------------------------------------------------
    # Поддержка
    # ------------------------------------------------------------------
    @router.get("/support")
    async def support_list(viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            rows = (await session.execute(
                select(SupportTicket).where(SupportTicket.user_id == viewer.user_id).order_by(SupportTicket.id.desc()).limit(30)
            )).scalars().all()
        return {"items": [
            {"id": int(t.id), "status": t.status.value, "message": t.message, "reply": t.admin_reply,
             "created_at": iso(t.created_at), "updated_at": iso(t.updated_at)}
            for t in rows
        ]}

    @router.post("/support")
    async def support_create(payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        message = str(payload.get("message") or "")
        _rate_limit("support", viewer.user_id, 20, "Обращение уже отправлено. Новое можно через 20 секунд.")
        async with session_scope() as session:
            try:
                ticket = await SupportService(session).create_ticket(viewer.user_id, message)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            ticket_id, text = int(ticket.id), ticket.message
        mention = f"@{viewer.username}" if viewer.username else f"user id={viewer.user_id}"
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✉️ Ответить", callback_data=f"sup:reply:{ticket_id}")
            ]])
        except Exception:  # noqa: BLE001
            markup = None
        notify.notify_admins(
            get_config(),
            f"💬 <b>ОБРАЩЕНИЕ В ПОДДЕРЖКУ #{ticket_id}</b> (Mini App)\n\n"
            f"<b>От:</b> {html_escape(mention)} (tg:{viewer.telegram_id})\n\n{html_escape(text)}\n\n"
            f"Ответить: /reply {ticket_id} &lt;текст&gt;",
            markup, label="support_ticket",
        )
        return {"ok": True, "id": ticket_id, "message": f"Обращение №{ticket_id} отправлено организаторам."}

    return router
