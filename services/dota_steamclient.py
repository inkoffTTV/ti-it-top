"""DotaSteamClientProvider — официальные данные Valve через Steam Web API.

Единственный источник статистики по Dota 2, который реально работает без
Cloudflare-вызовов сторонних сайтов:

  * ISteamUser/GetPlayerSummaries      -> ник, steamid64, accountid, ссылка;
  * IPlayerService/GetOwnedGames       -> часы в Dota 2 (2 недели / всего).

Никаких выдуманных значений: если поле не получено — оно остаётся None.
Числовой MMR Valve публично не отдаёт, поэтому mmr остаётся None и в UI
честно показывается «Недоступно» (правило ТЗ: никаких фейков).
"""
from __future__ import annotations

import logging

import aiohttp

from services.providers_base import PlayerDataProvider, PlayerStats, ProviderUnavailable
from services.steam import SteamProvider

logger = logging.getLogger("STEAM")

DOTA_APP_ID = 570
SUMMARIES_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"

def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class DotaSteamClientProvider(PlayerDataProvider):
    name = "steam_dota"

    def __init__(self, api_key: str = "", timeout: float = 6.0,
                 session: aiohttp.ClientSession | None = None, http=None):
        self.api_key = api_key
        self._steam = SteamProvider(api_key=api_key, timeout=timeout, session=session)
        self.timeout = timeout
        self._session = session
        self._own_session: aiohttp.ClientSession | None = None
        self.http = http

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None:
            return self._session
        if self._own_session is None or self._own_session.closed:
            self._own_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._own_session

    async def _get_json(self, url: str, params: dict, provider_name: str) -> dict:
        if self.http is not None:
            try:
                from urllib.parse import urlencode
                full_url = url + ("&" if "?" in url else "?") + urlencode(params)
                r = await self.http.get(full_url, timeout=self.timeout)
                if r.status != 200:
                    raise ProviderUnavailable(provider_name, f"HTTP {r.status}")
                import json
                data = json.loads(r.body)
            except ProviderUnavailable:
                raise
            except (ValueError, TypeError) as e:
                raise ProviderUnavailable(provider_name, f"invalid JSON: {e}") from e
            except Exception as e:
                raise ProviderUnavailable(provider_name, f"network error: {e}") from e
            if not isinstance(data, dict):
                raise ProviderUnavailable(provider_name, "unexpected payload shape")
            return data

        session = await self._get_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise ProviderUnavailable(provider_name, f"HTTP {resp.status}")
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as e:
                    raise ProviderUnavailable(provider_name, f"invalid JSON: {e}") from e
        except ProviderUnavailable:
            raise
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ProviderUnavailable(provider_name, f"network error: {e}") from e
        if not isinstance(data, dict):
            raise ProviderUnavailable(provider_name, "unexpected payload shape")
        return data

    async def get_player_by_id64(self, steam_id64: str) -> PlayerStats:
        """Официальные данные Steam + Dota 2 playtime по SteamID64."""
        if not self.available:
            raise ProviderUnavailable("steam_dota", "STEAM_API_KEY не задан")

        stats = PlayerStats(source="steam_dota")

        # 1. Профиль: ник, accountid, видимость.
        data = await self._get_json(
            SUMMARIES_URL, {"key": self.api_key, "steamids": steam_id64}, "steam_dota"
        )
        players = (data.get("response") or {}).get("players") or []
        if not players:
            raise LookupError(f"Steam-профиль {steam_id64} не найден")
        p = players[0]
        stats.nickname = p.get("personaname") or None
        stats.steam_id64 = str(p.get("steamid") or steam_id64)
        stats.profile_url = p.get("profileurl") or None
        account_id = _to_int(p.get("accountid"))
        if account_id is None:
            from utils.validation import id64_to_account_id
            try:
                account_id = id64_to_account_id(stats.steam_id64)
            except (ValueError, TypeError):
                account_id = None
        stats.account_id = account_id
        private_profile = p.get("communityvisibilitystate") != 3
        if private_profile:
            stats.raw["private_profile"] = True

        # 2. Часы в Dota 2 (только для публичного профиля).
        if not private_profile:
            try:
                pdata = await self._get_json(
                    OWNED_GAMES_URL,
                    {"key": self.api_key, "steamid": stats.steam_id64,
                     "appid_filter[]": DOTA_APP_ID,
                     "include_played_free_games": 1},
                    "steam_dota",
                )
                for g in ((pdata.get("response") or {}).get("games")) or []:
                    if _to_int(g.get("appid")) == DOTA_APP_ID:
                        total = _to_int(g.get("playtime_forever"))
                        two_weeks = _to_int(g.get("playtime_2weeks"))
                        if total is not None:
                            stats.raw["dota_hours_total"] = total
                        if two_weeks is not None:
                            stats.raw["dota_hours_2weeks"] = two_weeks
                        break
            except ProviderUnavailable as e:
                logger.info("[STEAM] playtime unavailable: %s", e)

        # 3. Официальный medal-ранг (единственный «рейтинг», который Valve отдаёт публично).
        if account_id is not None:
            try:
                rank = await self._steam.get_dota_rank(account_id)
                if rank:
                    stats.raw["dota_rank"] = rank
            except Exception as e:  # noqa: BLE001 — ранг опционален
                logger.info("[STEAM] medal rank unavailable: %s", e)

        logger.info(
            "[STEAM] Dota client data sid=%s nick=%s hours_total=%s private=%s",
            steam_id64, stats.nickname,
            stats.raw.get("dota_hours_total"), stats.raw.get("private_profile", False),
        )
        return stats

    async def get_player(self, account_id: int) -> PlayerStats:
        from utils.validation import account_id_to_id64
        return await self.get_player_by_id64(str(account_id_to_id64(account_id)))

    async def close(self) -> None:
        if self._own_session and not self._own_session.closed:
            await self._own_session.close()
