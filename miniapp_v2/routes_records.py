"""5.15: записи трансляций (OBS) — список, просмотр с главами по матчам, правка админом.

Видео и обложки лежат вне проекта (RECORDINGS_DIR, по умолчанию /opt/ti-it-top/RECORDINGS),
чтобы деплой их не трогал. Рядом с каждым ``<id>.mp4`` — ``<id>.json`` с описанием и
``<id>.jpg`` (обложка). Файлы кладёт загрузчик с ПК (tools/upload_recordings.py) через SFTP,
описание пишется последним — пока его нет, запись в списке не видна.
Сами файлы отдаёт nginx (location /rec/ с alias) — с перемоткой и без нагрузки на бота;
приложение тоже монтирует /rec как запасной путь (локальная отладка, тесты).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import or_, select

from database.engine import session_scope
from database.models import Tournament, TournamentMatch, TournamentStatus
from miniapp_v2.common import SafeRoute, Viewer, admin_dep, clean_text, viewer_dep
from services.tournament_engine import timeutil

logger = logging.getLogger("MINIAPP_V2.RECORDS")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FILE_RE = re.compile(r"^([A-Za-z0-9_-]{1,64})\.(mp4|jpg)$")
# Матч попадает в запись, если шёл хотя бы частично во время записи (с запасом на подготовку)
_SLACK = timedelta(minutes=10)


def records_dir() -> Path:
    return Path(os.getenv("RECORDINGS_DIR") or "/opt/ti-it-top/RECORDINGS")


def records_url() -> str:
    url = os.getenv("RECORDINGS_URL") or "/rec/"
    return url if url.endswith("/") else url + "/"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _read(rid: str) -> dict[str, Any] | None:
    if not _ID_RE.match(rid or ""):
        return None
    base = records_dir()
    try:
        meta = json.loads((base / f"{rid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not (base / f"{rid}.mp4").is_file():
        return None
    meta["id"] = rid
    return meta


def _write(rid: str, meta: dict[str, Any]) -> None:
    path = records_dir() / f"{rid}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: v for k, v in meta.items() if k != "id"}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def list_records(*, include_hidden: bool = False) -> list[dict[str, Any]]:
    base = records_dir()
    if not base.is_dir():
        return []
    out = []
    for p in base.glob("*.json"):
        meta = _read(p.stem)
        if meta and (include_hidden or not meta.get("hidden")):
            out.append(meta)
    out.sort(key=lambda m: str(m.get("recorded_at") or ""), reverse=True)
    return out


def count_records() -> int:
    return len(list_records())


def _hms(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_chapters(text: str) -> list[dict[str, Any]]:
    """«0:12:30 Матч 3» / «12:30 Финал» построчно → [{t, title}]."""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s+(.+?)\s*$", line)
        if not m:
            if line.strip():
                raise HTTPException(status_code=400, detail=f"Не понял строку «{line.strip()[:60]}». Формат: 0:12:30 Название.")
            continue
        h, mnt, s, title = int(m.group(1) or 0), int(m.group(2)), int(m.group(3)), m.group(4)
        if mnt > 59 or s > 59:
            raise HTTPException(status_code=400, detail=f"Неверное время в строке «{line.strip()[:60]}».")
        out.append({"t": h * 3600 + mnt * 60 + s, "title": title[:120]})
    return sorted(out, key=lambda c: c["t"])[:100]


async def _auto_chapters(session, start: datetime, duration: float) -> tuple[list[dict[str, Any]], int | None]:
    """Матчи, сыгранные во время записи: смещение от начала видео, команды, счёт."""
    from services.tournament_engine.service import _team_names

    end = start + timedelta(seconds=duration)
    lo, hi = (start - _SLACK).replace(tzinfo=None), (end + _SLACK).replace(tzinfo=None)
    rows = (await session.execute(
        select(TournamentMatch).where(or_(
            TournamentMatch.started_at.between(lo, hi),
            TournamentMatch.finished_at.between(lo, hi),
            TournamentMatch.scheduled_at.between(lo, hi),
        ))
    )).scalars().all()
    names = await _team_names(session, {x for m in rows for x in (m.team1_id, m.team2_id) if x})
    chapters, votes = [], {}
    for m in rows:
        st, fin, sch = (timeutil.as_utc(m.started_at), timeutil.as_utc(m.finished_at), timeutil.as_utc(m.scheduled_at))
        # «Идёт» не нажимали — started_at совпадает с finished_at; тогда начало по расписанию
        real = st is not None and (fin is None or (fin - st).total_seconds() > 120)
        at = st if real else sch
        approx = not real
        if at is None or not (start - _SLACK <= at <= end):
            if fin is not None and start <= fin <= end:
                at, approx = fin, True  # хотя бы конец матча есть в записи
            else:
                continue
        t1, t2 = names.get(m.team1_id or 0, "TBD"), names.get(m.team2_id or 0, "TBD")
        score = f" · {m.score1}:{m.score2}" if m.status == "FINISHED" and m.score1 is not None else ""
        chapters.append({
            "t": max(0, int((at - start).total_seconds())),
            "title": f"Матч {m.match_no}: {t1} — {t2}{score}",
            "sub": m.round_name or "",
            "approx": approx,
            "match_id": int(m.id),
        })
        votes[int(m.tournament_id)] = votes.get(int(m.tournament_id), 0) + 1
    chapters.sort(key=lambda c: c["t"])
    best = max(votes, key=votes.get) if votes else None
    return chapters, best


async def serialize(session, meta: dict[str, Any], *, full: bool, admin: bool) -> dict[str, Any]:
    rid = meta["id"]
    start = _parse_time(meta.get("recorded_at"))
    duration = float(meta.get("duration") or 0)
    url = records_url()
    poster = (records_dir() / f"{rid}.jpg").is_file()
    tid = meta.get("tournament_id")
    chapters: list[dict[str, Any]] | None = None
    custom = meta.get("chapters")
    if full or tid is None:
        if isinstance(custom, list) and custom:
            chapters = [{"t": int(c.get("t", 0)), "title": str(c.get("title", "")), "sub": "", "approx": False} for c in custom]
            auto_tid = None
        elif start is not None and duration > 0:
            chapters, auto_tid = await _auto_chapters(session, start, duration)
        else:
            auto_tid = None
        if tid is None:
            tid = auto_tid
    tournament = None
    if tid:
        t = await session.get(Tournament, int(tid))
        if t is not None and (admin or t.status != TournamentStatus.DRAFT):
            tournament = {"id": int(t.id), "name": t.name}
    out = {
        "id": rid,
        "title": meta.get("title") or "Запись трансляции",
        "recorded_at": start.isoformat().replace("+00:00", "Z") if start else None,
        "when": timeutil.fmt_local(start, label=True) if start else None,
        "duration": duration,
        "duration_text": _hms(duration) if duration else None,
        "size": int(meta.get("size") or 0),
        "height": meta.get("height"),
        "video_url": url + rid + ".mp4",
        "poster_url": url + rid + ".jpg" if poster else None,
        "tournament": tournament,
        "chapters_count": len(chapters) if chapters is not None else None,
    }
    if admin:
        out["hidden"] = bool(meta.get("hidden"))
    if full:
        chapters = chapters or []
        for c in chapters:
            c["at"] = _hms(c["t"])
        out["chapters"] = chapters
        if admin:
            out["chapters_custom"] = bool(isinstance(custom, list) and custom)
            out["chapters_text"] = "\n".join(f"{_hms(c['t'])} {c['title']}" for c in chapters)
            out["tournament_fixed"] = meta.get("tournament_id") is not None
    return out


def disk_info() -> dict[str, Any] | None:
    try:
        du = shutil.disk_usage(records_dir())
    except OSError:
        return None
    return {"free": du.free, "total": du.total}


def build_records_router() -> APIRouter:
    router = APIRouter(prefix="/api/v2", route_class=SafeRoute)

    @router.get("/records")
    async def records(tournament_id: int | None = None, viewer: Viewer = Depends(viewer_dep)):
        items = list_records(include_hidden=viewer.is_admin)
        async with session_scope() as session:
            out = [await serialize(session, m, full=False, admin=viewer.is_admin) for m in items]
        if tournament_id:
            out = [r for r in out if r["tournament"] and r["tournament"]["id"] == int(tournament_id)]
        res: dict[str, Any] = {"items": out}
        if viewer.is_admin:
            res["disk"] = disk_info()
        return res

    @router.get("/records/{rid}")
    async def record(rid: str, viewer: Viewer = Depends(viewer_dep)):
        meta = _read(rid)
        if meta is None or (meta.get("hidden") and not viewer.is_admin):
            raise HTTPException(status_code=404, detail="Запись не найдена.")
        async with session_scope() as session:
            return await serialize(session, meta, full=True, admin=viewer.is_admin)

    @router.post("/admin/records/{rid}")
    async def record_edit(rid: str, payload: dict = Body(...), viewer: Viewer = Depends(admin_dep)):
        meta = _read(rid)
        if meta is None:
            raise HTTPException(status_code=404, detail="Запись не найдена.")
        if "title" in payload:
            raw = str(payload.get("title") or "").strip()
            meta["title"] = clean_text(raw, max_len=120, field="Название") if raw else None
        if "hidden" in payload:
            meta["hidden"] = bool(payload.get("hidden"))
        if "tournament_id" in payload:
            raw = payload.get("tournament_id")
            if raw in (None, "", 0, "0"):
                meta["tournament_id"] = None
            else:
                async with session_scope() as session:
                    if await session.get(Tournament, int(raw)) is None:
                        raise HTTPException(status_code=400, detail="Нет такого турнира.")
                meta["tournament_id"] = int(raw)
        if "chapters" in payload:
            text = str(payload.get("chapters") or "")
            meta["chapters"] = parse_chapters(text) if text.strip() else None
        _write(rid, meta)
        logger.info("[RECORDS] %s edited by admin %s", rid, viewer.user_id)
        return {"ok": True}

    @router.delete("/admin/records/{rid}")
    async def record_delete(rid: str, payload: dict = Body(default={}), viewer: Viewer = Depends(admin_dep)):
        if not (payload or {}).get("confirm"):
            raise HTTPException(status_code=400, detail="Нужно подтверждение удаления.")
        if _read(rid) is None:
            raise HTTPException(status_code=404, detail="Запись не найдена.")
        base = records_dir()
        for ext in (".json", ".mp4", ".jpg"):
            try:
                (base / f"{rid}{ext}").unlink()
            except FileNotFoundError:
                pass
        logger.info("[RECORDS] %s deleted by admin %s", rid, viewer.user_id)
        return {"ok": True}

    return router


def build_records_files_router() -> APIRouter:
    """Только видео и обложки (описания .json и недокачанное наружу не отдаются)."""
    router = APIRouter()

    @router.get("/rec/{name}", include_in_schema=False)
    async def record_file(name: str):
        m = _FILE_RE.match(name or "")
        path = records_dir() / name if m else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Файл не найден.")
        media = "video/mp4" if m.group(2) == "mp4" else "image/jpeg"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=86400"})

    return router
