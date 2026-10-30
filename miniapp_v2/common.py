"""Общие части API v2: конфиг, авторизация, безопасные маршруты, сериализация.

Правила сериализации:
* значение, которого нет в источнике, возвращается как ``null`` (никаких 0/1000/…);
* MMR разделён на «подтверждённый организатором» и «заявленный игроком»;
* медаль — только фактическая (rank_tier из OpenDota/Valve), без расчёта из MMR;
* аватар команды — только локальный файл; Telegram API здесь не вызывается.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select

from config import GLOBAL_CONFIG, Config, is_admin
from database.engine import session_scope
from database.models import User
from database.repositories import UserRepository
from miniapp.auth import TelegramUser, validate_init_data
from services.teams import POSITIONS
from utils.ranks import rank_info_from_tier, rank_label_from_tier

logger = logging.getLogger("MINIAPP_V2")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
STATIC_DIR = BASE_DIR / "static"
TEAM_AVATAR_DIR = PROJECT_DIR / "miniapp" / "data" / "team_avatars"
RUNTIME_DIR = PROJECT_DIR / "runtime"
UI_FLAG_FILE = RUNTIME_DIR / "MINIAPP_UI"

POSITION_NAMES = {1: "Керри", 2: "Мид", 3: "Хард", 4: "Четвёрка", 5: "Пятёрка"}

_config: Config | None = None


def configure(config: Config) -> None:
    global _config
    _config = config


def get_config() -> Config:
    return _config or GLOBAL_CONFIG


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Какой интерфейс отдаётся по «/»: v2 (по умолчанию) или legacy.
# Переключение без рестарта: runtime/MINIAPP_UI содержит «v2» или «legacy».
# ---------------------------------------------------------------------------

def active_ui() -> str:
    try:
        value = UI_FLAG_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "v2"
    return "legacy" if value == "legacy" else "v2"


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Viewer:
    telegram_id: int
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    registered: bool
    is_admin: bool


def _dev_user(init_data: str) -> TelegramUser | None:
    """Локальная отладка без Telegram. В production отключено.

    Требует MINIAPP_ALLOW_DEV_AUTH=1 в окружении процесса. «dev:<telegram_id>»
    выбирает пользователя; «dev:» — MINIAPP_DEV_USER_ID.
    """
    if not _truthy(os.getenv("MINIAPP_ALLOW_DEV_AUTH")) or not init_data.startswith("dev:"):
        return None
    # username/first_name = None: dev-вход не перезаписывает реальные имена в БД.
    rest = init_data[4:].strip()
    if rest.isdigit():
        return TelegramUser(id=int(rest))
    dev_id = os.getenv("MINIAPP_DEV_USER_ID", "").strip()
    if dev_id.isdigit():
        return TelegramUser(id=int(dev_id))
    return None


def parse_telegram_user(init_data: str) -> TelegramUser:
    try:
        return validate_init_data(init_data, get_config().bot_token)
    except ValueError as exc:
        dev = _dev_user(init_data)
        if dev is not None:
            return dev
        message = str(exc).lower()
        if "expired" in message:
            detail = "Сессия Telegram устарела. Закройте Mini App и откройте её снова."
        elif "missing" in message:
            detail = "Откройте Mini App из Telegram — данные авторизации не переданы."
        else:
            detail = "Не удалось проверить данные Telegram. Откройте Mini App заново."
        raise HTTPException(status_code=401, detail=detail) from None


async def resolve_viewer(tg: TelegramUser, *, refresh: bool = False) -> Viewer:
    """Находит пользователя БД. Запись (upsert) — только для новых или при refresh."""
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.telegram_id == tg.id))
        if user is None or refresh:
            user = await UserRepository(session).upsert_from_telegram(
                tg.id, tg.username, tg.first_name, tg.last_name
            )
            await session.commit()
        admin = is_admin(tg.id, get_config())
        if not admin:
            from services.bans import BLOCKED_TEXT, is_banned

            if await is_banned(tg.id):
                raise HTTPException(status_code=403, detail=BLOCKED_TEXT)
        return Viewer(
            telegram_id=int(tg.id),
            user_id=int(user.id),
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            registered=bool(user.is_registered),
            is_admin=is_admin(tg.id, get_config()),
        )


async def viewer_dep(x_telegram_init_data: str | None = Header(default=None)) -> Viewer:
    return await resolve_viewer(parse_telegram_user(x_telegram_init_data or ""))


async def admin_dep(viewer: Viewer = Depends(viewer_dep)) -> Viewer:
    if not viewer.is_admin:
        raise HTTPException(status_code=403, detail="Раздел доступен только администраторам.")
    return viewer


# ---------------------------------------------------------------------------
# Маршруты: неожиданная ошибка -> JSON 500 с кодом, медленные запросы -> лог.
# ---------------------------------------------------------------------------

class SafeRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()
        path = self.path

        async def handler(request: Request):
            started = time.perf_counter()
            try:
                response = await original(request)
            except (StarletteHTTPException, RequestValidationError):
                raise
            except Exception:  # noqa: BLE001 — одна ошибка не должна ломать клиент
                error_id = uuid.uuid4().hex[:10]
                logger.exception("[MINIAPP_V2] %s %s failed, error_id=%s", request.method, path, error_id)
                return JSONResponse(
                    status_code=500,
                    content={
                        "detail": f"Внутренняя ошибка сервера (код {error_id}). Попробуйте ещё раз.",
                        "error_id": error_id,
                    },
                )
            elapsed = time.perf_counter() - started
            if elapsed > 1.5:
                logger.warning("[MINIAPP_V2] slow %s %s: %.2fs", request.method, path, elapsed)
            return response

        return handler


# ---------------------------------------------------------------------------
# Небольшой TTL-кэш только для публичных данных.
# ---------------------------------------------------------------------------

class TTLCache:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> Any:
        if len(self._items) > 500:
            self._items.clear()
        self._items[key] = (time.monotonic() + ttl, value)
        return value

    def invalidate(self, prefix: str = "") -> None:
        if not prefix:
            self._items.clear()
            return
        for key in [k for k in self._items if k.startswith(prefix)]:
            self._items.pop(key, None)


public_cache = TTLCache()


def invalidate_public(*prefixes: str) -> None:
    for prefix in prefixes or ("",):
        public_cache.invalidate(prefix)


# ---------------------------------------------------------------------------
# Сериализация
# ---------------------------------------------------------------------------

def iso(value: datetime | None) -> str | None:
    """ISO-время в UTC с явным «Z». SQLite хранит время без зоны (по проекту — UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def display_name(user: Any) -> str:
    if user is None:
        return "Игрок"
    full = " ".join(x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x)
    if full:
        return full
    if getattr(user, "username", None):
        return f"@{user.username}"
    ident = getattr(user, "id", None) or getattr(user, "user_id", None) or "?"
    return f"Игрок #{ident}"


def parse_positions(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit() and int(part) in POSITIONS and int(part) not in out:
            out.append(int(part))
    return sorted(out)


def confirmed_mmr(profile: Any) -> int | None:
    """MMR, подтверждённый организатором. Нет подтверждения -> None."""
    if profile is None:
        return None
    approved = getattr(profile, "approved_mmr", None)
    if approved is not None:
        return int(approved)
    # Старые записи до появления approved_mmr: миграция 4.0 переносит так же.
    if bool(getattr(profile, "mmr_verified", False)) and profile.exact_mmr is not None:
        return int(profile.exact_mmr)
    return None


def mmr_facts(profile: Any) -> dict[str, Any]:
    """Факты об MMR. state: none | pending | confirmed | rejected."""
    if profile is None:
        return {"confirmed": None, "declared": None, "state": "none"}
    confirmed = confirmed_mmr(profile)
    declared = int(profile.exact_mmr) if profile.exact_mmr is not None else None
    status = profile.verification_status.value if profile.verification_status else None
    if confirmed is not None:
        state = "confirmed"
    elif declared is not None and status == "REJECTED":
        state = "rejected"
    elif declared is not None:
        state = "pending"
    else:
        state = "none"
    return {"confirmed": confirmed, "declared": declared, "state": state}


def medal_facts(profile: Any) -> dict[str, Any] | None:
    """Фактическая медаль (rank_tier OpenDota/Valve). Из MMR медаль не вычисляется."""
    if profile is None or profile.medal_rank is None:
        return None
    info = rank_info_from_tier(profile.medal_rank)
    if info is None:
        return None
    return {
        "tier": info.rank_tier,
        "name": rank_label_from_tier(info.rank_tier),
        "leaderboard_rank": profile.leaderboard_rank if info.medal == 8 else None,
    }


def stats_facts(profile: Any) -> dict[str, Any]:
    """Статистика из внешних источников как есть.

    OpenDota для скрытого профиля отдаёт win=0/lose=0 -> matches=0 без winrate.
    Это «нет публичных данных», а не «0 матчей», поэтому добавлен флаг public.
    """
    if profile is None:
        return {"matches": None, "winrate": None, "hours": None, "public": False}
    matches = profile.matches
    winrate = profile.winrate
    hours = profile.hours_total
    public = not (matches in (None, 0) and winrate is None and hours is None)
    return {"matches": matches, "winrate": winrate, "hours": hours, "public": public}


def verification_state(profile: Any) -> str:
    if profile is None or not profile.steam_id64:
        return "unlinked"
    return profile.verification_status.value if profile.verification_status else "UNVERIFIED"


def avatar_url(team_id: int) -> str | None:
    """URL локального аватара с версией по mtime (для кэша браузера). Нет файла -> None."""
    path = TEAM_AVATAR_DIR / f"team_{int(team_id)}.jpg"
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"/api/v2/teams/{int(team_id)}/avatar?v={int(stat.st_mtime)}"


PLAYER_AVATAR_DIR = PROJECT_DIR / "miniapp" / "data" / "player_avatars"


def player_avatar_url(user_id: int | None) -> str | None:
    """Локальная копия аватара игрока (Steam/Telegram). Нет файла -> None."""
    if not user_id:
        return None
    try:
        stat = (PLAYER_AVATAR_DIR / f"user_{int(user_id)}.jpg").stat()
    except OSError:
        return None
    return f"/api/v2/players/{int(user_id)}/avatar?v={int(stat.st_mtime)}"


def player_brief(user: Any, profile: Any) -> dict[str, Any]:
    return {
        "user_id": int(user.id),
        "name": display_name(user),
        "username": user.username,
        "avatar_url": player_avatar_url(user.id),
        "mmr": mmr_facts(profile),
        "medal": medal_facts(profile),
        "positions": parse_positions(profile.preferred_positions if profile else None),
    }


def page_params(page: int, limit: int, *, max_limit: int = 50) -> tuple[int, int, int]:
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 20), max_limit))
    return page, limit, (page - 1) * limit


def page_payload(items: list, total: int, page: int, limit: int) -> dict[str, Any]:
    return {
        "items": items,
        "total": int(total),
        "page": page,
        "limit": limit,
        "has_more": page * limit < int(total),
    }


def clean_text(value: str | None, *, max_len: int, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"Поле «{field}» не может быть пустым.")
    if len(text) > max_len:
        raise HTTPException(status_code=400, detail=f"Поле «{field}»: не больше {max_len} символов.")
    return text


def html_escape(value: Any) -> str:
    from utils.formatting import escape

    return escape(str(value)) if value is not None else ""
