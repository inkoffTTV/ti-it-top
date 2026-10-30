"""StratzProvider — ОПЦИОНАЛЬНЫЙ GraphQL-источник статистики.

STRATZ_API_TOKEN опционален. При 403/Cloudflare/timeout/5xx/битом JSON
бросается ProviderUnavailable — игрок НЕ отклоняется, процесс идёт дальше.

HTTP идёт через services.http_client.HttpClient: curl_cffi (TLS-fingerprint
Chrome) -> aiohttp -> urllib; системный прокси/VPN подхватывается автоматически.
Дополнительно есть self-check провайдеров (diagnose) для админ-панели.
"""
from __future__ import annotations

import json
import logging

from services.http_client import HttpClient, get_http_client
from services.providers_base import PlayerDataProvider, PlayerStats, ProviderUnavailable

logger = logging.getLogger("STRATZ")

DEFAULT_URL = "https://api.stratz.com/graphql"

_QUERY = """
query PlayerRanking($steamAccountId: Long!) {
  player(steamAccountId: $steamAccountId) {
    steamAccount {
      id
      name
      seasonRank
      seasonLeaderboardRank
      smurfFlag
      isAnonymous
    }
    matchCount
    winCount
  }
}
"""

# Актуальная схема STRATZ (проверено с сервера 29.09.2026): seasonRank и
# seasonLeaderboardRank лежат в SteamAccountType, matchCount/winCount — в PlayerType.
# Старый запрос с player.seasonRank падал с «Cannot query field 'seasonRank'».
# MMR не выводим из seasonRank — это tier, а не точный MMR.
_QUERY_FULL = _QUERY



def _count_matches(match_counts) -> int | None:
    """matchCounts — список {name, counts:[{resultType,count}]}. Суммируем все."""
    if not isinstance(match_counts, list):
        return None
    total = 0
    found = False
    for entry in match_counts:
        if not isinstance(entry, dict):
            continue
        for c in entry.get("counts") or []:
            if isinstance(c, dict) and isinstance(c.get("count"), (int, float)):
                total += int(c["count"])
                found = True
    return total if found and total > 0 else None


def _count_wins(match_counts) -> int | None:
    if not isinstance(match_counts, list):
        return None
    wins = 0
    losses = 0
    for entry in match_counts:
        if not isinstance(entry, dict):
            continue
        for c in entry.get("counts") or []:
            if not isinstance(c, dict):
                continue
            rt = str(c.get("resultType") or "").upper()
            cnt = c.get("count")
            if not isinstance(cnt, (int, float)):
                continue
            if "WIN" in rt:
                wins += int(cnt)
            elif "LOSS" in rt or "LOSE" in rt:
                losses += int(cnt)
    if wins + losses == 0:
        return None
    return wins


def _already(body: str):
    """Coroutine returning an already-read body (for legacy test sessions)."""
    async def _coro() -> str:
        return body
    return _coro()


