"""SteamProvider: идентификация через Steam Web API (ISteamUser).

STEAM_API_KEY опционален: без ключа провайдер сообщает о недоступности,
и процесс верификации переходит к ручному режиму (PENDING), а не к отказу.
"""
from __future__ import annotations

import logging

import aiohttp

from services.providers_base import PlayerDataProvider, PlayerStats, ProviderUnavailable

logger = logging.getLogger("STEAM")

GET_SCHEMA_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
RESOLVE_VANITY_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
IGC_VERSION_URL = "https://api.steampowered.com/IGCVersion/AllGameDetails/v1/"
DOTA_APP_ID = 570


class SteamProvider(PlayerDataProvider):
    name = "steam"

    def __init__(self, api_key: str = "", timeout: float = 6.0, session: aiohttp.ClientSession | None = None):
        self.api_key = api_key
        self.timeout = timeout
        self._session = session
        self._own_session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None:
            return self._session
        if self._own_session is None or self._own_session.closed:
            self._own_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._own_session

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def resolve_vanity(self, vanity: str) -> str | None:
        """vanity URL -> steamid64. Возвращает None, если разрешить не удалось."""
        if not self.available:
            raise ProviderUnavailable("steam", "STEAM_API_KEY не задан")
        params = {"key": self.api_key, "vanityurl": vanity}
        session = await self._get_session()
        try:
            async with session.get(RESOLVE_VANITY_URL, params=params) as resp:
                if resp.status != 200:
                    logger.warning("[STEAM] ResolveVanityURL HTTP %s", resp.status)
                    return None
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as e:
                    logger.warning("[STEAM] ResolveVanityURL invalid JSON: %s", e)
                    return None
        except (aiohttp.ClientError, TimeoutError) as e:
            logger.warning("[STEAM] ResolveVanityURL network error: %s", e)
            return None
        response = data.get("response", {}) if isinstance(data, dict) else {}
        if response.get("success") == 1 and response.get("steamid"):
            return str(response["steamid"])
        logger.info("[STEAM] Vanity '%s' not resolved (success=%s)", vanity, response.get("success"))
        return None

    async def get_player_by_id64(self, steam_id64: str) -> PlayerStats:
        """Базовая проверка личности по steamid64."""
        if not self.available:
            raise ProviderUnavailable("steam", "STEAM_API_KEY не задан")
        params = {"key": self.api_key, "steamids": steam_id64}
        session = await self._get_session()
        try:
            async with session.get(GET_SCHEMA_URL, params=params) as resp:
                if resp.status in (403, 429) or resp.status >= 500:
                    raise ProviderUnavailable("steam", f"HTTP {resp.status}")
                if resp.status != 200:
                    raise ProviderUnavailable("steam", f"HTTP {resp.status}")
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as e:
                    raise ProviderUnavailable("steam", f"invalid JSON: {e}") from e
        except ProviderUnavailable:
            raise
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ProviderUnavailable("steam", f"network error: {e}") from e

        players = []
        if isinstance(data, dict):
            players = data.get("response", {}).get("players", []) or []
        if not players:
            # Профиль с таким ID не найден — это корректный «неизвестный ID».
            raise LookupError(f"Steam-профиль {steam_id64} не найден")
        p = players[0]
        from utils.validation import id64_to_account_id

        account_id = None
        try:
            account_id = id64_to_account_id(str(p.get("steamid")))
        except (ValueError, TypeError):
            account_id = None
        return PlayerStats(
            nickname=p.get("personaname"),
            steam_id64=str(p.get("steamid")),
            account_id=account_id,
            profile_url=p.get("profileurl"),
            source="steam",
            raw={"communityvisibilitystate": p.get("communityvisibilitystate")},
        )

    async def get_player(self, account_id: int) -> PlayerStats:
        """По interface: account_id -> steamid64 -> профиль."""
        from utils.validation import account_id_to_id64

        sid = str(account_id_to_id64(account_id))
        return await self.get_player_by_id64(sid)

    async def close(self) -> None:
        if self._own_session and not self._own_session.closed:
            await self._own_session.close()


    async def get_dota_rank(self, account_id: int) -> str | None:
        """Официальный medal-ранг игрока (herald_1 ... immortal) из IGCVersion.

        Valve не отдаёт числовой MMR публично; этот эндпоинт — единственный
        официальный источник текущего ранга без сторонних сайтов.
        Возвращает None, если ранг недоступен (приватный профиль и т.п.).
        """
        if not self.available:
            return None
        params = {"key": self.api_key}
        session = await self._get_session()
        try:
            async with session.get(IGC_VERSION_URL, params=params) as resp:
                if resp.status != 200:
                    logger.warning("[STEAM] IGCVersion HTTP %s", resp.status)
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError, aiohttp.ContentTypeError) as e:
            logger.warning("[STEAM] IGCVersion error: %s", e)
            return None
        try:
            game_info = data["result"]["game_info"]
            competitive = game_info.get("competitive") or {}
            entry = competitive.get(str(account_id))
            if not entry:
                return None
            rank_name = entry.get("rank_name")
            rank_grade = _to_int(entry.get("rank_grade"))
            if not rank_name:
                return None
            return f"{rank_name}_{rank_grade}" if rank_grade is not None else rank_name
        except (KeyError, TypeError, AttributeError):
            return None


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
