"""5.13: заморозка составов, снятие с турнира и расформирование команды организатором.

После закрытия регистрации (турнир «Черновик»/«Идёт», заявка одобрена) состав команды меняется только
с согласия организатора: вход, уход и исключение игрока превращаются в запрос, админ одобряет его
в Mini App или кнопкой в Telegram. Так в последний момент нельзя подсунуть в команду смурфа.
"""
from __future__ import annotations

import logging
from html import escape
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    ApplicationStatus,
    PlayerProfile,
    RosterRequest,
    Team,
    TeamArchive,
    TeamInvite,
    TeamInviteStatus,
    TeamMember,
    TeamRole,
    Tournament,
    TournamentApplication,
    TournamentStatus,
    User,
    utcnow,
)

logger = logging.getLogger("ROSTER")

FROZEN_STATUSES = (TournamentStatus.DRAFT, TournamentStatus.ACTIVE)
KIND_TEXT = {"ADD": "добавить в состав", "REMOVE": "исключить из состава", "LEAVE": "выйти из состава"}
MAX_ROSTER_SIZE = 6


class RosterFrozen(ValueError):
    """Изменение не применено, а отправлено организаторам на одобрение."""

    def __init__(self, message: str, request_id: int):
        super().__init__(message)
        self.request_id = request_id


def _h(value: Any) -> str:
    return escape(str(value if value is not None else ""), quote=False)


def _name(u: User | None) -> str:
    if u is None:
        return "игрок"
    if u.username:
        return "@" + u.username
    return " ".join(x for x in (u.first_name, u.last_name) if x) or f"id{u.telegram_id}"


async def frozen_tournament(session: AsyncSession, team_id: int) -> Tournament | None:
    """Турнир, в котором состав команды заморожен: заявка одобрена, регистрация закрыта, турнир не завершён."""
    return await session.scalar(
        select(Tournament).join(TournamentApplication, TournamentApplication.tournament_id == Tournament.id)
        .where(TournamentApplication.team_id == int(team_id), TournamentApplication.status == ApplicationStatus.APPROVED,
               Tournament.status.in_(FROZEN_STATUSES))
        .order_by(Tournament.id.desc()).limit(1)
    )


async def guard(session: AsyncSession, team_id: int, kind: str, target_user_id: int, requested_by: int,
                *, invite: TeamInvite | None = None) -> None:
    """Состав не заморожен — ничего не делает. Заморожен — создаёт запрос (или находит такой же) и бросает RosterFrozen."""
    t = await frozen_tournament(session, team_id)
    if t is None:
        return
    req = await session.scalar(select(RosterRequest).where(
        RosterRequest.team_id == int(team_id), RosterRequest.kind == kind, RosterRequest.target_user_id == int(target_user_id),
        RosterRequest.status == "PENDING"))
    fresh = req is None
    if fresh:
        req = RosterRequest(team_id=int(team_id), tournament_id=int(t.id), kind=kind, target_user_id=int(target_user_id),
                            requested_by=int(requested_by), invite_id=int(invite.id) if invite else None)
        session.add(req)
    if invite is not None and invite.status == TeamInviteStatus.PENDING:
        invite.status = TeamInviteStatus.ACCEPTED
        invite.decided_at = utcnow()
    await session.commit()
    if fresh:
        await notify_admins_new(session, req)
    raise RosterFrozen(
        f"Состав заморожен: регистрация на «{t.name[:40]}» закрыта. Запрос «{KIND_TEXT.get(kind, kind)}» отправлен "
        "организаторам — вступит в силу после одобрения.", int(req.id))


async def _player_facts(session: AsyncSession, user_id: int) -> str:
    """MMR, ранг и последняя проверка на смурфа — чтобы админ решал по фактам."""
    p = await session.scalar(select(PlayerProfile).where(PlayerProfile.user_id == int(user_id)))
    parts = []
    if p is not None:
        if p.exact_mmr is not None:
            parts.append(f"MMR {p.exact_mmr}" + (" (подтверждён)" if p.mmr_verified else " (не подтверждён)"))
        if p.steam_id64:
            parts.append(f"Steam {p.steam_id64}")
    try:
        from database.models import PlayerCheck

        chk = await session.scalar(select(PlayerCheck).where(PlayerCheck.user_id == int(user_id)).order_by(PlayerCheck.id.desc()).limit(1))
        if chk is not None and getattr(chk, "risk_level", None):
            parts.append(f"риск смурфа: {chk.risk_level}" + (f" ({chk.risk_score}/100)" if getattr(chk, "risk_score", None) is not None else ""))
    except Exception:  # noqa: BLE001 — проверки могут отсутствовать
        pass
    return ", ".join(parts) or "данных о MMR нет"


