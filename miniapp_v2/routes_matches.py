"""Турнирный движок 5.2 в API Mini App: сетка, расписание, матчи, действия админа.

Публичное: GET /api/v2/tournaments/{id}/schedule, /api/v2/tournaments/{id}/engine,
/api/v2/matches/{id}. Админское: /api/v2/admin/tournaments/{id}/engine/*,
/api/v2/admin/matches/{id}/*. Каждое действие — одна транзакция; 409 с
need_confirm означает «покажите предупреждения и спросите подтверждение».
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select

from database.engine import session_scope
from database.models import Notification, TeamMember, Tournament, TournamentMatch, TournamentStatus  # noqa: F401
from miniapp_v2 import notify
from miniapp_v2.common import SafeRoute, Viewer, admin_dep, avatar_url, get_config, html_escape, invalidate_public, viewer_dep
from services.activity import add_event
from services.tournament_engine import service as engine
from services.tournament_engine import timeutil
from services.tournament_engine.formats import formats_catalog

logger = logging.getLogger("MINIAPP_V2.MATCHES")


def _fail(exc: engine.EngineError):
    if exc.status == 409:
        return JSONResponse(status_code=409, content={"detail": str(exc), **exc.data})
    raise HTTPException(status_code=exc.status, detail=str(exc))


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


async def _viewer_team(session, user_id: int) -> int | None:
    tid = await session.scalar(select(TeamMember.team_id).where(TeamMember.user_id == int(user_id)).limit(1))
    return int(tid) if tid else None


async def _visible_tournament(session, tournament_id: int, viewer: Viewer) -> Tournament:
    t = await session.get(Tournament, int(tournament_id))
    if t is None or (t.status == TournamentStatus.DRAFT and not viewer.is_admin):
        raise HTTPException(status_code=404, detail="Турнир не найден.")
    return t


async def _tournament(session, tournament_id: int) -> Tournament:
    t = await session.get(Tournament, int(tournament_id))
    if t is None:
        raise HTTPException(status_code=404, detail="Турнир не найден.")
    return t


def _settings_from(payload: dict[str, Any]) -> dict[str, Any]:
    out = {
        "bo_default": payload.get("bo_default", 3),
        "bo_final": payload.get("bo_final", payload.get("bo_default", 3)),
        "third_place": _flag(payload.get("third_place")),
        "grand_final_reset": _flag(payload.get("grand_final_reset")),
    }
    # Табличные форматы (5.4): группы, выход в плей-офф, пороги швейцарки.
    for key in ("groups", "advance", "playoff_format", "playoff_bo_default", "playoff_bo_final",
                "wins_to_advance", "losses_to_eliminate"):
        if payload.get(key) not in (None, ""):
            out[key] = payload.get(key)
    return out


async def _notify_match(session, m: TournamentMatch, kind: str, title: str, body: str) -> list[int]:
    """In-app уведомления игрокам обеих команд; возвращает telegram_id для рассылки."""
    members = await engine.match_team_members(session, m)
    for user_id, _tg in members:
        session.add(Notification(user_id=user_id, kind=kind[:64], title=title[:160], body=body[:1000]))
    return [tg for _, tg in members]


def _send_telegram(telegram_ids: list[int], text: str, label: str) -> None:
    cfg = get_config()
    for tg in telegram_ids:
        notify.notify_user(cfg, tg, text, label=label)


def build_matches_router() -> APIRouter:
    router = APIRouter(prefix="/api/v2", route_class=SafeRoute)

    # ------------------------------------------------------------------
    # Публичное
    # ------------------------------------------------------------------
    @router.get("/tournaments/{tournament_id}/engine")
    async def tournament_engine(tournament_id: int, viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            t = await _visible_tournament(session, tournament_id, viewer)
            team_id = await _viewer_team(session, viewer.user_id)
            view = await engine.stage_view(session, t, viewer_team_id=team_id, avatar_fn=avatar_url)
        return {"engine": view, "tz": timeutil.tz_info()}

    @router.get("/tournaments/{tournament_id}/schedule")
    async def tournament_schedule(tournament_id: int, viewer: Viewer = Depends(viewer_dep)):
        async with session_scope() as session:
            t = await _visible_tournament(session, tournament_id, viewer)
            team_id = await _viewer_team(session, viewer.user_id)
            data = await engine.schedule_view(session, t, viewer_team_id=team_id, avatar_fn=avatar_url)
        data.update({"tournament_id": int(t.id), "tournament_name": t.name, "my_team_id": team_id})
        return data

    @router.get("/matches/{match_id}")
    async def match_detail(match_id: int, viewer: Viewer = Depends(viewer_dep)):
        from database.models import Team
        from services import tournament_extras as extras

        async with session_scope() as session:
            m = await session.get(TournamentMatch, int(match_id))
            if m is None:
                raise HTTPException(status_code=404, detail="Матч не найден.")
            await _visible_tournament(session, m.tournament_id, viewer)
            team_id = await _viewer_team(session, viewer.user_id)
            try:
                data = await engine.match_detail(session, match_id, admin=viewer.is_admin, viewer_team_id=team_id, avatar_fn=avatar_url)
            except engine.EngineError as exc:
                return _fail(exc)
            participant = bool(team_id and team_id in (m.team1_id, m.team2_id))
            # 5.8: лобби видят игроки матча и админы; готовность и результат от капитанов.
            if (participant or viewer.is_admin) and m.team1_id and m.team2_id and m.status != "FINISHED":
                data["lobby"] = extras.lobby_for(m, get_config().bot_token)
            data["ready_team_ids"] = sorted(await extras.ready_teams(session, m.id))
            rep = await extras.active_report(session, m.id)
            data["report"] = extras.report_json(rep, await extras.report_dota(session, rep.id)) if rep and (participant or viewer.is_admin) else None
            captain_of = await session.scalar(select(Team.id).where(Team.captain_user_id == viewer.user_id, Team.id.in_([m.team1_id or 0, m.team2_id or 0])))
            data["my_team_id"] = team_id
            data["i_am_captain"] = bool(captain_of)
            # 5.13: прогнозы и карты серии (ID матчей Dota после одобрения результата).
            from database.models import TournamentMatchGame
            from services import predictions

            data["prediction"] = await predictions.state(session, m, viewer.user_id) if m.team1_id and m.team2_id else None
            data["games"] = [{"no": g.game_no, "winner_team_id": g.winner_team_id, "dota_match_id": g.dota_match_id}
                             for g in (await session.execute(select(TournamentMatchGame).where(TournamentMatchGame.match_id == m.id)
                                                             .order_by(TournamentMatchGame.game_no))).scalars().all()]
        data["can_edit"] = bool(viewer.is_admin)
        return data

    @router.post("/matches/{match_id}/predict")
    async def match_predict(match_id: int, payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        from services import predictions

        try:
            team_id = int(payload.get("team_id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Выберите команду.") from None
        async with session_scope() as session:
            m = await session.get(TournamentMatch, int(match_id))
            if m is None:
                raise HTTPException(status_code=404, detail="Матч не найден.")
            await _visible_tournament(session, m.tournament_id, viewer)
            try:
                out = await predictions.predict(session, int(match_id), viewer.user_id, team_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            await session.commit()
        return {"ok": True, "prediction": out}

    @router.get("/tournaments/{tournament_id}/predictions")
    async def tournament_predictions(tournament_id: int, viewer: Viewer = Depends(viewer_dep)):
        from services import predictions

        async with session_scope() as session:
            await _visible_tournament(session, int(tournament_id), viewer)
            return await predictions.leaderboard(session, int(tournament_id), viewer.user_id)

    # ------------------------------------------------------------------
    # 5.8: результат от капитанов, MVP, статистика, зал славы
    # ------------------------------------------------------------------
    @router.post("/matches/{match_id}/report")
    async def match_report(match_id: int, score1: int | None = Form(default=None), score2: int | None = Form(default=None),
                           dota_ids: str | None = Form(default=None),
                           screenshot: UploadFile | None = File(default=None), viewer: Viewer = Depends(viewer_dep)):
        from services import tournament_extras as extras
        from services.opendota_match import parse_ids

        ids = parse_ids(dota_ids)
        if not ids and (score1 is None or score2 is None):
            return JSONResponse(status_code=400, content={"detail": "Укажите счёт серии или ID матчей Dota."})
        raw = await screenshot.read(extras.MAX_SCREENSHOT + 1) if screenshot is not None else None
        try:
            out = await extras.create_report(match_id, viewer.user_id, score1, score2, raw or None, ids or None)
        except engine.EngineError as exc:
            return _fail(exc)
        text = f"Капитан «{out['reporter']}» прислал результат матча {out['match_no']}: {out['text']}."
        if out.get("dota"):
            text += " OpenDota: " + "; ".join(f"карта {g['no']} — {g['duration']}" for g in out["dota"]["games"])
            text += " (команды опознаны по Steam)." if out.get("verified") else " (проверьте вручную)."
        markup = None
        if out.get("verified"):
            try:
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Одобрить результат", callback_data=f"mr:ok:{out['id']}")]])
            except Exception:  # noqa: BLE001
                markup = None
        if out.get("opponent_tg"):
            tail = ("\nРезультат подтверждён данными OpenDota — ждёт одобрения организатора." if out.get("verified")
                    else "\nПодтвердите или оспорьте его в Mini App: Турниры → матч.")
            notify.notify_user(get_config(), out["opponent_tg"], html_escape(text) + tail, label="report")
        notify.notify_admins(get_config(), html_escape(text) + "\nОдобрить: кнопкой ниже или Mini App → Админ → Результаты капитанов.",
                             markup, label="report")
        return {"ok": True, "report_id": out["id"], "verified": bool(out.get("verified")),
                "score1": out["dota"]["score1"] if out.get("dota") else score1, "score2": out["dota"]["score2"] if out.get("dota") else score2}

    @router.post("/match-reports/{report_id}/respond")
    async def report_respond(report_id: int, payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        from services import tournament_extras as extras

        try:
            out = await extras.respond_report(report_id, viewer.user_id, _flag(payload.get("confirm")), payload.get("comment"))
        except engine.EngineError as exc:
            return _fail(exc)
        word = "подтвердил" if out["status"] == "CONFIRMED" else "оспорил"
        notify.notify_admins(get_config(), f"Соперник {word} результат матча {out['match_no']}. Mini App → Админ → Результаты капитанов.", label="report")
        return {"ok": True, "status": out["status"]}

    @router.get("/match-reports/{report_id}/screenshot", include_in_schema=False)
    async def report_screenshot(report_id: int, viewer: Viewer = Depends(viewer_dep)):
        from fastapi.responses import FileResponse

        from database.models import MatchReport
        from services import tournament_extras as extras

        async with session_scope() as session:
            row = await session.get(MatchReport, int(report_id))
            m = await session.get(TournamentMatch, row.match_id) if row else None
            team_id = await _viewer_team(session, viewer.user_id)
        if row is None or not row.screenshot or not (viewer.is_admin or (m and team_id in (m.team1_id, m.team2_id))):
            raise HTTPException(status_code=404, detail="Скриншот не найден.")
        return FileResponse(extras.REPORT_DIR / row.screenshot, media_type="image/jpeg")

    @router.get("/admin/match-reports")
    async def admin_reports(viewer: Viewer = Depends(admin_dep)):
        from database.models import MatchReport
        from services import tournament_extras as extras

        async with session_scope() as session:
            rows = (await session.execute(select(MatchReport).where(MatchReport.status.in_(extras.ACTIVE_REPORT)).order_by(MatchReport.id))).scalars().all()
            items = []
            for r in rows:
                m = await session.get(TournamentMatch, r.match_id)
                names = await engine._team_names(session, {m.team1_id, m.team2_id, r.team_id} - {None}) if m else {}
                items.append({**extras.report_json(r, await extras.report_dota(session, r.id)), "match_no": m.match_no if m else None, "round": m.round_name if m else None,
                              "team1": names.get(m.team1_id) if m else None, "team2": names.get(m.team2_id) if m else None,
                              "reporter": names.get(r.team_id), "tournament_id": m.tournament_id if m else None,
                              "team1_id": m.team1_id if m else None, "team2_id": m.team2_id if m else None})
        return {"items": items}

    @router.post("/admin/match-reports/{report_id}")
    async def admin_report_decide(report_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        from services import tournament_extras as extras

        try:
            out = await extras.decide_report(report_id, viewer.user_id, _flag(payload.get("approve")), cascade=_flag(payload.get("cascade")))
        except engine.EngineError as exc:
            return _fail(exc)
        invalidate_public("tournaments:")
        return {"ok": True, **out}

    @router.get("/tournaments/{tournament_id}/stats")
    async def t_stats(tournament_id: int, viewer: Viewer = Depends(viewer_dep)):
        from services import tournament_extras as extras

        async with session_scope() as session:
            await _visible_tournament(session, tournament_id, viewer)
        return await extras.tournament_stats(tournament_id)

    @router.get("/tournaments/{tournament_id}/mvp")
    async def t_mvp(tournament_id: int, viewer: Viewer = Depends(viewer_dep)):
        from services import tournament_extras as extras

        async with session_scope() as session:
            await _visible_tournament(session, tournament_id, viewer)
        state = await extras.mvp_state(tournament_id, viewer.user_id)
        state["audience"] = await extras.audience_state(tournament_id, viewer.user_id)
        return state

    @router.post("/tournaments/{tournament_id}/mvp")
    async def t_mvp_vote(tournament_id: int, payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        from services import tournament_extras as extras

        try:
            await extras.vote_mvp(tournament_id, viewer.user_id, int(payload.get("candidate_user_id") or 0))
        except engine.EngineError as exc:
            return _fail(exc)
        return {"ok": True}

    @router.get("/live")
    async def live(viewer: Viewer = Depends(viewer_dep)):
        """Матчи сегодня (МСК) во всех открытых турнирах — для главной страницы и зрителей."""
        from database.models import TournamentStatus
        from services.tournament_engine.service import STATUS_TEXT, _team_names

        day = timeutil.to_local(timeutil.now_utc()).strftime("%d.%m.%Y")
        async with session_scope() as session:
            rows = (await session.execute(
                select(TournamentMatch, Tournament).join(Tournament, Tournament.id == TournamentMatch.tournament_id)
                .where(TournamentMatch.scheduled_at.is_not(None), Tournament.status != TournamentStatus.DRAFT)
            )).all()
            todays = sorted([(m, t) for m, t in rows if (timeutil.fmt_local(m.scheduled_at) or "").startswith(day)],
                            key=lambda p: timeutil.as_utc(p[0].scheduled_at))
            names = await _team_names(session, {x for m, _ in todays for x in (m.team1_id, m.team2_id) if x})
        return {"day": day, "items": [{
            "id": int(m.id), "tournament_id": int(t.id), "tournament": t.name, "round": m.round_name, "best_of": m.best_of,
            "time": timeutil.to_local(m.scheduled_at).strftime("%H:%M"), "status": m.status, "status_text": STATUS_TEXT.get(m.status, m.status),
            "team1": names.get(m.team1_id) if m.team1_id else None, "team2": names.get(m.team2_id) if m.team2_id else None,
            "team1_id": m.team1_id, "team2_id": m.team2_id, "score1": m.score1, "score2": m.score2,
        } for m, t in todays]}

    @router.get("/matches/{match_id}/chat")
    async def match_chat(match_id: int, after_id: int = 0, viewer: Viewer = Depends(viewer_dep)):
        from database.models import MatchChatMessage, User
        from services.audience import chat_access

        async with session_scope() as session:
            m = await session.get(TournamentMatch, int(match_id))
            if m is None:
                raise HTTPException(status_code=404, detail="Матч не найден.")
            can_read, can_write, reason = await chat_access(session, m, viewer.user_id, viewer.is_admin)
            names = await engine._team_names(session, {m.team1_id, m.team2_id} - {None})
            head = {"match_id": int(m.id), "match_no": m.match_no, "round": m.round_name,
                    "team1": names.get(m.team1_id), "team2": names.get(m.team2_id), "when": timeutil.fmt_local(m.scheduled_at, label=True)}
            if not can_read:
                return {**head, "open": False, "reason": reason, "messages": [], "can_write": False}
            rows = (await session.execute(
                select(MatchChatMessage, User).outerjoin(User, User.id == MatchChatMessage.user_id)
                .where(MatchChatMessage.match_id == m.id, MatchChatMessage.id > int(after_id)).order_by(MatchChatMessage.id).limit(200)
            )).all()
        return {**head, "open": True, "can_write": can_write, "messages": [{
            "id": int(x.id), "system": bool(x.is_system), "text": x.text, "at": timeutil.fmt_local(x.created_at),
            "name": ("Организатор" if False else (" ".join(v for v in [u.first_name, u.last_name] if v) or u.username or "Игрок")) if u else "TI:IT TOP",
            "mine": bool(u and int(u.id) == viewer.user_id),
        } for x, u in rows]}

    @router.post("/matches/{match_id}/chat")
    async def match_chat_post(match_id: int, payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        from database.models import MatchChatMessage
        from services.audience import chat_access

        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Пустое сообщение.")
        async with session_scope() as session:
            m = await session.get(TournamentMatch, int(match_id))
            if m is None:
                raise HTTPException(status_code=404, detail="Матч не найден.")
            _, can_write, reason = await chat_access(session, m, viewer.user_id, viewer.is_admin)
            if not can_write:
                raise HTTPException(status_code=403, detail=reason or "Чат закрыт.")
            session.add(MatchChatMessage(match_id=m.id, user_id=viewer.user_id, text=text[:1000]))
            await session.commit()
        return {"ok": True}

    @router.post("/tournaments/{tournament_id}/audience-vote")
    async def t_audience_vote(tournament_id: int, payload: dict = Body(...), viewer: Viewer = Depends(viewer_dep)):
        from services import tournament_extras as extras

        try:
            await extras.vote_audience(tournament_id, viewer.user_id, int(payload.get("candidate_user_id") or 0))
        except engine.EngineError as exc:
            return _fail(exc)
        return {"ok": True}

    @router.get("/hall-of-fame")
    async def hall(viewer: Viewer = Depends(viewer_dep)):
        from services import tournament_extras as extras

        return {"items": await extras.hall_of_fame()}

    # ------------------------------------------------------------------
    # Админ: сетка
    # ------------------------------------------------------------------
    @router.get("/admin/engine/formats")
    async def formats(viewer: Viewer = Depends(admin_dep)):
        return {
            "formats": formats_catalog(),
            "seeding": [
                {"key": "standard", "title": "Стандартный посев по MMR (1 против последнего)"},
                {"key": "opening", "title": "Открывающий матч выбираю сам, остальные по MMR"},
            ],
            "best_of": [1, 3, 5],
            "best_of_groups": [1, 2, 3, 5],
            "playoff_formats": ["single_elim", "double_elim"],
            "tz": timeutil.tz_info(),
        }

    @router.post("/admin/tournaments/{tournament_id}/engine/generate")
    async def engine_generate(tournament_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        seeding_mode = str(payload.get("seeding") or "standard")
        if seeding_mode not in ("standard", "opening", "everyone"):
            raise HTTPException(status_code=400, detail="Неизвестный способ посева.")
        async with session_scope() as session:
            t = await _tournament(session, tournament_id)
            try:
                stage = await engine.generate(
                    session, t, fmt_key=str(payload.get("format") or ""), seeding_mode=seeding_mode,
                    settings=_settings_from(payload),
                    opening=(payload.get("opening_team_a"), payload.get("opening_team_b")) if seeding_mode == "opening" else None,
                    admin_user_id=viewer.user_id, lock=_flag(payload.get("lock")),
                )
            except engine.EngineError as exc:
                return _fail(exc)
            await add_event(session, kind="tournament_bracket", title="Сетка турнира сгенерирована", body=f"{t.name}: {stage.name}")
            await session.commit()
            view = await engine.stage_view(session, t, avatar_fn=avatar_url)
        invalidate_public("tournaments:")
        return {"ok": True, "engine": view}

    @router.post("/admin/tournaments/{tournament_id}/engine/import-legacy")
    async def engine_import(tournament_id: int, payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            t = await _tournament(session, tournament_id)
            try:
                stage = await engine.import_legacy(
                    session, t, fmt_key=str(payload.get("format") or "double_elim"),
                    settings=_settings_from(payload), admin_user_id=viewer.user_id,
                )
            except engine.EngineError as exc:
                return _fail(exc)
            await add_event(session, kind="tournament_bracket", title="Сетка перенесена в новый движок", body=f"{t.name}: {stage.name}")
            await session.commit()
            view = await engine.stage_view(session, t, avatar_fn=avatar_url)
        invalidate_public("tournaments:")
        return {"ok": True, "engine": view}

    @router.post("/admin/tournaments/{tournament_id}/engine/playoffs")
    async def engine_playoffs(tournament_id: int, viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            t = await _tournament(session, tournament_id)
            try:
                stage = await engine.create_playoffs(session, t, admin_user_id=viewer.user_id)
            except engine.EngineError as exc:
                return _fail(exc)
            await add_event(session, kind="tournament_bracket", title="Сформирован плей-офф", body=f"{t.name}: {stage.name}")
            await session.commit()
            view = await engine.stage_view(session, t, avatar_fn=avatar_url)
        invalidate_public("tournaments:")
        return {"ok": True, "engine": view}

    @router.get("/admin/tournaments/{tournament_id}/engine/pairs")
    async def engine_pairs(tournament_id: int, viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            t = await _tournament(session, tournament_id)
            try:
                data = await engine.pairs_view(session, t)
            except engine.EngineError as exc:
                return _fail(exc)
        data["tournament"] = {"id": int(t.id), "name": t.name}
        return data

    @router.post("/admin/tournaments/{tournament_id}/engine/autopair")
    async def engine_autopair(tournament_id: int, payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        """Добор пар вручную (то же, что делается при закрытии регистрации). dry_run — только показать план."""
        dry = bool(payload.get("dry_run"))
        async with session_scope() as session:
            t = await _tournament(session, tournament_id)
            try:
                report = await engine.autopair_leftovers(session, t, dry_run=dry, admin_user_id=viewer.user_id)
            except engine.EngineError as exc:
                return _fail(exc)
            if not dry and report.get("action") in ("rebuilt", "filled"):
                await add_event(session, kind="tournament_bracket", title="Пары добраны автоматически", body=f"{t.name}: {report.get('text')}")
                await session.commit()
        if not dry and report.get("action") in ("rebuilt", "filled"):
            notify.send_autopair(get_config(), report)
            invalidate_public("tournaments:")
        return {"ok": True, "dry_run": dry, **{k: report.get(k) for k in ("action", "text", "pairs", "byes", "left")}}

    @router.post("/admin/tournaments/{tournament_id}/engine/pairs")
    async def engine_pairs_save(tournament_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            t = await _tournament(session, tournament_id)
            try:
                changed = await engine.set_pairs(session, t, payload.get("pairs"), admin_user_id=viewer.user_id)
            except engine.EngineError as exc:
                return _fail(exc)
            if changed:
                await add_event(session, kind="tournament_bracket", title="Пары матчей обновлены", body=f"{t.name}: изменено матчей — {changed}")
            await session.commit()
        if changed:
            invalidate_public("tournaments:")
        return {"ok": True, "changed": changed}

    @router.get("/public/tournaments/{tournament_id}/results.png", include_in_schema=False)
    async def public_results_png(tournament_id: int):
        """5.13: картинка с итогами (для табло и чтобы переслать вручную)."""
        from fastapi.responses import Response

        from services.results_card import card_for

        async with session_scope() as session:
            t = await session.get(Tournament, int(tournament_id))
            if t is None or t.status == TournamentStatus.DRAFT:
                raise HTTPException(status_code=404, detail="Турнир не найден.")
        card = await card_for(int(tournament_id))
        if card is None:
            raise HTTPException(status_code=404, detail="Итогов ещё нет.")
        return Response(content=card[0], media_type="image/png", headers={"Cache-Control": "public, max-age=60"})

    @router.get("/public/tournaments/{tournament_id}")
    async def public_board(tournament_id: int):
        """Публичные данные для страницы проектора/стрима: без авторизации и без личных данных игроков."""
        async with session_scope() as session:
            t = await session.get(Tournament, int(tournament_id))
            if t is None or t.status == TournamentStatus.DRAFT:
                raise HTTPException(status_code=404, detail="Турнир не найден.")
            view = await engine.stage_view(session, t, avatar_fn=avatar_url)
            sched = await engine.schedule_view(session, t, avatar_fn=avatar_url)
            from database.models import TournamentMatchGame

            games: dict[str, list[dict[str, Any]]] = {}
            for g in (await session.execute(select(TournamentMatchGame).join(TournamentMatch, TournamentMatch.id == TournamentMatchGame.match_id)
                                            .where(TournamentMatch.tournament_id == t.id).order_by(TournamentMatchGame.game_no))).scalars().all():
                games.setdefault(str(g.match_id), []).append({"no": g.game_no, "winner_team_id": g.winner_team_id, "dota_match_id": g.dota_match_id})
        from services.audience import TWITCH_URL

        return {"tournament": {"id": int(t.id), "name": t.name, "status": t.status.value, "start": timeutil.fmt_local(t.start_date, label=True)},
                "engine": view, "schedule": sched.get("items", []), "games": games, "stream": TWITCH_URL, "tz": timeutil.tz_info()}

    @router.post("/admin/tournaments/{tournament_id}/engine/lock")
    async def engine_lock(tournament_id: int, viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            t = await _tournament(session, tournament_id)
            try:
                await engine.lock_stage(session, t, admin_user_id=viewer.user_id)
            except engine.EngineError as exc:
                return _fail(exc)
            await add_event(session, kind="tournament_bracket", title="Сетка турнира зафиксирована", body=t.name)
            await session.commit()
        invalidate_public("tournaments:")
        return {"ok": True}

    @router.post("/admin/tournaments/{tournament_id}/engine/reset")
    async def engine_reset(tournament_id: int, payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            t = await _tournament(session, tournament_id)
            try:
                await engine.reset_stage(session, t, admin_user_id=viewer.user_id, force=_flag(payload.get("force")))
            except engine.EngineError as exc:
                return _fail(exc)
            await add_event(session, kind="tournament_bracket", title="Сетка турнира сброшена", body=t.name)
            await session.commit()
        invalidate_public("tournaments:")
        return {"ok": True}

    # ------------------------------------------------------------------
    # Админ: матч
    # ------------------------------------------------------------------
    async def _names(session, m: TournamentMatch) -> tuple[str, str]:
        names = await engine._team_names(session, {m.team1_id, m.team2_id})
        return names.get(m.team1_id, "TBD"), names.get(m.team2_id, "TBD")

    @router.post("/admin/matches/{match_id}/schedule")
    async def match_schedule(match_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        telegram_ids: list[int] = []
        text = ""
        async with session_scope() as session:
            try:
                out = await engine.set_schedule(
                    session, match_id, payload.get("when"), postpone=_flag(payload.get("postpone")),
                    force=_flag(payload.get("force")), admin_user_id=viewer.user_id,
                )
            except engine.EngineError as exc:
                return _fail(exc)
            m = out["match"]
            if m.team1_id and m.team2_id and out["current"] != out["previous"]:
                a, b = await _names(session, m)
                when = timeutil.fmt_local(out["current"], label=True) or "время уточняется"
                if out["postponed"]:
                    kind, title = "match_postponed", f"Матч перенесён: {a} — {b}"
                    icon = "⏰"
                else:
                    kind, title = "match_scheduled", f"Матч назначен: {a} — {b}"
                    icon = "📅"
                body = f"{m.round_name} · BO{m.best_of} · {when}"
                telegram_ids = await _notify_match(session, m, kind, title, body)
                text = f"{icon} <b>{html_escape(title)}</b>\n{html_escape(m.round_name or '')} · BO{m.best_of}\n🕒 {html_escape(when)}"
            await session.commit()
        if telegram_ids:
            _send_telegram(telegram_ids, text, "match_schedule")
        invalidate_public("tournaments:")
        return {"ok": True, "status": m.status, "scheduled_text": timeutil.fmt_local(m.scheduled_at)}

    @router.post("/admin/matches/{match_id}/result")
    async def match_result(match_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        forfeit = payload.get("forfeit_winner")
        async with session_scope() as session:
            try:
                out = await engine.set_result(
                    session, match_id, score1=payload.get("score1"), score2=payload.get("score2"),
                    forfeit_winner=int(forfeit) if forfeit not in (None, "", 0, "0") else None,
                    cascade=_flag(payload.get("cascade")),
                    expected_version=payload.get("version"), admin_user_id=viewer.user_id,
                )
            except (ValueError, TypeError) as exc:
                if isinstance(exc, engine.EngineError):
                    return _fail(exc)
                raise HTTPException(status_code=400, detail="Неверные данные результата.") from None
            m = out["match"]
            a, b = await _names(session, m)
            t = await session.get(Tournament, m.tournament_id)
            score = f"{m.score1}:{m.score2}" + (" (тех.)" if m.result_type == "forfeit" else "")
            title = f"{a} {score} {b}"
            body = f"{t.name if t else ''} · {m.round_name} · матч {m.match_no}"
            await add_event(session, kind="match_result", title=title, body=body)
            telegram_ids = await _notify_match(session, m, "match_result", f"Результат: {title}", body)
            from services.audience import follower_telegram_ids

            telegram_ids += [tg for tg in await follower_telegram_ids(session, [m.team1_id, m.team2_id]) if tg not in telegram_ids]
            await session.commit()
        _send_telegram(telegram_ids, f"🏁 <b>Результат матча</b>\n{html_escape(title)}\n{html_escape(body)}", "match_result")
        invalidate_public("tournaments:")
        return {"ok": True, "affected": out["affected"], "winner_changed": out["winner_changed"]}

    @router.post("/admin/matches/{match_id}/reset")
    async def match_reset(match_id: int, payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            try:
                out = await engine.reset_result(session, match_id, cascade=_flag(payload.get("cascade")), admin_user_id=viewer.user_id)
            except engine.EngineError as exc:
                return _fail(exc)
            await session.commit()
        invalidate_public("tournaments:")
        return {"ok": True, "affected": out["affected"]}

    @router.post("/admin/matches/{match_id}/status")
    async def match_status(match_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            try:
                m = await engine.set_status(session, match_id, str(payload.get("status") or ""), admin_user_id=viewer.user_id)
            except engine.EngineError as exc:
                return _fail(exc)
            await session.commit()
            status = m.status
        invalidate_public("tournaments:")
        return {"ok": True, "status": status}

    @router.post("/admin/matches/{match_id}/teams")
    async def match_teams(match_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            try:
                m = await engine.set_teams(session, match_id, payload.get("team1_id"), payload.get("team2_id"),
                                           force=_flag(payload.get("force")), admin_user_id=viewer.user_id)
            except engine.EngineError as exc:
                return _fail(exc)
            await session.commit()
            status = m.status
        invalidate_public("tournaments:")
        return {"ok": True, "status": status}

    @router.post("/admin/tournaments/{tournament_id}/engine/matches")
    async def engine_add_match(tournament_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            t = await _tournament(session, tournament_id)
            try:
                m = await engine.add_manual_match(
                    session, t, team1_id=payload.get("team1_id"), team2_id=payload.get("team2_id"),
                    best_of=payload.get("best_of", 1), when=payload.get("when"), round_no=payload.get("round_no"),
                    admin_user_id=viewer.user_id,
                )
            except engine.EngineError as exc:
                return _fail(exc)
            await session.commit()
            match_id = int(m.id)
        invalidate_public("tournaments:")
        return {"ok": True, "match_id": match_id}

    @router.delete("/admin/matches/{match_id}")
    async def engine_delete_match(match_id: int, viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            try:
                await engine.delete_match(session, match_id, admin_user_id=viewer.user_id)
            except engine.EngineError as exc:
                return _fail(exc)
            await session.commit()
        invalidate_public("tournaments:")
        return {"ok": True}

    @router.post("/admin/matches/{match_id}/best-of")
    async def match_best_of(match_id: int, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        async with session_scope() as session:
            try:
                m = await engine.set_best_of(session, match_id, payload.get("best_of"), admin_user_id=viewer.user_id)
            except engine.EngineError as exc:
                return _fail(exc)
            await session.commit()
            bo = m.best_of
        invalidate_public("tournaments:")
        return {"ok": True, "best_of": bo}

    return router
