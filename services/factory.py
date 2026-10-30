"""Фабрика сервисов: единая точка создания провайдеров/сервисов.

Используется и в bot.py (runtime), и в тестах (с подменой провайдеров).
"""
from __future__ import annotations

from config import Config, GLOBAL_CONFIG
from services.cache import ProfileCache
from services.dota_steamclient import DotaSteamClientProvider
from services.dotabuff import DotabuffProvider
from services.http_client import close_shared_client, get_http_client
from services.player_verification import PlayerVerificationService
from services.sheets import SheetsService
from services.steam import SteamProvider
from services.stratz import StratzProvider


class Services:
    """Лёгкий контейнер зависимостей."""

    def __init__(self, config: Config | None = None):
        self.config = config or GLOBAL_CONFIG
        # Единый HTTP-транспорт (curl_cffi -> aiohttp -> urllib + proxy detect).
        http = get_http_client(
            proxy=self.config.http_proxy_url or None,
            impersonate=getattr(self.config, "http_impersonate", "chrome126"),
        )
        self.http = http
        self.steam = SteamProvider(api_key=self.config.steam_api_key, timeout=6.0)
        self.dotabuff = DotabuffProvider(timeout=http.timeout, http=http)
        self.stratz = StratzProvider(
            api_token=self.config.stratz_api_token,
            url=self.config.stratz_api_url,
            timeout=http.timeout,
            http=http,
        )
        self.cache = ProfileCache(
            path=self.config.profile_cache_path, ttl=self.config.profile_cache_ttl
        )
        self.dota_client = DotaSteamClientProvider(
            api_key=self.config.steam_api_key, timeout=6.0, http=http
        )
        self.verification = PlayerVerificationService(
            steam=self.steam, dotabuff=self.dotabuff, stratz=self.stratz,
            cache=self.cache, dota_client=self.dota_client,
        )
        self.sheets = SheetsService(webhook_url=self.config.google_sheets_id)

    async def close(self) -> None:
        await self.verification.close()


_default: Services | None = None


def get_services() -> Services:
    global _default
    if _default is None:
        _default = Services()
    return _default


def set_services(services: Services) -> None:
    """Инъекция для тестов."""
    global _default
    _default = services