async def request_json(session: AsyncSession, req: RosterRequest) -> dict[str, Any]:
    team = await session.get(Team, req.team_id)
    target = await session.get(User, req.target_user_id)
    by = await session.get(User, req.requested_by)
    t = await session.get(Tournament, req.tournament_id) if req.tournament_id else None
    return {
        "id": int(req.id), "kind": req.kind, "kind_text": KIND_TEXT.get(req.kind, req.kind), "status": req.status,
        "team": {"id": int(req.team_id), "name": team.name if team else "команда"},
        "tournament": t.name if t else None,
        "target": {"user_id": int(req.target_user_id), "name": _name(target)},
        "requested_by": _name(by), "facts": await _player_facts(session, req.target_user_id),
        "comment": req.comment, "created_at": req.created_at.isoformat() if req.created_at else None,
    }


async def notify_admins_new(session: AsyncSession, req: RosterRequest) -> None:
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from miniapp_v2 import notify
        from miniapp_v2.common import get_config

        data = await request_json(session, req)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Одобрить", callback_data=f"rq:ok:{req.id}"),
            InlineKeyboardButton(text="Отклонить", callback_data=f"rq:no:{req.id}"),
        ]])
        notify.notify_admins(get_config(), (
            f"<b>Замена в составе #{req.id}</b> — {_h(data['tournament'] or 'турнир')}\n"
            f"Команда: <b>{_h(data['team']['name'])}</b>\nЗапрос: {_h(data['kind_text'])} — <b>{_h(data['target']['name'])}</b>\n"
            f"Кто просит: {_h(data['requested_by'])}\nИгрок: {_h(data['facts'])}\n"
            "Решить можно здесь или в Mini App: Админ → Замены."), markup, label="roster_request")
    except Exception:  # noqa: BLE001
        logger.exception("[ROSTER] admin notify failed for request %s", req.id)


async def pending(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(select(RosterRequest).where(RosterRequest.status == "PENDING").order_by(RosterRequest.id))).scalars().all()
    return [await request_json(session, r) for r in rows]


async def decide(session: AsyncSession, request_id: int, approve: bool, admin_user_id: int | None,
                 comment: str | None = None) -> dict[str, Any]:
    """Одобрение применяет изменение состава. Возвращает тексты уведомлений: [(telegram_id, text)]."""
    req = await session.get(RosterRequest, int(request_id))
    if req is None:
        raise ValueError("Запрос не найден.")
    if req.status != "PENDING":
        raise ValueError("Запрос уже рассмотрен.")
    team = await session.get(Team, req.team_id)
    target = await session.get(User, req.target_user_id)
    captain = await session.get(User, team.captain_user_id) if team else None
    if approve:
        if team is None:
            raise ValueError("Команды больше нет.")
        if req.kind == "ADD":
            if await session.scalar(select(TeamMember.id).where(TeamMember.user_id == req.target_user_id)):
                raise ValueError("Игрок уже состоит в команде.")
            players = (await session.execute(select(TeamMember.id).where(TeamMember.team_id == team.id, TeamMember.role != TeamRole.COACH))).all()
            if len(players) >= MAX_ROSTER_SIZE:
                raise ValueError(f"В команде уже {MAX_ROSTER_SIZE} игроков.")
            session.add(TeamMember(team_id=team.id, user_id=req.target_user_id, role=TeamRole.PLAYER))
        else:
            if int(req.target_user_id) == int(team.captain_user_id):
                raise ValueError("Капитана нельзя убрать из состава.")
            member = await session.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == req.target_user_id))
            if member is not None:
                await session.delete(member)
    req.status = "APPROVED" if approve else "REJECTED"
    req.decided_by = admin_user_id
    req.decided_at = utcnow()
    req.comment = (comment or "").strip()[:300] or None
    await session.flush()
    team_name = team.name if team else "команда"
    verdict = "одобрил" if approve else "отклонил"
    text = (f"Организатор {verdict} изменение состава «{_h(team_name)}»: {_h(KIND_TEXT.get(req.kind, req.kind))} — "
            f"{_h(_name(target))}." + (f"\nКомментарий: {_h(req.comment)}" if req.comment else ""))
    to = {u.telegram_id for u in (captain, target) if u is not None and u.telegram_id}
    return {"status": req.status, "messages": [(tg, text) for tg in to], "team_name": team_name}


