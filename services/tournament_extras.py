"""5.8: лобби и готовность к матчу, результаты от капитанов, зал славы, MVP, статистика, ежедневный бэкап."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from database.engine import session_scope
from database.models import (
    AudienceVote,
    MatchReport,
    MvpVote,
    StageParticipant,
    Team,
    TeamMember,
    TeamRole,
    Tournament,
    TournamentMatch,
    TournamentMatchLog,
    TournamentStatus,
    User,
)
from services.tournament_engine import service as engine
from services.tournament_engine import timeutil
from services.tournament_engine.formats import FORMATS
from services.tournament_engine.series import SeriesError, validate_score

logger = logging.getLogger("TOURNAMENT_EXTRAS")
PROJECT_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = PROJECT_DIR / "miniapp" / "data" / "match_reports"
MAX_SCREENSHOT = 8 * 1024 * 1024
ACTIVE_REPORT = ("PENDING", "CONFIRMED", "DISPUTED")

# ---------------------------------------------------------------------------
# Лобби и готовность
# ---------------------------------------------------------------------------
_ALPH = "abcdefghjkmnpqrstuvwxyz23456789"


def lobby_for(m: TournamentMatch, secret: str) -> dict[str, str]:
    """Название и пароль лобби матча: стабильные, выводятся из секрета бота (сам секрет не раскрывается)."""
    digest = hmac.new((secret or "ti-it-top").encode(), f"lobby:{m.id}:{m.stage_id}".encode(), hashlib.sha256).digest()
    return {"name": f"TIIT {m.tournament_id}-{m.match_no}", "password": "".join(_ALPH[b % len(_ALPH)] for b in digest[:6])}


async def ready_teams(session, match_id: int) -> set[int]:
    rows = (await session.execute(
        select(TournamentMatchLog.new_json).where(TournamentMatchLog.match_id == int(match_id), TournamentMatchLog.action == "ready")
    )).scalars().all()
    out: set[int] = set()
    for raw in rows:
        try:
            out.add(int(json.loads(raw or "{}").get("team_id")))
        except (TypeError, ValueError):
            continue
    return out


async def user_team_id(session, user_id: int) -> int | None:
    tid = await session.scalar(select(TeamMember.team_id).where(TeamMember.user_id == int(user_id)).limit(1))
    return int(tid) if tid else None


async def mark_ready(match_id: int, telegram_id: int) -> tuple[bool, str]:
    async with session_scope() as s:
        m = await s.get(TournamentMatch, int(match_id))
        if m is None or m.status == "FINISHED":
            return False, "Матч уже сыгран или не найден."
        user = await s.scalar(select(User).where(User.telegram_id == int(telegram_id)))
        team_id = await user_team_id(s, user.id) if user else None
        if not team_id or team_id not in (m.team1_id, m.team2_id):
            return False, "Вы не участник этого матча."
        if team_id in await ready_teams(s, m.id):
            return True, "Готовность вашей команды уже отмечена."
        s.add(TournamentMatchLog(match_id=m.id, stage_id=m.stage_id, action="ready",
                                 new_json=json.dumps({"team_id": team_id, "user_id": int(user.id)})))
        await s.commit()
        return True, "Готовность команды отмечена. Удачной игры!"


def noshow_grace() -> int:
    try:
        return max(0, min(120, int(os.getenv("NOSHOW_GRACE_MINUTES", "15"))))
    except ValueError:
        return 15


async def noshow_check(now: datetime | None = None) -> list[str]:
    return [a["text"] for a in await noshow_alerts(now)]


async def noshow_alerts(now: datetime | None = None) -> list[dict[str, Any]]:
    """Через NOSHOW_GRACE_MINUTES (15) после начала матча: кто не подтвердил готовность. Админам — один раз,
    с кнопками «Присудить победу» (5.13)."""
    now = now or timeutil.now_utc()
    grace = timedelta(minutes=noshow_grace())
    alerts: list[dict[str, Any]] = []
    async with session_scope() as s:
        rows = (await s.execute(select(TournamentMatch).where(
            TournamentMatch.status.in_(("SCHEDULED", "POSTPONED", "LIVE")), TournamentMatch.scheduled_at.is_not(None),
            TournamentMatch.team1_id.is_not(None), TournamentMatch.team2_id.is_not(None)))).scalars().all()
        for m in rows:
            at = timeutil.as_utc(m.scheduled_at)
            if not (at + grace <= now <= at + grace + timedelta(minutes=45)):
                continue
            done = (await s.execute(select(TournamentMatchLog.new_json).where(
                TournamentMatchLog.match_id == m.id, TournamentMatchLog.action == "noshow_check"))).scalars().all()
            if any((json.loads(x or "{}") or {}).get("scheduled_at") == at.isoformat() for x in done):
                continue
            ready = await ready_teams(s, m.id)
            missing = [t for t in (m.team1_id, m.team2_id) if t not in ready]
            s.add(TournamentMatchLog(match_id=m.id, stage_id=m.stage_id, action="noshow_check",
                                     new_json=json.dumps({"scheduled_at": at.isoformat(), "missing": missing})))
            if missing:
                names = await engine._team_names(s, {m.team1_id, m.team2_id})
                t = await s.get(Tournament, m.tournament_id)
                late = int(grace.total_seconds() // 60)
                # Победу можно присудить команде, которая готова; если не готовы обе — любой из двух.
                winners = [slot for slot, tid in ((1, m.team1_id), (2, m.team2_id)) if tid not in missing] or [1, 2]
                alerts.append({
                    "match_id": int(m.id),
                    "text": (f"Матч {m.match_no} ({t.name if t else ''}, {m.round_name}) начался в "
                             f"{timeutil.fmt_local(at, label=True)}, прошло {late} мин. "
                             f"Не подтвердили готовность: {', '.join(names.get(x, str(x)) for x in missing)}.\n"
                             "Если команда не пришла — присудите техническую победу кнопкой ниже (или в Mini App → матч)."),
                    "buttons": [(f"Победа: {names.get(m.team1_id if slot == 1 else m.team2_id, '?')}"[:60], slot) for slot in winners],
                })
        await s.commit()
    return alerts


# ---------------------------------------------------------------------------
# Результаты от капитанов
# ---------------------------------------------------------------------------

def _save_screenshot(report_id: int, raw: bytes) -> str:
    from PIL import Image, ImageOps

    with Image.open(BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((1600, 1600))
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"report_{int(report_id)}.jpg"
        img.save(REPORT_DIR / name, format="JPEG", quality=85, optimize=True)
    return name


async def _captain_team(session, user_id: int, m: TournamentMatch) -> int | None:
    tid = await session.scalar(select(Team.id).where(Team.captain_user_id == int(user_id), Team.id.in_([m.team1_id, m.team2_id])))
    return int(tid) if tid else None


async def create_report(match_id: int, user_id: int, score1: Any, score2: Any, screenshot: bytes | None,
                        dota_ids: list[int] | None = None, *, _dota_data=None) -> dict[str, Any]:
    from services import opendota_match as od

    raw = None
    if dota_ids:  # 5.13: результат по ID матчей Dota — данные OpenDota качаем до транзакции
        try:
            raw = _dota_data if _dota_data is not None else await od.collect(dota_ids)
        except od.MatchDataError as exc:
            raise engine.EngineError(str(exc)) from None
    async with session_scope() as s:
        m, stage, fmt, _ = await engine._load_match(s, match_id)
        if m.status == "FINISHED":
            raise engine.EngineError("Результат этого матча уже внесён.")
        if not (m.team1_id and m.team2_id):
            raise engine.EngineError("Команды матча ещё не определены.")
        team_id = await _captain_team(s, user_id, m)
        if team_id is None:
            raise engine.EngineError("Результат присылает капитан одной из команд матча.", status=403)
        dota = None
        if raw is not None:
            try:
                dota = await od.analyze(s, m, dota_ids, raw[0], raw[1])
            except od.MatchDataError as exc:
                raise engine.EngineError(str(exc)) from None
            score1, score2 = dota["score1"], dota["score2"]
        try:
            validate_score(m.best_of, score1, score2, allow_draw=fmt.allow_draw)
        except SeriesError as exc:
            if dota is not None:
                raise engine.EngineError(f"По этим матчам счёт {score1}:{score2} — серия BO{m.best_of} не завершена. "
                                         "Добавьте ID остальных карт.") from None
            raise engine.EngineError(str(exc)) from None
        if await s.scalar(select(MatchReport.id).where(MatchReport.match_id == m.id, MatchReport.status.in_(ACTIVE_REPORT))):
            raise engine.EngineError("По этому матчу уже есть результат на проверке.")
        if screenshot and len(screenshot) > MAX_SCREENSHOT:
            raise engine.EngineError("Скриншот больше 8 МБ.")
        verified = bool(dota and dota["confident"])
        # Подтверждённое данными OpenDota не ждёт соперника — сразу к организатору.
        row = MatchReport(match_id=m.id, team_id=team_id, user_id=int(user_id), score1=int(score1), score2=int(score2),
                          status="CONFIRMED" if verified else "PENDING", comment="Подтверждено OpenDota" if verified else None)
        s.add(row)
        await s.flush()
        if dota is not None:
            from database.models import MatchReportDota

            s.add(MatchReportDota(report_id=row.id, data_json=json.dumps(dota, ensure_ascii=False), verified=verified))
        if screenshot:
            try:
                row.screenshot = await asyncio.to_thread(_save_screenshot, row.id, screenshot)
            except Exception:  # noqa: BLE001
                raise engine.EngineError("Не удалось прочитать изображение — пришлите PNG или JPG.") from None
        other = m.team2_id if team_id == m.team1_id else m.team1_id
        opp = await s.scalar(select(User.telegram_id).join(Team, Team.captain_user_id == User.id).where(Team.id == other))
        names = await engine._team_names(s, {m.team1_id, m.team2_id})
        await s.commit()
        return {"id": int(row.id), "match_no": m.match_no, "opponent_tg": opp, "verified": verified, "dota": dota,
                "text": f"{names.get(m.team1_id)} {row.score1}:{row.score2} {names.get(m.team2_id)}", "reporter": names.get(team_id)}


async def respond_report(report_id: int, user_id: int, confirm: bool, comment: str | None) -> dict[str, Any]:
    async with session_scope() as s:
        row = await s.get(MatchReport, int(report_id))
        if row is None or row.status != "PENDING":
            raise engine.EngineError("Результат уже рассмотрен или не найден.")
        m = await s.get(TournamentMatch, row.match_id)
        team_id = await _captain_team(s, user_id, m)
        if team_id is None or team_id == row.team_id:
            raise engine.EngineError("Подтверждает капитан команды-соперника.", status=403)
        row.status = "CONFIRMED" if confirm else "DISPUTED"
        row.opponent_user_id = int(user_id)
        row.comment = (comment or "").strip()[:500] or None
        await s.commit()
        return {"id": int(row.id), "status": row.status, "match_no": m.match_no}


async def decide_report(report_id: int, admin_user_id: int, approve: bool, *, cascade: bool = False) -> dict[str, Any]:
    async with session_scope() as s:
        row = await s.get(MatchReport, int(report_id))
        if row is None or row.status not in ACTIVE_REPORT:
            raise engine.EngineError("Результат уже рассмотрен или не найден.")
        if approve:
            await engine.set_result(s, row.match_id, score1=row.score1, score2=row.score2, cascade=cascade, admin_user_id=admin_user_id)
            from database.models import MatchReportDota, TournamentMatchGame

            dota = await s.scalar(select(MatchReportDota).where(MatchReportDota.report_id == row.id))
            if dota is not None:  # 5.13: игры серии с ID матчей Dota — в историю матча
                for g in (json.loads(dota.data_json) or {}).get("games") or []:
                    s.add(TournamentMatchGame(match_id=row.match_id, game_no=int(g["no"]), winner_team_id=int(g["winner_team_id"]),
                                              dota_match_id=int(g["dota_match_id"])))
        row.status = "APPROVED" if approve else "REJECTED"
        row.decided_by = int(admin_user_id)
        await s.commit()
        return {"id": int(row.id), "status": row.status, "match_id": int(row.match_id)}


def report_json(row: MatchReport, dota: Any = None) -> dict[str, Any]:
    out = {"id": int(row.id), "match_id": int(row.match_id), "team_id": int(row.team_id), "score1": row.score1, "score2": row.score2,
           "status": row.status, "comment": row.comment, "created_at": timeutil.fmt_local(row.created_at),
           "screenshot_url": f"/api/v2/match-reports/{row.id}/screenshot" if row.screenshot else None, "dota": None}
    if dota is not None:
        try:
            out["dota"] = {**json.loads(dota.data_json), "verified": bool(dota.verified)}
        except ValueError:
            pass
    return out


async def report_dota(session, report_id: int):
    from database.models import MatchReportDota

    return await session.scalar(select(MatchReportDota).where(MatchReportDota.report_id == int(report_id)))


async def active_report(session, match_id: int) -> MatchReport | None:
    return await session.scalar(select(MatchReport).where(MatchReport.match_id == int(match_id), MatchReport.status.in_(ACTIVE_REPORT))
                                .order_by(MatchReport.id.desc()).limit(1))


# ---------------------------------------------------------------------------
# Места, зал славы, MVP, статистика
# ---------------------------------------------------------------------------

async def final_places(session, stage) -> list[tuple[int, int]]:
    fmt = FORMATS.get(stage.format)
    if fmt is None:
        return []
    matches = await engine._stage_matches(session, stage.id)
    if fmt.table:
        st = await engine._standings_for(session, stage, fmt, engine._settings(stage), matches)
        rows = st[0]["rows"] if len(st) == 1 else []
        return [(r["place"], int(r["team_id"])) for r in rows[:3]]
    done = [m for m in matches if m.status == "FINISHED" and m.winner_team_id]

    def loser(m):
        return m.team2_id if m.winner_team_id == m.team1_id else m.team1_id

    finals = [m for m in done if m.bracket in ("main", "final")]
    if not finals:
        return []
    last_round = max(m.round_no for m in finals if m.bracket == finals[0].bracket)
    final = max((m for m in finals if m.round_no == last_round or m.bracket == "final"), key=lambda m: m.match_no)
    places = [(1, int(final.winner_team_id)), (2, int(loser(final)))]
    if stage.format == "double_elim":
        lower = [m for m in done if m.bracket == "lower"]
        if lower:
            places.append((3, int(loser(max(lower, key=lambda m: (m.round_no, m.match_no))))))
    else:
        third = [m for m in done if m.bracket == "third"]
        if third:
            places.append((3, int(third[0].winner_team_id)))
        else:
            semis = [m for m in done if m.bracket == "main" and m.round_no == last_round - 1]
            places += [(3, int(loser(m))) for m in semis]
    return places


async def mvp_results(session, tournament_id: int) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(MvpVote.candidate_user_id, func.count(MvpVote.id)).where(MvpVote.tournament_id == int(tournament_id))
        .group_by(MvpVote.candidate_user_id).order_by(func.count(MvpVote.id).desc())
    )).all()
    return [{"user_id": int(u), "votes": int(n)} for u, n in rows]


async def hall_of_fame() -> list[dict[str, Any]]:
    out = []
    async with session_scope() as s:
        tournaments = (await s.execute(select(Tournament).where(Tournament.status != TournamentStatus.DRAFT).order_by(Tournament.id.desc()))).scalars().all()
        for t in tournaments:
            stages = await engine.get_stages(s, t.id)
            if not stages or stages[-1].status != "FINISHED":
                continue
            places = await final_places(s, stages[-1])
            names = await engine._team_names(s, {tid for _, tid in places})
            mvp = (await mvp_results(s, t.id))[:1]
            mvp_user = await s.get(User, mvp[0]["user_id"]) if mvp else None
            aud = (await audience_results(s, t.id))[:1]
            aud_user = await s.get(User, aud[0]["user_id"]) if aud else None
            out.append({"tournament_id": int(t.id), "name": t.name, "date": timeutil.fmt_local(t.start_date),
                        "places": [{"place": p, "team_id": tid, "team": names.get(tid, "Команда удалена")} for p, tid in places],
                        "mvp": {"name": " ".join(x for x in [mvp_user.first_name, mvp_user.last_name] if x) or mvp_user.username,
                                "votes": mvp[0]["votes"]} if mvp_user else None,
                        "audience": {"name": " ".join(x for x in [aud_user.first_name, aud_user.last_name] if x) or aud_user.username,
                                     "votes": aud[0]["votes"]} if aud_user else None})
    return out


async def _participant_team_ids(session, tournament_id: int) -> set[int]:
    stages = await engine.get_stages(session, tournament_id)
    ids: set[int] = set()
    for st in stages:
        ids.update(int(x) for x in (await session.execute(select(StageParticipant.team_id).where(StageParticipant.stage_id == st.id))).scalars().all())
    return ids


async def mvp_state(tournament_id: int, viewer_user_id: int) -> dict[str, Any]:
    async with session_scope() as s:
        t = await s.get(Tournament, int(tournament_id))
        stages = await engine.get_stages(s, int(tournament_id))
        is_open = bool(stages and stages[-1].status == "FINISHED") or (t is not None and t.status == TournamentStatus.FINISHED)
        teams = await _participant_team_ids(s, int(tournament_id))
        rows = (await s.execute(
            select(User, TeamMember.team_id, Team.name).join(TeamMember, TeamMember.user_id == User.id).join(Team, Team.id == TeamMember.team_id)
            .where(TeamMember.team_id.in_(teams or {0}), TeamMember.role != TeamRole.COACH)
        )).all()
        votes = {r["user_id"]: r["votes"] for r in await mvp_results(s, int(tournament_id))}
        mine = await s.scalar(select(MvpVote.candidate_user_id).where(MvpVote.tournament_id == int(tournament_id), MvpVote.voter_user_id == int(viewer_user_id)))
        viewer_team = await user_team_id(s, viewer_user_id)
    candidates = sorted(({
        "user_id": int(u.id), "name": " ".join(x for x in [u.first_name, u.last_name] if x) or u.username or str(u.telegram_id),
        "username": u.username, "team_id": int(tid), "team": tname, "votes": votes.get(int(u.id), 0),
    } for u, tid, tname in rows), key=lambda c: (-c["votes"], c["team"].casefold(), c["name"].casefold()))
    return {"open": is_open, "can_vote": is_open and viewer_team in teams, "my_vote": int(mine) if mine else None,
            "candidates": candidates, "total_votes": sum(votes.values())}


async def vote_mvp(tournament_id: int, voter_user_id: int, candidate_user_id: int) -> None:
    state = await mvp_state(tournament_id, voter_user_id)
    if not state["open"]:
        raise engine.EngineError("Голосование за MVP откроется после финала.")
    if not state["can_vote"]:
        raise engine.EngineError("Голосуют игроки команд-участниц турнира.", status=403)
    if int(candidate_user_id) == int(voter_user_id):
        raise engine.EngineError("За себя голосовать нельзя.")
    if int(candidate_user_id) not in {c["user_id"] for c in state["candidates"]}:
        raise engine.EngineError("Этот игрок не участвовал в турнире.")
    async with session_scope() as s:
        row = await s.scalar(select(MvpVote).where(MvpVote.tournament_id == int(tournament_id), MvpVote.voter_user_id == int(voter_user_id)))
        if row is None:
            s.add(MvpVote(tournament_id=int(tournament_id), voter_user_id=int(voter_user_id), candidate_user_id=int(candidate_user_id)))
        else:
            row.candidate_user_id = int(candidate_user_id)
        await s.commit()


async def tournament_stats(tournament_id: int) -> dict[str, Any]:
    async with session_scope() as s:
        stages = await engine.get_stages(s, int(tournament_id))
        matches: list[TournamentMatch] = []
        seeds: dict[int, int] = {}
        for st in stages:
            matches += await engine._stage_matches(s, st.id)
            for tid, seed in (await s.execute(select(StageParticipant.team_id, StageParticipant.seed).where(StageParticipant.stage_id == st.id))).all():
                if seed is not None and st.order_no == 1:
                    seeds[int(tid)] = int(seed)
        done = [m for m in matches if m.status == "FINISHED" and m.team1_id and m.team2_id]
        names = await engine._team_names(s, {x for m in done for x in (m.team1_id, m.team2_id)})
    per: dict[int, dict[str, int]] = {}
    for m in done:
        for tid, won, lost in ((m.team1_id, m.score1 or 0, m.score2 or 0), (m.team2_id, m.score2 or 0, m.score1 or 0)):
            r = per.setdefault(int(tid), {"series_w": 0, "series_l": 0, "maps_w": 0, "maps_l": 0})
            r["maps_w"] += won
            r["maps_l"] += lost
            if m.winner_team_id:
                r["series_w" if m.winner_team_id == tid else "series_l"] += 1

    def label(m):
        return {"match_no": m.match_no, "round": m.round_name, "text": f"{names.get(m.team1_id)} {m.score1}:{m.score2} {names.get(m.team2_id)}"}

    longest = sorted(done, key=lambda m: (-(int(m.score1 or 0) + int(m.score2 or 0)), m.match_no))[:3]
    upsets = []
    for m in done:
        if m.winner_team_id and m.result_type != "forfeit":
            lo = m.team2_id if m.winner_team_id == m.team1_id else m.team1_id
            if seeds.get(int(m.winner_team_id), 0) - seeds.get(int(lo), 0) >= 3:
                upsets.append({**label(m), "winner_seed": seeds.get(int(m.winner_team_id)), "loser_seed": seeds.get(int(lo))})
    teams = [{"team_id": tid, "team": names.get(tid), **r,
              "series_winrate": round(r["series_w"] * 100 / (r["series_w"] + r["series_l"])) if (r["series_w"] + r["series_l"]) else None}
             for tid, r in per.items()]
    best = sorted([t for t in teams if t["series_w"] + t["series_l"] >= 2], key=lambda t: (-(t["series_winrate"] or 0), -t["maps_w"]))[:3]
    return {
        "series_played": len(done),
        "series_total": len(matches),
        "maps_played": sum(int(m.score1 or 0) + int(m.score2 or 0) for m in done if m.result_type != "forfeit"),
        "forfeits": sum(1 for m in done if m.result_type == "forfeit"),
        "longest": [label(m) for m in longest if (m.score1 or 0) + (m.score2 or 0) > 1],
        "upsets": upsets[:5],
        "best_teams": best,
        "most_maps": sorted(teams, key=lambda t: -t["maps_w"])[:3],
    }


# ---------------------------------------------------------------------------
# Ежедневный бэкап базы
# ---------------------------------------------------------------------------

def backup_once(db_path: str, dest_dir: Path, keep: int = 14) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"tournament_{datetime.now(timezone.utc):%Y%m%d_%H%M}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        target.unlink(missing_ok=True)
        raise RuntimeError(f"integrity_check: {ok}")
    for old in sorted(dest_dir.glob("tournament_*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return target


async def backup_loop(db_path: str, dest_dir: Path, *, keep: int = 14, first_delay: float = 300.0, every: float = 86400.0,
                      on_done=None) -> None:
    await asyncio.sleep(first_delay)
    while True:
        try:
            path = await asyncio.to_thread(backup_once, db_path, dest_dir, keep)
            logger.info("[BACKUP] daily backup: %s", path)
            if on_done is not None:  # 5.13: копия вне сервера (в Telegram владельцу)
                try:
                    await on_done(path)
                except Exception:  # noqa: BLE001
                    logger.exception("[BACKUP] offsite copy failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[BACKUP] daily backup failed")
        await asyncio.sleep(every)


# ---------------------------------------------------------------------------
# 5.9.1: приз зрительских симпатий
# ---------------------------------------------------------------------------

async def audience_results(session, tournament_id: int) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(AudienceVote.candidate_user_id, func.count(AudienceVote.id)).where(AudienceVote.tournament_id == int(tournament_id))
        .group_by(AudienceVote.candidate_user_id).order_by(func.count(AudienceVote.id).desc())
    )).all()
    return [{"user_id": int(u), "votes": int(n)} for u, n in rows]


async def audience_state(tournament_id: int, viewer_user_id: int) -> dict[str, Any]:
    """Голосуют все, кто зашёл в бота до старта турнира (защита от накрутки свежими аккаунтами)."""
    base = await mvp_state(tournament_id, viewer_user_id)
    async with session_scope() as s:
        t = await s.get(Tournament, int(tournament_id))
        user = await s.get(User, int(viewer_user_id))
        votes = {r["user_id"]: r["votes"] for r in await audience_results(s, int(tournament_id))}
        mine = await s.scalar(select(AudienceVote.candidate_user_id).where(
            AudienceVote.tournament_id == int(tournament_id), AudienceVote.voter_user_id == int(viewer_user_id)))
    start = timeutil.as_utc(t.start_date) if t else None
    joined = timeutil.as_utc(user.created_at) if user else None
    fresh = bool(start and joined and joined >= start)
    reason = None
    if not base["open"]:
        reason = "Голосование откроется после финала."
    elif fresh:
        reason = "Голосуют аккаунты, которые зашли в бота до начала турнира."
    candidates = sorted(({**c, "votes": votes.get(c["user_id"], 0)} for c in base["candidates"]),
                        key=lambda c: (-c["votes"], c["team"].casefold(), c["name"].casefold()))
    return {"open": base["open"], "can_vote": base["open"] and not fresh, "reason": reason,
            "my_vote": int(mine) if mine else None, "candidates": candidates, "total_votes": sum(votes.values())}


async def vote_audience(tournament_id: int, voter_user_id: int, candidate_user_id: int) -> None:
    state = await audience_state(tournament_id, voter_user_id)
    if not state["can_vote"]:
        raise engine.EngineError(state["reason"] or "Голосование закрыто.", status=403)
    if int(candidate_user_id) == int(voter_user_id):
        raise engine.EngineError("За себя голосовать нельзя.")
    if int(candidate_user_id) not in {c["user_id"] for c in state["candidates"]}:
        raise engine.EngineError("Этот игрок не участвовал в турнире.")
    async with session_scope() as s:
        row = await s.scalar(select(AudienceVote).where(AudienceVote.tournament_id == int(tournament_id), AudienceVote.voter_user_id == int(voter_user_id)))
        if row is None:
            s.add(AudienceVote(tournament_id=int(tournament_id), voter_user_id=int(voter_user_id), candidate_user_id=int(candidate_user_id)))
        else:
            row.candidate_user_id = int(candidate_user_id)
        await s.commit()