class StratzProvider(PlayerDataProvider):
    name = "stratz"

    def __init__(
        self,
        api_token: str = "",
        url: str = DEFAULT_URL,
        timeout: float = 8.0,
        session=None,  # back-compat for tests injecting aiohttp-like session
        http: HttpClient | None = None,
    ):
        self.api_token = api_token
        self.url = url or DEFAULT_URL
        self.timeout = timeout
        self._session = session  # legacy injected transport (tests)
        self.http = http or get_http_client()

    @property
    def available(self) -> bool:
        return bool(self.api_token)

    async def _post(self, payload: dict, headers: dict[str, str]):
        """Возвращает объект с .status и текстом; совместим и с aiohttp-сессией."""
        if self._session is not None:
            class _R:
                def __init__(self, resp):
                    self._resp = resp
                    self.status = resp.status

                async def read_text(self):
                    return await self._resp.text(errors="replace")
            ctx = self._session.post(self.url, json=payload, headers=headers)
            resp = await ctx.__aenter__()
            try:
                body = await resp.text(errors="replace")
            finally:
                await ctx.__aexit__(None, None, None)
            r = _R(resp)
            r.read_text = lambda b=body: _already(b)  # type: ignore[method-assign]
            return r

        resp = await self.http.post_json(
            self.url, payload, headers=headers, timeout=self.timeout
        )

        class _Simple:
            def __init__(self, hr):
                self.status = hr.status
                self._body = hr.body

            async def read_text(self):
                return self._body
        return _Simple(resp)

    async def get_player(self, account_id: int) -> PlayerStats:
        if not self.available:
            raise ProviderUnavailable("stratz", "STRATZ_API_TOKEN не задан")
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {"query": _QUERY_FULL, "variables": {"steamAccountId": account_id}}
        try:
            resp = await self._post(payload, headers)
        except ConnectionError as e:
            raise ProviderUnavailable("stratz", f"network error: {e}") from e
        except ProviderUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — сеть/таймаут => недоступность, не отказ
            logger.warning("[STRATZ] request error: %s", e)
            raise ProviderUnavailable("stratz", f"network error: {e}") from e

        if resp.status == 401:
            raise ProviderUnavailable(
                "stratz", "HTTP 401 — недействительный/истёкший STRATZ_API_TOKEN"
            )
        if resp.status == 403:
            body_probe = ""
            try:
                body_probe = (await resp.read_text())[:600].lower()
            except Exception:  # noqa: BLE001
                pass
            if "cloudflare" in body_probe or "just a moment" in body_probe:
                raise ProviderUnavailable(
                    "stratz",
                    "HTTP 403 — Cloudflare-челлендж по TLS/IP (попробуй включить VPN/"
                    "прокси и '🔎 Диагностика API' в админке)",
                )
            raise ProviderUnavailable(
                "stratz",
                "HTTP 403 — доступ запрещён (проверь STRATZ_API_TOKEN в .env)",
            )
        if resp.status != 200:
            raise ProviderUnavailable("stratz", f"HTTP {resp.status}")
        text = await resp.read_text()

        lowered = text.lower()
        if "cloudflare" in lowered and ("challenge" in lowered or "just a moment" in lowered):
            raise ProviderUnavailable("stratz", "Cloudflare challenge page")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderUnavailable("stratz", f"invalid JSON: {e}") from e

        if not isinstance(data, dict):
            raise ProviderUnavailable("stratz", "unexpected payload shape")
        if data.get("errors"):
            # GraphQL-ошибки бывают и при валидном токене — это недоступность данных, не отказ.
            raise ProviderUnavailable("stratz", f"graphql errors: {data['errors'][:1]}")

        player = (data.get("data") or {}).get("player")
        if not player:
            raise LookupError(f"Игрок {account_id} не найден на STRATZ")

        stats = PlayerStats(source="stratz")
        steam_account = player.get("steamAccount") or {}
        stats.nickname = steam_account.get("name") or player.get("name")
        if steam_account.get("id") is not None:
            # steamAccount.id — Steam account ID (32-bit), не SteamID64.
            # Не подменяем им steam_id64.
            stats.raw["steam_account_id"] = steam_account.get("id")
        stats.account_id = account_id

        def _field(name: str):
            # Новая схема — в steamAccount, старая — прямо в player.
            value = steam_account.get(name)
            return value if value is not None else player.get(name)

        if _field("seasonLeaderboardRank") is not None:
            try:
                stats.raw["leaderboard_rank"] = int(_field("seasonLeaderboardRank"))
            except (ValueError, TypeError):
                pass
        if _field("seasonRank") is not None:
            try:
                stats.raw["rank_tier"] = int(_field("seasonRank"))
            except (ValueError, TypeError):
                pass
        if steam_account.get("smurfFlag") is not None:
            stats.raw["smurf_flag"] = steam_account.get("smurfFlag")
        if steam_account.get("isAnonymous") is not None:
            stats.raw["is_anonymous"] = bool(steam_account.get("isAnonymous"))
        try:
            if player.get("matchCount") is not None:
                stats.matches = int(player["matchCount"])
                if stats.matches and player.get("winCount") is not None:
                    stats.winrate = round(int(player["winCount"]) / stats.matches * 100.0, 1)
        except (ValueError, TypeError, ZeroDivisionError):
            pass

        # matchCounts может отсутствовать (схема менялась) — парсим безопасно.
        mc = player.get("matchCounts")
        if mc is None:
            mc = player.get("match_counts")
        if mc is not None:
            total = _count_matches(mc)
            wins = _count_wins(mc)
            if total is not None:
                stats.matches = total
            if total and wins is not None:
                stats.winrate = round(wins / total * 100.0, 1)

        logger.info(
            "[STRATZ] Parsed account_id=%s mmr=%s matches=%s winrate=%s nick=%s",
            account_id, stats.mmr, stats.matches, stats.winrate, stats.nickname,
        )
        return stats

    async def close(self) -> None:
        # Общая http-сессия (curl_cffi) закрывается на уровне Services.close().
        return None