async def withdraw(session: AsyncSession, tournament_id: int, team_id: int, *, by_user_id: int | None,
                   reason: str, admin: bool = False) -> dict[str, Any]:
    """Снимает команду с турнира: заявка «Отменена», команда уходит из сетки (или проигрывает технически)."""
    from services.tournament_engine import service as engine

    t = await session.get(Tournament, int(tournament_id))
    if t is None:
        raise ValueError("Турнир не найден.")
    if t.status in (TournamentStatus.FINISHED, TournamentStatus.CANCELLED):
        raise ValueError("Турнир уже завершён.")
    app = await session.scalar(select(TournamentApplication).where(
        TournamentApplication.tournament_id == t.id, TournamentApplication.team_id == int(team_id),
        TournamentApplication.status.in_((ApplicationStatus.DRAFT, ApplicationStatus.PENDING, ApplicationStatus.APPROVED)))
        .order_by(TournamentApplication.id.desc()).limit(1))
    if app is None:
        raise ValueError("Команда не заявлена на этот турнир.")
    was_approved = app.status == ApplicationStatus.APPROVED
    app.status = ApplicationStatus.CANCELLED
    app.admin_comment = reason[:500]
    app.decided_at = utcnow()
    await session.flush()
    report = await engine.withdraw_team(session, t, int(team_id), admin_user_id=by_user_id if admin else None) if was_approved else \
        {"forfeits": [], "removed": 0, "waiting": [], "autopair": None}
    team = await session.get(Team, int(team_id))
    team_name = team.name if team else (await engine._team_names(session, {int(team_id)})).get(int(team_id), "команда")
    messages: list[tuple[int, str]] = []
    opp_ids = {f["opponent_id"] for f in report["forfeits"]}
    if opp_ids:
        rows = (await session.execute(select(Team.id, User.telegram_id).join(User, User.id == Team.captain_user_id).where(Team.id.in_(opp_ids)))).all()
        caps = {int(i): tg for i, tg in rows if tg}
        for f in report["forfeits"]:
            if caps.get(f["opponent_id"]):
                messages.append((caps[f["opponent_id"]], f"Команда «{_h(team_name)}» снялась с «{_h(t.name)}». Вам засчитана техническая победа "
                                                          f"в матче {f['match_no']} ({_h(f['round_name'])})."))
    lines = [f"Команда <b>{_h(team_name)}</b> снята с «{_h(t.name)}» ({_h(reason)})."]
    if report["forfeits"]:
        lines.append("Технические поражения: " + ", ".join(f"матч {f['match_no']}" for f in report["forfeits"]) + ".")
    if report["waiting"]:
        lines.append("Матчи, где соперник ещё не определён, засчитаются автоматически: " + ", ".join(map(str, report["waiting"])) + ".")
    if report.get("autopair") and report["autopair"].get("text"):
        lines.append("Сетка: " + _h(report["autopair"]["text"]))
    return {"team_name": team_name, "tournament": t.name, "was_approved": was_approved, "report": report,
            "messages": messages, "admin_text": "\n".join(lines)}


async def disband(session: AsyncSession, team_id: int, *, admin_user_id: int | None, reason: str) -> dict[str, Any]:
    """Организатор расформировывает команду: снимает её со всех турниров, сохраняет название в архиве, удаляет состав."""
    team = await session.get(Team, int(team_id))
    if team is None:
        raise ValueError("Команда не найдена.")
    name = team.name
    apps = (await session.execute(select(TournamentApplication).where(
        TournamentApplication.team_id == team.id,
        TournamentApplication.status.in_((ApplicationStatus.DRAFT, ApplicationStatus.PENDING, ApplicationStatus.APPROVED))))).scalars().all()
    withdrawals = []
    for tid in sorted({int(a.tournament_id) for a in apps}):
        t = await session.get(Tournament, tid)
        if t is None or t.status in (TournamentStatus.FINISHED, TournamentStatus.CANCELLED):
            continue
        withdrawals.append(await withdraw(session, tid, team.id, by_user_id=admin_user_id, reason=f"команда расформирована: {reason}", admin=True))
    members = (await session.execute(select(TeamMember.user_id).where(TeamMember.team_id == team.id))).scalars().all()
    tgs = [tg for tg in (await session.execute(select(User.telegram_id).where(User.id.in_(members)))).scalars().all() if tg]
    if await session.scalar(select(TeamArchive.id).where(TeamArchive.team_id == team.id)) is None:
        session.add(TeamArchive(team_id=team.id, name=name, reason=reason[:300], disbanded_by=admin_user_id))
    for model, col in ((TeamInvite, TeamInvite.team_id), (TournamentApplication, TournamentApplication.team_id), (TeamMember, TeamMember.team_id)):
        await session.execute(delete(model).where(col == team.id))
    await session.execute(delete(RosterRequest).where(RosterRequest.team_id == team.id, RosterRequest.status == "PENDING"))
    try:
        from database.models import TeamFollow

        await session.execute(delete(TeamFollow).where(TeamFollow.team_id == team.id))
    except ImportError:
        pass
    await session.delete(team)
    await session.flush()
    try:
        (Path(__file__).resolve().parent.parent / "miniapp" / "data" / "team_avatars" / f"team_{int(team_id)}.jpg").unlink(missing_ok=True)
    except OSError:
        logger.exception("[ROSTER] avatar remove failed team=%s", team_id)
    text = f"Организатор расформировал команду «{_h(name)}»." + (f"\nПричина: {_h(reason)}" if reason else "") + "\nВы снова свободный игрок."
    messages = [(tg, text) for tg in tgs]
    for w in withdrawals:
        messages += w["messages"]
    logger.info("[ROSTER] team %s (%s) disbanded by %s", team_id, name, admin_user_id)
    return {"team_name": name, "members": list(members), "messages": messages,
            "admin_text": f"Команда <b>{_h(name)}</b> расформирована." + "".join("\n" + w["admin_text"] for w in withdrawals)}
