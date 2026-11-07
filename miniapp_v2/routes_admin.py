"""Админский API Mini App 5.0 (/api/v2/admin/...).

Разделён по разделам вместо одного /api/admin/overview. Большие списки —
с пагинацией (?page=&limit=). Права проверяются на каждом запросе и не
кэшируются. Внешние сервисы (Steam, AI, Telegram) вызываются только по явным
действиям администратора и с таймаутами.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import String, func, or_, select

from database.engine import session_scope
from database.models import (
    ActivityEvent,
    AdminUser,
    ApplicationStatus,
    BroadcastLog,
    ChatMessage,
    Notification,
    PlayerProfile,
    RankRange,
    SupportTicket,
    Team,
    TeamMember,
    TicketStatus,
    Tournament,
    TournamentApplication,
    TournamentStatus,
    User,
    VerificationRecord,
    VerificationStatus,
)
from database.repositories import ApplicationRepository, ProfileRepository, TeamRepository
from miniapp_v2 import notify
from miniapp_v2.common import (
    TEAM_AVATAR_DIR,
    SafeRoute,
    Viewer,
    admin_dep,
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
    player_avatar_url,
    stats_facts,
    verification_state,
)
from miniapp_v2.queries import (
    application_counts_by_tournament,
    confirmed_mmr_expr,
    load_roster,
    read_team_description,
    roster_summary,
    team_applications,
    team_list_statement,
    team_row_json,
    tournament_json,
)
from services.activity import add_event
from services.registration import RegistrationService
from services.support import SupportService
from services.teams import MAX_ROSTER_SIZE
from services.tournaments import TournamentService

logger = logging.getLogger("MINIAPP_V2.ADMIN")

RECHECK_TIMEOUT = 45.0
PLAYER_CHECK_TIMEOUT = 80.0
DIAGNOSTICS_TIMEOUT = 60.0
AVATAR_SYNC_BUDGET = 40.0

_ai_last: dict[int, float] = {}


def _parse_dt(value: Any, field: str = "Дата") -> datetime | None:
    """ISO с Z/смещением — как есть; время без зоны — московское (часовой пояс проекта, 5.2)."""
    from services.tournament_engine.timeutil import parse_local

    if value in (None, ""):
        return None
    try:
        return parse_local(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field}: неверный формат даты.") from None


def _int(value: Any, field: str, lo: int | None = None, hi: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field}: нужно целое число.") from None
    if lo is not None and number < lo or hi is not None and number > hi:
        raise HTTPException(status_code=400, detail=f"{field}: допустимо от {lo} до {hi}.")
    return number


def _profile_admin_json(p: PlayerProfile | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "id": int(p.id),
        "steam_id64": p.steam_id64,
        "account_id": p.account_id,
        "nickname": p.steam_nickname,
        "status": verification_state(p),
        "source": p.verification_source,
        "admin_comment": p.admin_comment,
        "mmr": mmr_facts(p),
        "medal": medal_facts(p),
        "stats": stats_facts(p),
        "positions": parse_positions(p.preferred_positions),
        "verified_at": iso(p.verified_at),
        "updated_at": iso(p.updated_at),
        "links": {
            "steam": f"https://steamcommunity.com/profiles/{p.steam_id64}" if p.steam_id64 else None,
            "dotabuff": f"https://www.dotabuff.com/players/{p.account_id}" if p.account_id else None,
            "opendota": f"https://www.opendota.com/players/{p.account_id}" if p.account_id else None,
            "stratz": f"https://stratz.com/players/{p.account_id}" if p.account_id else None,
        },
    }


def build_admin_router() -> APIRouter:
    router = APIRouter(prefix="/api/v2/admin", route_class=SafeRoute)

    # ------------------------------------------------------------------
    # Плашка «MVP по выбору игроков» на главном экране (services/mvp_banner.py)
    # ------------------------------------------------------------------
    @router.get("/mvp-banner")
    async def mvp_banner_get(viewer: Viewer = Depends(admin_dep)):
        from services import mvp_banner

        return {"manual": mvp_banner.manual(), "current": await mvp_banner.current()}

    @router.post("/mvp-banner")
    async def mvp_banner_set(payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        from services import mvp_banner

        try:
            mvp_banner.set_manual(str(payload.get("name") or ""), str(payload.get("caption") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"manual": mvp_banner.manual(), "current": await mvp_banner.current()}

    @router.delete("/mvp-banner")
    async def mvp_banner_clear(viewer: Viewer = Depends(admin_dep)):
        from services import mvp_banner

        mvp_banner.clear()
        return {"manual": None, "current": await mvp_banner.current()}

    # ------------------------------------------------------------------
    # Статистика (один запрос со скалярными подзапросами)
    # ------------------------------------------------------------------
    @router.get("/stats")
    async def stats(viewer: Viewer = Depends(admin_dep)):
        def count(model, *where):
            stmt = select(func.count()).select_from(model)
            for clause in where:
                stmt = stmt.where(clause)
            return stmt.scalar_subquery()

        free_agents = (
            select(func.count(User.id))
            .join(PlayerProfile, PlayerProfile.user_id == User.id)
            .outerjoin(TeamMember, TeamMember.user_id == User.id)
            .where(
                TeamMember.id.is_(None),
                confirmed_mmr_expr().is_not(None),
                PlayerProfile.verification_status == VerificationStatus.VERIFIED,
            )
            .scalar_subquery()
        )
        labels = [
            "users", "registered", "teams", "free_agents", "tournaments", "tournaments_open",
            "applications_pending", "applications_approved", "verification_pending",
            "verified_players", "rejected_players", "support_open", "chat_messages", "events",
        ]
        async with session_scope() as session:
            row = (await session.execute(select(
                count(User),
                count(User, User.is_registered.is_(True)),
                count(Team),
                free_agents,
                count(Tournament),
                count(Tournament, Tournament.status == TournamentStatus.REGISTRATION, Tournament.registration_open.is_(True)),
                count(TournamentApplication, TournamentApplication.status == ApplicationStatus.PENDING),
                count(TournamentApplication, TournamentApplication.status == ApplicationStatus.APPROVED),
                count(PlayerProfile, PlayerProfile.verification_status == VerificationStatus.PENDING),
                count(PlayerProfile, PlayerProfile.verification_status == VerificationStatus.VERIFIED),
                count(PlayerProfile, PlayerProfile.verification_status == VerificationStatus.REJECTED),
                count(SupportTicket, SupportTicket.status == TicketStatus.OPEN),
                count(ChatMessage, ChatMessage.is_deleted.is_(False)),
                count(ActivityEvent),
            ))).one()
        config = get_config()
        return {
            "counts": {label: int(value or 0) for label, value in zip(labels, row)},
            "mmr_range_hard_block": bool(config.mmr_rank_range_hard_block),
            "telegram_notifications": not notify.telegram_disabled(),
            "broadcast_running": bool(notify.broadcast_state.get("running")),
        }

    # ------------------------------------------------------------------
    # Проверка игроков (MMR)
    # ------------------------------------------------------------------
    @router.get("/verification")
    async def verification_queue(
        page: int = Query(default=1, ge=1, le=1000),
        limit: int = Query(default=20, ge=1, le=50),
        viewer: Viewer = Depends(admin_dep),
    ):
        page, limit, offset = page_params(page, limit)
        async with session_scope() as session:
            base = select(PlayerProfile, User).join(User, User.id == PlayerProfile.user_id).where(
                PlayerProfile.verification_status == VerificationStatus.PENDING
            )
            total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
            rows = (await session.execute(base.order_by(PlayerProfile.updated_at).limit(limit).offset(offset))).all()
            tiers = {int(p.medal_rank) for p, _ in rows if p.medal_rank is not None}
            ranges = {}
            if tiers:
                ranges = {
                    int(r.rank_tier): {"mmr_min": r.mmr_min, "mmr_max": r.mmr_max}
                    for r in (await session.execute(select(RankRange).where(RankRange.rank_tier.in_(tiers)))).scalars().all()
                }
        from services.player_checks import latest_checks

        checks = await latest_checks([int(u.id) for _, u in rows], admin=True)
        items = []
        for p, u in rows:
            item = _profile_admin_json(p)
            item.update({
                "profile_id": int(p.id),
                "user": {"user_id": int(u.id), "telegram_id": u.telegram_id, "name": display_name(u), "username": u.username,
                         "avatar_url": player_avatar_url(u.id)},
                "medal_mmr_range": ranges.get(int(p.medal_rank)) if p.medal_rank is not None else None,
                "check": checks.get(int(u.id)),
            })
            items.append(item)
        return page_payload(items, total, page, limit)

    # ------------------------------------------------------------------
    # 5.1: проверка игрока (источники медали, риск смурфа/твинка, AI)
    # ------------------------------------------------------------------
    @router.post("/players/{user_id}/check")
    async def player_check(user_id: int, payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        from services.player_checks import run_player_check

        use_ai = payload.get("use_ai", True) not in (False, "0", 0, "false")
        try:
            result = await asyncio.wait_for(
                run_player_check(get_config(), int(user_id), trigger="admin", use_ai=use_ai, admin_tg=viewer.telegram_id),
                timeout=PLAYER_CHECK_TIMEOUT,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Источники или AI не ответили вовремя. Повторите позже.") from None
        invalidate_public("teams:", "team:", "players:")
        from services import ai_budget

        return {"ok": True, "check": result, "ai_left": ai_budget.check_remaining()}

    # ------------------------------------------------------------------
    # 5.7: блокировка доступа к боту и Mini App
    # ------------------------------------------------------------------
    @router.post("/users/{user_id}/ban")
    async def user_ban(user_id: int, payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        from config import is_admin as _is_admin
        from services import bans

        async with session_scope() as session:
            u = await session.get(User, int(user_id))
            if u is None:
                raise HTTPException(status_code=404, detail="Пользователь не найден.")
            if _is_admin(int(u.telegram_id), get_config()):
                raise HTTPException(status_code=400, detail="Администратора заблокировать нельзя — сначала снимите права.")
            reason = str(payload.get("reason") or "").strip()[:300] or None
            await bans.ban(session, u, reason=reason, admin_telegram_id=viewer.telegram_id)
            await add_event(session, kind="user_banned", title="Пользователь заблокирован",
                            body=f"{display_name(u)}" + (f": {reason}" if reason else ""), actor_user_id=viewer.user_id)
            await session.commit()
            tg = int(u.telegram_id)
        notify.notify_user(get_config(), tg, bans.BLOCKED_TEXT + (f"\nПричина: {html_escape(reason)}" if reason else ""), label="ban")
        invalidate_public("")
        return {"ok": True}

    @router.delete("/users/{user_id}/ban")
    async def user_unban(user_id: int, viewer: Viewer = Depends(admin_dep)):
        from services import bans

        async with session_scope() as session:
            u = await session.get(User, int(user_id))
            if u is None:
                raise HTTPException(status_code=404, detail="Пользователь не найден.")
            removed = await bans.unban(session, u.id)
            await session.commit()
            tg = int(u.telegram_id)
        if removed:
            notify.notify_user(get_config(), tg, "Доступ к TI:IT TOP восстановлен организатором.", label="unban")
        invalidate_public("")
        return {"ok": True, "removed": removed}

    @router.get("/bans")
    async def bans_list(viewer: Viewer = Depends(admin_dep)):
        from database.models import UserBan

        async with session_scope() as session:
            rows = (await session.execute(
                select(UserBan, User).join(User, User.id == UserBan.user_id).order_by(UserBan.id.desc())
            )).all()
        return {"items": [{
            "user_id": int(u.id), "name": display_name(u), "username": u.username, "telegram_id": u.telegram_id,
            "avatar_url": player_avatar_url(u.id), "reason": b.reason, "banned_by": b.banned_by, "created_at": iso(b.created_at),
        } for b, u in rows]}

    @router.get("/players/suspicious")
    async def players_suspicious(level: str = Query(default="risky"), viewer: Viewer = Depends(admin_dep)):
        """5.6: последняя проверка каждого игрока; по умолчанию — только средний и высокий риск."""
        from database.models import PlayerCheck
        from services.player_checks import SWEEP, check_json, users_to_check

        async with session_scope() as session:
            last = select(PlayerCheck.user_id, func.max(PlayerCheck.id).label("mid")).group_by(PlayerCheck.user_id).subquery()
            stmt = (select(PlayerCheck, User, Team.name)
                    .join(last, PlayerCheck.id == last.c.mid)
                    .join(User, User.id == PlayerCheck.user_id)
                    .outerjoin(TeamMember, TeamMember.user_id == User.id)
                    .outerjoin(Team, Team.id == TeamMember.team_id))
            rows_all = (await session.execute(stmt)).all()
        counts = {"checked": len(rows_all), "high": 0, "medium": 0, "low": 0}
        for chk, _, _ in rows_all:
            if chk.risk_level in counts:
                counts[chk.risk_level] += 1
        wanted = {"risky": ("medium", "high"), "high": ("high",)}.get(level)
        rows = [r for r in rows_all if wanted is None or r[0].risk_level in wanted]
        rows.sort(key=lambda r: (-(r[0].risk_score or 0), r[1].id))
        items = [{
            "user": {"user_id": int(u.id), "name": display_name(u), "username": u.username, "avatar_url": player_avatar_url(u.id)},
            "team": team_name,
            "check": check_json(chk, admin=True),
        } for chk, u, team_name in rows[:200]]
        counts["unchecked"] = len(await users_to_check())
        return {"items": items, "counts": counts, "sweep": dict(SWEEP)}

    @router.post("/players/check-all")
    async def players_check_all(payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        from services.player_checks import start_sweep

        age = _int(payload.get("max_age_days", 7), "Дней с прошлой проверки", 0, 365)
        return {"ok": True, "sweep": await start_sweep(get_config(), max_age_days=age)}

    @router.post("/tournaments/{tournament_id}/recheck-rosters")
    async def tournament_recheck(tournament_id: int, viewer: Viewer = Depends(admin_dep)):
        """5.13: свежая проверка рангов всех игроков одобренных команд турнира (AI — только подозрительным)."""
        from services.player_checks import SWEEP, start_sweep, tournament_players

        if SWEEP.get("running"):
            raise HTTPException(status_code=409, detail=f"Уже идёт проверка: {SWEEP['done']} из {SWEEP['total']}.")
        ids = await tournament_players(int(tournament_id))
        if not ids:
            raise HTTPException(status_code=400, detail="В одобренных командах нет игроков с привязанным Steam.")
        return {"ok": True, "sweep": await start_sweep(get_config(), user_ids=ids)}

    @router.get("/players/{user_id}/checks")
    async def player_checks_history(user_id: int, viewer: Viewer = Depends(admin_dep)):
        from database.models import PlayerCheck
        from services.player_checks import check_json

        async with session_scope() as session:
            rows = (await session.execute(
                select(PlayerCheck).where(PlayerCheck.user_id == int(user_id)).order_by(PlayerCheck.id.desc()).limit(10)
            )).scalars().all()
            return {"items": [check_json(r, admin=True) for r in rows]}

    @router.post("/players/avatars/sync")
    async def player_avatars_sync(payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        from services.player_avatars import sync_all

        try:
            result = await asyncio.wait_for(
                sync_all(get_config(), force=bool(payload.get("force")), budget=AVATAR_SYNC_BUDGET),
                timeout=AVATAR_SYNC_BUDGET + 20,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Синхронизация не уложилась во время — повторите, продолжит с места остановки.") from None
        invalidate_public("teams:", "team:", "players:")
        return {"ok": True, **(result if isinstance(result, dict) else {"result": result})}

    @router.post("/verification/{profile_id}")
    async def verification_decision(profile_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"approve", "reject"}:
            raise HTTPException(status_code=400, detail="decision: approve или reject.")
        comment = (str(payload.get("comment")).strip() or None) if payload.get("comment") else None
        async with session_scope() as session:
            profile = await session.get(PlayerProfile, int(profile_id))
            if profile is None:
                raise HTTPException(status_code=404, detail="Профиль не найден.")
            if payload.get("mmr") not in (None, ""):
                profile.exact_mmr = _int(payload.get("mmr"), "MMR", 0, 9500)
            if decision == "approve" and profile.exact_mmr is None:
                raise HTTPException(status_code=400, detail="Для подтверждения нужен точный MMR — впишите его.")
            await RegistrationService(session).set_mmr_decision(
                profile, verified=decision == "approve", admin_id=viewer.telegram_id,
                comment=comment or (None if decision == "approve" else "Проверка точного MMR отклонена организатором."),
            )
            title = "MMR подтверждён" if decision == "approve" else "MMR отклонён"
            await add_event(session, kind="mmr_decision", title=title, body=f"Профиль #{profile.id}", target_user_id=profile.user_id)
            await session.commit()
            user = await session.get(User, profile.user_id)
            status, exact = profile.verification_status.value, profile.exact_mmr
            telegram_id = user.telegram_id if user else None
        text = (
            "✅ <b>Проверка игрока пройдена.</b>\nВаш точный MMR подтверждён организатором."
            if decision == "approve" else
            "❌ <b>Проверка игрока отклонена.</b>\nУказанный MMR не был подтверждён. Обратитесь к организаторам."
        )
        if comment and decision == "reject":
            text += f"\n\nКомментарий: {html_escape(comment)}"
        notify.notify_user(get_config(), telegram_id, text, label="mmr_decision")
        invalidate_public("teams:", "team:", "tournaments:")
        return {"ok": True, "status": status, "exact_mmr": exact}

    @router.post("/users/{user_id}/recheck")
    async def user_recheck(user_id: int, viewer: Viewer = Depends(admin_dep)):
        """Повторная авто-проверка (как «🔄 Повторить проверку» в боте). Турнирный допуск не выдаёт."""
        from services.factory import get_services

        async with session_scope() as session:
            profile = await ProfileRepository(session).get_by_user_id(int(user_id))
            if profile is None or not profile.steam_id64 or profile.account_id is None:
                raise HTTPException(status_code=400, detail="У пользователя нет Steam-данных для проверки.")
            profile_id, sid, aid = int(profile.id), profile.steam_id64, int(profile.account_id)
        try:
            result = await asyncio.wait_for(get_services().verification.recheck(sid, aid), timeout=RECHECK_TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Внешние сервисы не ответили за 45 секунд. Повторите позже.") from None
        async with session_scope() as session:
            p = await session.get(PlayerProfile, profile_id)
            if p is not None:
                from utils.ranks import rank_label_from_tier

                if result.mmr is not None:
                    p.mmr = result.mmr
                if result.rank_name is not None:
                    p.rank_name = result.rank_name
                if result.medal_rank is not None:
                    p.medal_rank = result.medal_rank
                    p.rank_name = rank_label_from_tier(result.medal_rank) or p.rank_name
                if result.leaderboard_rank is not None:
                    p.leaderboard_rank = result.leaderboard_rank
                if result.matches is not None:
                    p.matches = result.matches
                if result.winrate is not None:
                    p.winrate = result.winrate
                if result.hours_total is not None:
                    p.hours_total = result.hours_total
                if result.nickname:
                    p.steam_nickname = result.nickname
                if not p.mmr_verified:
                    p.verification_status = VerificationStatus.PENDING
                    p.verification_source = f"recheck:{result.source}"
                await session.commit()
        return {"ok": True, "status": result.status, "source": result.source, "notes": list(result.notes or [])}

    # ------------------------------------------------------------------
    # Пользователи
    # ------------------------------------------------------------------
    @router.get("/users")
    async def users(
        q: str = Query(default="", max_length=64),
        status: str = Query(default="ALL"),
        page: int = Query(default=1, ge=1, le=10000),
        limit: int = Query(default=20, ge=1, le=50),
        viewer: Viewer = Depends(admin_dep),
    ):
        page, limit, offset = page_params(page, limit)
        async with session_scope() as session:
            stmt = (
                select(User, PlayerProfile, Team)
                .outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
                .outerjoin(TeamMember, TeamMember.user_id == User.id)
                .outerjoin(Team, Team.id == TeamMember.team_id)
            )
            text = q.strip().lstrip("@").lower()
            if text:
                like = f"%{text}%"
                stmt = stmt.where(or_(
                    func.lower(User.username).like(like),
                    func.lower(User.first_name).like(like),
                    func.lower(User.last_name).like(like),
                    func.cast(User.telegram_id, String).like(like),
                    func.lower(PlayerProfile.steam_nickname).like(like),
                    PlayerProfile.steam_id64.like(like),
                ))
            wanted = status.strip().upper()
            if wanted in {"VERIFIED", "PENDING", "REJECTED", "UNVERIFIED"}:
                stmt = stmt.where(PlayerProfile.verification_status == VerificationStatus(wanted))
            elif wanted == "UNLINKED":
                stmt = stmt.where(or_(PlayerProfile.id.is_(None), PlayerProfile.steam_id64.is_(None)))
            total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            rows = (await session.execute(stmt.order_by(User.id.desc()).limit(limit).offset(offset))).all()
        items = [
            {
                "user_id": int(u.id),
                "telegram_id": u.telegram_id,
                "name": display_name(u),
                "username": u.username,
                "registered": bool(u.is_registered),
                "status": verification_state(p),
                "mmr": mmr_facts(p),
                "team": {"id": int(t.id), "name": t.name} if t else None,
                "created_at": iso(u.created_at),
            }
            for u, p, t in rows
        ]
        return page_payload(items, total, page, limit)

    @router.get("/users/{user_id}")
    async def user_detail(user_id: int, viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            u = await session.get(User, int(user_id))
            if u is None:
                raise HTTPException(status_code=404, detail="Пользователь не найден.")
            p = await ProfileRepository(session).get_by_user_id(u.id)
            team = await TeamRepository(session).get_for_user(u.id)
            history = (await session.execute(
                select(VerificationRecord).where(VerificationRecord.user_id == u.id).order_by(VerificationRecord.id.desc()).limit(10)
            )).scalars().all()
            tickets = int(await session.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.user_id == u.id)) or 0)
        config = get_config()
        from services.player_checks import latest_checks

        check = (await latest_checks([int(u.id)], admin=True)).get(int(u.id))
        from database.models import UserBan

        async with session_scope() as session:
            ban_row = await session.scalar(select(UserBan).where(UserBan.user_id == u.id))
        return {
            "ban": {"reason": ban_row.reason, "banned_by": ban_row.banned_by, "created_at": iso(ban_row.created_at)} if ban_row else None,
            "user_id": int(u.id),
            "telegram_id": u.telegram_id,
            "avatar_url": player_avatar_url(u.id),
            "check": check,
            "name": display_name(u),
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "registered": bool(u.is_registered),
            "is_admin": int(u.telegram_id) in config.admin_ids,
            "created_at": iso(u.created_at),
            "profile": _profile_admin_json(p),
            "team": {"id": int(team.id), "name": team.name, "is_captain": int(team.captain_user_id) == int(u.id)} if team else None,
            "support_tickets": tickets,
            "history": [
                {"id": int(r.id), "status": r.status.value, "source": r.source, "performed_by": r.performed_by,
                 "comment": r.comment, "created_at": iso(r.created_at)}
                for r in history
            ],
        }

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------
    @router.get("/teams")
    async def teams(
        q: str = Query(default="", max_length=64),
        page: int = Query(default=1, ge=1, le=1000),
        limit: int = Query(default=20, ge=1, le=50),
        viewer: Viewer = Depends(admin_dep),
    ):
        page, limit, offset = page_params(page, limit)
        async with session_scope() as session:
            stmt = team_list_statement()
            count_stmt = select(func.count(Team.id))
            text = q.strip().lower()
            if text:
                stmt = stmt.where(func.lower(Team.name).like(f"%{text}%"))
                count_stmt = count_stmt.where(func.lower(Team.name).like(f"%{text}%"))
            total = int(await session.scalar(count_stmt) or 0)
            rows = (await session.execute(stmt.order_by(Team.id.desc()).limit(limit).offset(offset))).all()
        items = []
        for row in rows:
            item = team_row_json(*row)
            item["telegram_avatar_only"] = bool(row[0].avatar_file_id) and item["avatar_url"] is None
            items.append(item)
        return page_payload(items, total, page, limit)

    @router.get("/teams/{team_id}")
    async def team_detail(team_id: int, viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            team = await session.get(Team, int(team_id))
            if team is None:
                raise HTTPException(status_code=404, detail="Команда не найдена.")
            captain = await session.get(User, team.captain_user_id)
            roster = await load_roster(session, team)
            apps = await team_applications(session, team.id)
            data = {
                "id": int(team.id),
                "name": team.name,
                "captain": {"user_id": int(captain.id), "name": display_name(captain), "username": captain.username, "telegram_id": captain.telegram_id} if captain else None,
                "role_mode": team.role_mode,
                "avatar_url": avatar_url(team.id),
                "telegram_avatar_only": bool(team.avatar_file_id) and avatar_url(team.id) is None,
                "created_at": iso(team.created_at),
                "roster": roster,
                **roster_summary(roster),
                "applications": apps,
            }
        data["description"] = await read_team_description(int(team_id))
        return data

    @router.delete("/teams/{team_id}/members/{user_id}")
    async def team_remove_member(team_id: int, user_id: int, viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            team = await session.get(Team, int(team_id))
            if team is None:
                raise HTTPException(status_code=404, detail="Команда не найдена.")
            member = await session.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == int(user_id)))
            if member is None:
                raise HTTPException(status_code=404, detail="Игрок не найден в составе.")
            if int(member.user_id) == int(team.captain_user_id):
                raise HTTPException(status_code=400, detail="Нельзя удалить капитана. Команду может расформировать капитан.")
            await session.delete(member)
            # Личное уведомление без публичного события в ленте.
            session.add(Notification(
                user_id=int(user_id), kind="team_member_removed", title="Вы исключены из команды",
                body=f"Организатор исключил вас из команды «{team.name}».",
            ))
            await session.commit()
        invalidate_public("teams:", "team:", "tournaments:")
        return {"ok": True}

    # ------------------------------------------------------------------
    # 5.13: расформирование команды, снятие с турнира, замены в составах, дубли, сводка
    # ------------------------------------------------------------------
    def _send_all(messages, admin_text: str | None, label: str) -> None:
        cfg = get_config()
        for tg, text in messages or []:
            notify.notify_user(cfg, tg, text, label=label)
        if admin_text:
            notify.notify_admins(cfg, admin_text, label=label)

    @router.post("/teams/{team_id}/disband")
    async def team_disband(team_id: int, payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        from services import roster

        if payload.get("confirm") is not True:
            raise HTTPException(status_code=400, detail="Подтвердите расформирование команды.")
        reason = str(payload.get("reason") or "").strip()[:300] or "решение организатора"
        async with session_scope() as session:
            try:
                out = await roster.disband(session, int(team_id), admin_user_id=viewer.user_id, reason=reason)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            for uid in out["members"]:
                session.add(Notification(user_id=int(uid), kind="team_deleted", title="Команда расформирована",
                                         body=f"Организатор расформировал команду «{out['team_name']}». Причина: {reason}."))
            await add_event(session, kind="team_deleted", title="Команда расформирована организатором", body=out["team_name"],
                            actor_user_id=viewer.user_id)
            await session.commit()
        _send_all(out["messages"], out["admin_text"], "disband")
        invalidate_public("teams:", "team:", "tournaments:")
        return {"ok": True, "team_name": out["team_name"]}

    @router.post("/tournaments/{tournament_id}/teams/{team_id}/withdraw")
    async def tournament_withdraw_team(tournament_id: int, team_id: int, payload: dict = Body(default={}),
                                       viewer: Viewer = Depends(admin_dep)):
        from services import roster

        if payload.get("confirm") is not True:
            raise HTTPException(status_code=400, detail="Подтвердите снятие команды.")
        reason = str(payload.get("reason") or "").strip()[:300] or "снята организатором"
        async with session_scope() as session:
            try:
                out = await roster.withdraw(session, int(tournament_id), int(team_id), by_user_id=viewer.user_id, reason=reason, admin=True)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            captain = await session.scalar(select(User).join(Team, Team.captain_user_id == User.id).where(Team.id == int(team_id)))
            await add_event(session, kind="team_withdrawn", title="Команда снята с турнира",
                            body=f"{out['team_name']} → {out['tournament']}", actor_user_id=viewer.user_id)
            await session.commit()
            msgs = list(out["messages"])
            if captain and captain.telegram_id:
                msgs.append((captain.telegram_id, f"Организатор снял вашу команду «{html_escape(out['team_name'])}» с «{html_escape(out['tournament'])}». "
                                                  f"Причина: {html_escape(reason)}."))
        _send_all(msgs, out["admin_text"], "withdraw")
        invalidate_public("teams:", "team:", "tournaments:")
        return {"ok": True, "team_name": out["team_name"]}

    @router.get("/roster-requests")
    async def roster_requests(viewer: Viewer = Depends(admin_dep)):
        from services import roster

        async with session_scope() as session:
            return {"items": await roster.pending(session)}

    @router.post("/roster-requests/{request_id}")
    async def roster_request_decide(request_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        from services import roster

        async with session_scope() as session:
            try:
                out = await roster.decide(session, int(request_id), payload.get("approve") is True, viewer.user_id,
                                          str(payload.get("comment") or "") or None)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            await session.commit()
        _send_all(out["messages"], None, "roster_decision")
        invalidate_public("teams:", "team:", "tournaments:")
        return {"ok": True, "status": out["status"]}

    @router.get("/duplicates")
    async def duplicates(viewer: Viewer = Depends(admin_dep)):
        from services.digest import find_duplicates

        async with session_scope() as session:
            return await find_duplicates(session)

    @router.get("/journal")
    async def journal(viewer: Viewer = Depends(admin_dep)):
        from services.journal import entries

        async with session_scope() as session:
            return {"items": await entries(session)}

    @router.get("/digest")
    async def digest(viewer: Viewer = Depends(admin_dep)):
        from services.digest import build_digest

        return {"html": await build_digest(force=True)}

    # ------------------------------------------------------------------
    # Турниры и сетка
    # ------------------------------------------------------------------
    @router.get("/tournaments")
    async def tournaments(
        page: int = Query(default=1, ge=1, le=1000),
        limit: int = Query(default=20, ge=1, le=50),
        viewer: Viewer = Depends(admin_dep),
    ):
        page, limit, offset = page_params(page, limit)
        async with session_scope() as session:
            total = int(await session.scalar(select(func.count(Tournament.id))) or 0)
            rows = (await session.execute(select(Tournament).order_by(Tournament.id.desc()).limit(limit).offset(offset))).scalars().all()
            counts = await application_counts_by_tournament(session)
        return page_payload([tournament_json(t, counts.get(int(t.id))) for t in rows], total, page, limit)

    @router.post("/tournaments")
    async def tournament_create(payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Введите название турнира.")
        max_teams = _int(payload.get("max_teams", 16), "Максимум команд", 2, 1024)
        start = _parse_dt(payload.get("start_date"), "Дата старта")
        description = (str(payload.get("description") or "").strip() or None)
        async with session_scope() as session:
            try:
                t = await TournamentService(session).create(name, description, max_teams, start, bool(payload.get("open_registration")))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            await add_event(session, kind="tournament_created", title="Создан турнир", body=t.name)
            await session.commit()
            result = {"ok": True, "id": int(t.id), "name": t.name, "status": t.status.value}
        invalidate_public("tournaments:")
        return result

    @router.get("/tournaments/{tournament_id}")
    async def tournament_detail(tournament_id: int, viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            t = await session.get(Tournament, int(tournament_id))
            if t is None:
                raise HTTPException(status_code=404, detail="Турнир не найден.")
            counts = (await application_counts_by_tournament(session)).get(int(t.id))
            rows = (await session.execute(
                team_list_statement()
                .add_columns(TournamentApplication)
                .join(TournamentApplication, TournamentApplication.team_id == Team.id)
                .where(TournamentApplication.tournament_id == t.id)
                .order_by(TournamentApplication.id.desc())
            )).all()
            apps = []
            for team, captain, members, mmr_count, mmr_avg, app in rows:
                row = team_row_json(team, captain, members, mmr_count, mmr_avg)
                row.update({
                    "application_id": int(app.id),
                    "status": app.status.value,
                    "comment": app.admin_comment,
                    "created_at": iso(app.created_at),
                    "decided_at": iso(app.decided_at),
                })
                apps.append(row)
            data = tournament_json(t, counts)
        data["applications"] = apps
        return data

    @router.patch("/tournaments/{tournament_id}")
    async def tournament_edit(tournament_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            t = await session.get(Tournament, int(tournament_id))
            if t is None:
                raise HTTPException(status_code=404, detail="Турнир не найден.")
            if "name" in payload:
                name = str(payload.get("name") or "").strip()
                if not name:
                    raise HTTPException(status_code=400, detail="Название не может быть пустым.")
                if len(name) > 128:
                    raise HTTPException(status_code=400, detail="Название: не больше 128 символов.")
                t.name = name
            if "description" in payload:
                text = str(payload.get("description") or "").strip()
                if len(text) > 4000:
                    raise HTTPException(status_code=400, detail="Описание: не больше 4000 символов.")
                t.description = text or None
            if "max_teams" in payload:
                max_teams = _int(payload.get("max_teams"), "Максимум команд", 2, 1024)
                approved = int(await session.scalar(select(func.count(TournamentApplication.id)).where(
                    TournamentApplication.tournament_id == t.id, TournamentApplication.status == ApplicationStatus.APPROVED
                )) or 0)
                if max_teams < approved:
                    raise HTTPException(status_code=400, detail=f"Уже одобрено {approved} команд — лимит не может быть меньше.")
                t.max_teams = max_teams
            if "start_date" in payload:
                t.start_date = _parse_dt(payload.get("start_date"), "Дата старта")
            t.updated_at = datetime.now(timezone.utc)
            await session.commit()
        invalidate_public("tournaments:")
        return {"ok": True}

    @router.post("/tournaments/{tournament_id}/status")
    async def tournament_status(tournament_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        try:
            status = TournamentStatus(str(payload.get("status") or "").upper())
        except ValueError:
            raise HTTPException(status_code=400, detail="Неизвестный статус турнира.") from None
        async with session_scope() as session:
            svc = TournamentService(session)
            try:
                t = await svc.set_status(int(tournament_id), status)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
            await add_event(session, kind="tournament_status", title="Изменён статус турнира", body=f"{t.name}: {t.status.value}")
            await session.commit()
            result = {"ok": True, "status": t.status.value, "registration_open": bool(t.registration_open)}
        if svc.autopair:
            result["autopair"] = {k: svc.autopair.get(k) for k in ("action", "text", "pairs", "byes", "left")}
            notify.send_autopair(get_config(), svc.autopair)
        if svc.finished_now:
            from services.results_card import publish

            result["results_sent"] = await publish(get_config(), int(tournament_id))
        invalidate_public("tournaments:")
        return result

    @router.post("/tournaments/{tournament_id}/publish-results")
    async def tournament_publish_results(tournament_id: int, viewer: Viewer = Depends(admin_dep)):
        """5.13: разослать картинку с итогами ещё раз (админам, игрокам команд, болельщикам)."""
        from services.results_card import publish

        sent = await publish(get_config(), int(tournament_id))
        if not sent:
            raise HTTPException(status_code=400, detail="Итогов ещё нет — финал не сыгран.")
        return {"ok": True, "sent": sent}

    @router.get("/tournaments/{tournament_id}/bracket")
    async def bracket(tournament_id: int, viewer: Viewer = Depends(admin_dep)):
        from services.bracket import get_admin_state

        async with session_scope() as session:
            t = await session.get(Tournament, int(tournament_id))
            if t is None:
                raise HTTPException(status_code=404, detail="Турнир не найден.")
            state = await get_admin_state(session, t)
        for team in state.get("teams") or []:
            team["avatar_url"] = avatar_url(team["id"])
        return state

    @router.post("/tournaments/{tournament_id}/bracket/generate")
    async def bracket_generate(tournament_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        from services.bracket import generate

        a = payload.get("opening_team_a")
        b = payload.get("opening_team_b")
        if a in (None, "") or b in (None, ""):
            raise HTTPException(status_code=400, detail="Выберите две команды для первого матча.")
        a, b = _int(a, "Команда A"), _int(b, "Команда B")
        lock = bool(payload.get("lock"))
        async with session_scope() as session:
            t = await session.get(Tournament, int(tournament_id))
            if t is None:
                raise HTTPException(status_code=404, detail="Турнир не найден.")
            try:
                state = await generate(session, t, a, b, lock)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            await add_event(
                session, kind="tournament_bracket",
                title="Сетка турнира зафиксирована" if lock else "Сетка турнира сгенерирована", body=t.name,
            )
            await session.commit()
        invalidate_public("tournaments:")
        return {"ok": True, **state}

    @router.post("/tournaments/{tournament_id}/bracket/lock")
    async def bracket_lock(tournament_id: int, viewer: Viewer = Depends(admin_dep)):
        from services.bracket import lock_existing

        async with session_scope() as session:
            t = await session.get(Tournament, int(tournament_id))
            if t is None:
                raise HTTPException(status_code=404, detail="Турнир не найден.")
            try:
                state = await lock_existing(session, t)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            await add_event(session, kind="tournament_bracket", title="Сетка турнира зафиксирована", body=t.name)
            await session.commit()
        invalidate_public("tournaments:")
        return {"ok": True, **state}

    # ------------------------------------------------------------------
    # Заявки
    # ------------------------------------------------------------------
    @router.get("/applications")
    async def applications(
        status: str = Query(default="PENDING"),
        page: int = Query(default=1, ge=1, le=1000),
        limit: int = Query(default=20, ge=1, le=50),
        viewer: Viewer = Depends(admin_dep),
    ):
        page, limit, offset = page_params(page, limit)
        wanted = status.strip().upper()
        valid = {s.value for s in ApplicationStatus}
        if wanted != "ALL" and wanted not in valid:
            raise HTTPException(status_code=400, detail="Неизвестный статус заявки.")
        async with session_scope() as session:
            base = (
                select(TournamentApplication, Tournament, Team, User)
                .join(Tournament, Tournament.id == TournamentApplication.tournament_id)
                .outerjoin(Team, Team.id == TournamentApplication.team_id)
                .outerjoin(User, User.id == TournamentApplication.created_by)
            )
            if wanted != "ALL":
                base = base.where(TournamentApplication.status == ApplicationStatus(wanted))
            total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
            rows = (await session.execute(base.order_by(TournamentApplication.id.desc()).limit(limit).offset(offset))).all()
            counts = {
                (s.value if hasattr(s, "value") else str(s)): int(n)
                for s, n in (await session.execute(
                    select(TournamentApplication.status, func.count(TournamentApplication.id)).group_by(TournamentApplication.status)
                )).all()
            }
        items = [
            {
                "id": int(a.id),
                "status": a.status.value,
                "comment": a.admin_comment,
                "tournament": {"id": int(t.id), "name": t.name},
                "team": {"id": int(team.id), "name": team.name, "avatar_url": avatar_url(team.id)} if team else None,
                "captain": {"user_id": int(u.id), "name": display_name(u), "username": u.username} if u else None,
                "created_at": iso(a.created_at),
                "decided_at": iso(a.decided_at),
            }
            for a, t, team, u in rows
        ]
        payload = page_payload(items, total, page, limit)
        payload["counts"] = {s.value: counts.get(s.value, 0) for s in ApplicationStatus}
        return payload

    @router.post("/applications/{app_id}/decision")
    async def application_decision(app_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        try:
            status = ApplicationStatus(str(payload.get("status") or "").upper())
        except ValueError:
            raise HTTPException(status_code=400, detail="Неизвестный статус.") from None
        if status not in {ApplicationStatus.APPROVED, ApplicationStatus.REJECTED, ApplicationStatus.CANCELLED}:
            raise HTTPException(status_code=400, detail="Допустимо: APPROVED, REJECTED, CANCELLED.")
        comment = (str(payload.get("comment")).strip() or None) if payload.get("comment") else None
        async with session_scope() as session:
            app = await session.get(TournamentApplication, int(app_id))
            if app is None:
                raise HTTPException(status_code=404, detail="Заявка не найдена.")
            if app.status not in {ApplicationStatus.PENDING, ApplicationStatus.DRAFT}:
                raise HTTPException(status_code=400, detail="Заявка уже обработана.")
            t = await session.get(Tournament, app.tournament_id)
            if status == ApplicationStatus.APPROVED and t is not None:
                approved = int(await session.scalar(select(func.count(TournamentApplication.id)).where(
                    TournamentApplication.tournament_id == t.id, TournamentApplication.status == ApplicationStatus.APPROVED
                )) or 0)
                if approved >= int(t.max_teams):
                    raise HTTPException(status_code=400, detail=f"Лимит турнира ({t.max_teams} команд) уже заполнен.")
            await ApplicationRepository(session).decide(app, status, viewer.telegram_id, comment)
            team = await session.get(Team, app.team_id)
            captain = await session.get(User, app.created_by)
            title = {"APPROVED": "Заявка одобрена", "REJECTED": "Заявка отклонена", "CANCELLED": "Заявка отменена"}[status.value]
            await add_event(
                session, kind="application_decision", title=title,
                body=comment or f"{team.name if team else 'Команда'} → {t.name if t else 'турнир'}",
                target_user_id=int(captain.id) if captain else None,
            )
            await session.commit()
            captain_tg = captain.telegram_id if captain else None
            t_name = t.name if t else "турнир"
        text = {
            "APPROVED": f"✅ Заявка команды на «{html_escape(t_name)}» одобрена.",
            "REJECTED": f"❌ Заявка команды на «{html_escape(t_name)}» отклонена.",
            "CANCELLED": f"⛔ Заявка команды на «{html_escape(t_name)}» отменена.",
        }[status.value]
        if comment:
            text += f"\nКомментарий: {html_escape(comment)}"
        notify.notify_user(get_config(), captain_tg, text, label="application_decision")
        invalidate_public("tournaments:", "team:")
        return {"ok": True, "status": status.value}

    # ------------------------------------------------------------------
    # Поддержка
    # ------------------------------------------------------------------
    @router.get("/support")
    async def support(
        status: str = Query(default="OPEN"),
        page: int = Query(default=1, ge=1, le=1000),
        limit: int = Query(default=20, ge=1, le=50),
        viewer: Viewer = Depends(admin_dep),
    ):
        page, limit, offset = page_params(page, limit)
        wanted = status.strip().upper()
        async with session_scope() as session:
            base = select(SupportTicket, User).outerjoin(User, User.id == SupportTicket.user_id)
            if wanted != "ALL":
                try:
                    base = base.where(SupportTicket.status == TicketStatus(wanted))
                except ValueError:
                    raise HTTPException(status_code=400, detail="Неизвестный статус обращения.") from None
            total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
            rows = (await session.execute(base.order_by(SupportTicket.id.desc()).limit(limit).offset(offset))).all()
            counts = {
                (s.value if hasattr(s, "value") else str(s)): int(n)
                for s, n in (await session.execute(
                    select(SupportTicket.status, func.count(SupportTicket.id)).group_by(SupportTicket.status)
                )).all()
            }
        items = [
            {
                "id": int(t.id),
                "status": t.status.value,
                "message": t.message,
                "reply": t.admin_reply,
                "user": {"user_id": int(u.id), "telegram_id": u.telegram_id, "name": display_name(u), "username": u.username} if u else None,
                "created_at": iso(t.created_at),
                "updated_at": iso(t.updated_at),
            }
            for t, u in rows
        ]
        payload = page_payload(items, total, page, limit)
        payload["counts"] = {s.value: counts.get(s.value, 0) for s in TicketStatus}
        return payload

    @router.post("/support/{ticket_id}/reply")
    async def support_reply(ticket_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        reply = str(payload.get("reply") or "")
        async with session_scope() as session:
            pair = await SupportService(session).get_for_admin(int(ticket_id))
            if not pair:
                raise HTTPException(status_code=404, detail="Обращение не найдено.")
            ticket, user = pair
            try:
                await SupportService(session).answer(int(ticket_id), reply, viewer.telegram_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            # Текст ответа — приватная переписка: только личное уведомление, без публичного события.
            session.add(Notification(
                user_id=int(user.id), kind="support_reply", title=f"Ответ поддержки (обращение №{int(ticket_id)})",
                body=reply.strip()[:1000],
            ))
            await session.commit()
            telegram_id = user.telegram_id
        notify.notify_user(
            get_config(), telegram_id,
            f"💬 <b>Ответ поддержки</b> (обращение №{int(ticket_id)}):\n\n{html_escape(reply.strip())}",
            label="support_reply",
        )
        return {"ok": True}

    @router.post("/support/{ticket_id}/close")
    async def support_close(ticket_id: int, viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            ticket = await session.get(SupportTicket, int(ticket_id))
            if ticket is None:
                raise HTTPException(status_code=404, detail="Обращение не найдено.")
            ticket.status = TicketStatus.CLOSED
            ticket.updated_at = datetime.now(timezone.utc)
            await session.commit()
        return {"ok": True}

    # ------------------------------------------------------------------
    # Рассылка (фоновая)
    # ------------------------------------------------------------------
    @router.post("/broadcast")
    async def broadcast(payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Введите текст рассылки.")
        if len(text) > 4000:
            raise HTTPException(status_code=400, detail="Текст рассылки: не больше 4000 символов.")
        async with session_scope() as session:
            recipients = [int(x) for x in (await session.execute(select(User.telegram_id).order_by(User.id))).scalars().all() if x]
        try:
            state = notify.start_broadcast(get_config(), html_escape(text), viewer.telegram_id, recipients)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"ok": True, "state": state}

    @router.get("/broadcast/status")
    async def broadcast_status(viewer: Viewer = Depends(admin_dep)):
        return {"state": dict(notify.broadcast_state)}

    @router.get("/broadcast/history")
    async def broadcast_history(viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            rows = (await session.execute(select(BroadcastLog).order_by(BroadcastLog.id.desc()).limit(30))).scalars().all()
        return {"items": [
            {"id": int(r.id), "type": r.content_type, "total": r.total, "sent": r.sent, "blocked": r.blocked,
             "failed": r.failed, "created_at": iso(r.created_at)}
            for r in rows
        ]}

    # ------------------------------------------------------------------
    # Диагностика (только по кнопке)
    # ------------------------------------------------------------------
    @router.get("/diagnostics")
    async def diagnostics(viewer: Viewer = Depends(admin_dep)):
        from services.diagnostics import run_diagnostics
        from services.factory import get_services

        started = time.perf_counter()
        try:
            text = await asyncio.wait_for(run_diagnostics(get_services()), timeout=DIAGNOSTICS_TIMEOUT)
            ok = True
        except asyncio.TimeoutError:
            text, ok = f"Диагностика не завершилась за {int(DIAGNOSTICS_TIMEOUT)} с — внешние сервисы отвечают слишком долго.", False
        except Exception as exc:  # noqa: BLE001
            text, ok = f"Ошибка диагностики: {type(exc).__name__}: {exc}", False
        from miniapp_v2 import VERSION

        return {
            "ok": ok,
            "text": text,
            "elapsed": round(time.perf_counter() - started, 2),
            "miniapp": {
                "version": VERSION,
                "telegram_notifications": not notify.telegram_disabled(),
                "background_tasks": notify.pending_tasks(),
            },
        }

    # ------------------------------------------------------------------
    # Администраторы
    # ------------------------------------------------------------------
    @router.get("/admins")
    async def admins(viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            rows = (await session.execute(
                select(AdminUser, User).outerjoin(User, User.telegram_id == AdminUser.telegram_id)
                .where(AdminUser.active.is_(True)).order_by(AdminUser.id)
            )).all()
        config = get_config()
        return {"items": [
            {"telegram_id": int(a.telegram_id), "name": display_name(u) if u else str(a.telegram_id),
             "username": u.username if u else None, "created_at": iso(a.created_at),
             "is_me": int(a.telegram_id) == viewer.telegram_id,
             "bootstrap": int(a.telegram_id) in _bootstrap_ids(config)}
            for a, u in rows
        ]}

    @router.post("/admins")
    async def admin_add(payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        telegram_id = _int(payload.get("telegram_id"), "Telegram ID", 1, 10**15)
        async with session_scope() as session:
            row = await session.scalar(select(AdminUser).where(AdminUser.telegram_id == telegram_id))
            if row is None:
                session.add(AdminUser(telegram_id=telegram_id, added_by=viewer.telegram_id, active=True))
            else:
                row.active = True
            await session.commit()
        _set_admin(telegram_id, True)
        return {"ok": True, "telegram_id": telegram_id}

    @router.delete("/admins/{telegram_id}")
    async def admin_remove(telegram_id: int, viewer: Viewer = Depends(admin_dep)):
        if int(telegram_id) == viewer.telegram_id:
            raise HTTPException(status_code=400, detail="Нельзя удалить самого себя.")
        async with session_scope() as session:
            row = await session.scalar(select(AdminUser).where(AdminUser.telegram_id == int(telegram_id), AdminUser.active.is_(True)))
            if row is None:
                raise HTTPException(status_code=404, detail="Администратор не найден.")
            active = int(await session.scalar(select(func.count(AdminUser.id)).where(AdminUser.active.is_(True))) or 0)
            if active <= 1:
                raise HTTPException(status_code=400, detail="Должен остаться хотя бы один администратор.")
            row.active = False
            await session.commit()
        _set_admin(int(telegram_id), False)
        warning = None
        if int(telegram_id) in _bootstrap_ids(get_config()):
            warning = "Этот ID указан в ADMIN_IDS (.env) и вернётся после перезапуска бота."
        return {"ok": True, "warning": warning}

    # ------------------------------------------------------------------
    # AI-помощник
    # ------------------------------------------------------------------
    @router.get("/ai/status")
    async def ai_status(viewer: Viewer = Depends(admin_dep)):
        from services.ai_assistant import TOOL_DEFINITIONS

        c = get_config()
        providers = []
        if (c.ai_api_key or "").strip():
            providers.append({"name": "OpenRouter", "model": c.ai_model, "configured": True})
            if (c.ai_fallback_model or "").strip() and c.ai_fallback_model != c.ai_model:
                providers.append({"name": "OpenRouter (резерв)", "model": c.ai_fallback_model, "configured": True})
        providers.append({"name": "Groq (резерв)", "model": c.ai_backup_model, "configured": bool((c.ai_backup_api_key or "").strip())})
        from services import ai_budget

        return {
            "configured": any(p["configured"] for p in providers),
            "providers": providers,
            "tool_count": len(TOOL_DEFINITIONS),
            "max_tool_rounds": c.ai_max_tool_rounds,
            "proxy_configured": bool((c.http_proxy_url or "").strip()),
            "usage": ai_budget.usage(),
        }

    @router.post("/ai/ask")
    async def ai_ask(payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        from services.ai_assistant import ask_ai_agent

        question = str(payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="Введите вопрос.")
        if len(question) > 4000:
            raise HTTPException(status_code=400, detail="Вопрос: не больше 4000 символов.")
        history_raw = payload.get("history") or []
        history = []
        if isinstance(history_raw, list):
            for item in history_raw[-8:]:
                if isinstance(item, dict) and item.get("role") in {"user", "assistant"}:
                    history.append({"role": item["role"], "content": str(item.get("content") or "")[:4000]})
        now = time.monotonic()
        last = _ai_last.get(viewer.telegram_id)
        if last is not None and now - last < 2.0:
            raise HTTPException(status_code=429, detail="Подождите пару секунд перед следующим вопросом.")
        _ai_last[viewer.telegram_id] = now
        try:
            async with session_scope() as session:
                answer, telemetry = await asyncio.wait_for(
                    ask_ai_agent(get_config(), session, question, history), timeout=100.0
                )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="AI не ответил за 100 секунд. Попробуйте короче сформулировать вопрос.") from None
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Не удалось получить ответ AI: {type(exc).__name__}: {str(exc)[:500]}") from None
        return {"ok": True, "answer": answer, **(telemetry or {})}

    # ------------------------------------------------------------------
    # Аватары: перенос копий из Telegram (явное действие, с таймаутами)
    # ------------------------------------------------------------------
    @router.post("/avatars/sync")
    async def avatars_sync(viewer: Viewer = Depends(admin_dep)):
        from miniapp_v2.routes_user import prepare_avatar, save_avatar_file

        async with session_scope() as session:
            rows = (await session.execute(
                select(Team.id, Team.name, Team.avatar_file_id).where(Team.avatar_file_id.is_not(None)).order_by(Team.id)
            )).all()
        todo = [(int(tid), name, fid) for tid, name, fid in rows if avatar_url(int(tid)) is None]
        saved, failed, skipped = [], [], []
        started = time.monotonic()
        config = get_config()
        for tid, name, fid in todo:
            if time.monotonic() - started > AVATAR_SYNC_BUDGET:
                skipped.append({"team_id": tid, "name": name})
                continue
            try:
                raw = await notify.download_file(config, fid)
                data = await asyncio.to_thread(prepare_avatar, raw)
                await asyncio.to_thread(save_avatar_file, tid, data)
                saved.append({"team_id": tid, "name": name})
            except Exception as exc:  # noqa: BLE001
                failed.append({"team_id": tid, "name": name, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
        if saved:
            invalidate_public("teams:", "team:", "tournaments:")
        return {"ok": True, "checked": len(todo), "saved": saved, "failed": failed, "skipped": skipped}

    return router


def _bootstrap_ids(config) -> set[int]:
    import os

    from config import _parse_admin_ids

    return _parse_admin_ids(os.getenv("ADMIN_IDS", ""))


def _set_admin(telegram_id: int, active: bool) -> None:
    from config import GLOBAL_CONFIG

    for cfg in {id(get_config()): get_config(), id(GLOBAL_CONFIG): GLOBAL_CONFIG}.values():
        if active:
            cfg.admin_ids.add(int(telegram_id))
        else:
            cfg.admin_ids.discard(int(telegram_id))
