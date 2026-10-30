"""Конфигурация TI:IT TOP bot. Все секреты — только из окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

BASE_DIR = Path(__file__).resolve().parent


def _normalize_database_url(raw: str) -> str:
    """Привязывает относительный SQLite-файл к папке проекта.

    Это предотвращает появление новых tournament.db при каждом запуске
    из разных рабочих каталогов (VS Code, PowerShell, ярлык и т.д.).
    Внешние PostgreSQL/другие URL не изменяются.
    """
    raw = (raw or "").strip()
    if not raw.startswith("sqlite+aiosqlite://"):
        return raw
    try:
        url = make_url(raw)
        database = url.database
        if not database or database == ":memory:":
            return raw
        db_path = Path(database)
        if not db_path.is_absolute():
            db_path = (BASE_DIR / db_path).resolve()
            return url.set(database=str(db_path)).render_as_string(hide_password=False)
    except Exception:
        # Не ломаем запуск из-за необычного URL; SQLAlchemy сообщит об ошибке позже.
        return raw
    return raw


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            # Игнорируем некорректные записи, но не падаем.
            continue
    return ids


@dataclass(slots=True)
class Config:
    bot_token: str = ""
    admin_ids: set[int] = field(default_factory=set)
    database_url: str = "sqlite+aiosqlite:///./tournament.db"

    steam_api_key: str = ""
    stratz_api_token: str = ""
    stratz_api_url: str = "https://api.stratz.com/graphql"

    profile_cache_ttl: int = 1800
    profile_cache_path: str = "profile_cache.json"

    google_sheets_credentials: str = ""
    google_sheets_id: str = ""

    http_timeout: float = 8.0
    http_proxy_url: str = ""
    http_impersonate: str = "chrome126"

    miniapp_url: str = ""
    miniapp_host: str = "127.0.0.1"
    miniapp_port: int = 8080
    mmr_rank_range_hard_block: bool = False
    ai_api_key: str = ""
    ai_base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    ai_model: str = "openrouter/free"
    ai_fallback_model: str = "qwen/qwen3.8-27b:free"
    ai_backup_api_key: str = ""
    ai_backup_base_url: str = "https://api.groq.com/openai/v1/chat/completions"
    ai_backup_model: str = "openai/gpt-oss-120b"
    ai_max_tool_rounds: int = 5

    @property
    def is_valid(self) -> bool:
        return bool(self.bot_token)


def load_config(dotenv_path: str | Path | None = None) -> Config:
    """Загружает конфигурацию из .env и переменных окружения."""
    load_dotenv(dotenv_path or BASE_DIR / ".env", override=False)

    try:
        ttl = int(os.getenv("PROFILE_CACHE_TTL", "1800"))
    except ValueError:
        ttl = 1800

    return Config(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
        database_url=_normalize_database_url(
            os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./tournament.db")
        ),
        steam_api_key=os.getenv("STEAM_API_KEY", "").strip(),
        stratz_api_token=os.getenv("STRATZ_API_TOKEN", "").strip(),
        stratz_api_url=os.getenv(
            "STRATZ_API_URL", "https://api.stratz.com/graphql"
        ).strip(),
        profile_cache_ttl=ttl,
        profile_cache_path=os.getenv("PROFILE_CACHE_PATH", "profile_cache.json").strip(),
        google_sheets_credentials=os.getenv("GOOGLE_SHEETS_CREDENTIALS", "").strip(),
        google_sheets_id=os.getenv("GOOGLE_SHEETS_ID", "").strip(),
        http_proxy_url=os.getenv("HTTP_PROXY_URL", "").strip(),
        http_impersonate=os.getenv("HTTP_IMPERSONATE", "chrome126").strip() or "chrome126",
        miniapp_url=os.getenv("MINIAPP_URL", "").strip().rstrip("/"),
        miniapp_host=os.getenv("MINIAPP_HOST", "127.0.0.1").strip() or "127.0.0.1",
        miniapp_port=int(os.getenv("MINIAPP_PORT", "8080") or 8080),
        mmr_rank_range_hard_block=os.getenv("MMR_RANK_RANGE_HARD_BLOCK", "false").strip().lower() in {"1", "true", "yes", "on"},
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_base_url=os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1/chat/completions").strip(),
        ai_model=os.getenv("AI_MODEL", "openrouter/free").strip() or "openrouter/free",
        ai_fallback_model=os.getenv("AI_FALLBACK_MODEL", "qwen/qwen3.8-27b:free").strip() or "qwen/qwen3.8-27b:free",
        ai_backup_api_key=os.getenv("AI_BACKUP_API_KEY", "").strip(),
        ai_backup_base_url=os.getenv("AI_BACKUP_BASE_URL", "https://api.groq.com/openai/v1/chat/completions").strip(),
        ai_backup_model=os.getenv("AI_BACKUP_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b",
        ai_max_tool_rounds=max(1, min(int(os.getenv("AI_MAX_TOOL_ROUNDS", "5") or 5), 8)),
    )


def is_admin(user_id: int | None, config: Config | None = None) -> bool:
    """Проверка прав администратора по telegram_id."""
    if user_id is None:
        return False
    cfg = config or GLOBAL_CONFIG
    return int(user_id) in cfg.admin_ids


# Глобальный конфиг; перечитывается в bot.py при старте.
GLOBAL_CONFIG = load_config()
