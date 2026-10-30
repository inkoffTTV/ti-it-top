"""Dotabuff + optional OpenDota player statistics.

The Dotabuff implementation keeps the simple transport/parser used by the
17:58 build.  The current verification architecture is preserved through
DotabuffProvider/PlayerStats.

Order is strict:
    1. Dotabuff
    2. OpenDota only if Dotabuff is unavailable

OpenDota is optional. A timeout, 522, bad JSON, or any other network failure
never propagates into the Telegram handler.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

from services.providers_base import PlayerDataProvider, PlayerStats, ProviderUnavailable
from utils.ranks import rank_label_from_tier, rank_name_to_tier

logger = logging.getLogger("services.dotabuff")

DOTABUFF_PROFILE = "https://www.dotabuff.com/players/{account_id}"
OPENDOTA_PROFILE = "https://api.opendota.com/api/players/{account_id}"
OPENDOTA_WL = "https://api.opendota.com/api/players/{account_id}/wl"
OPENDOTA_COUNTS = "https://api.opendota.com/api/players/{account_id}/counts"

DOTABUFF_RE = re.compile(r"(?:https?://)?(?:www\.)?dotabuff\.com/players/(\d+)", re.I)
NUMBER_RE = re.compile(r"^\d{3,20}$")

_NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*type="application/json"[^>]*>(.*?)</script>', re.S | re.I)
_RATING_JSON_RE = re.compile(r'"(?:leaderboard_?rating|solo_?competitive_?rank|competitive_?rank|mmr|rating)"\s*:\s*(\d{2,6})', re.I)
_MATCHES_JSON_RE = re.compile(r'"(?:matchesPlayed|totalMatches|matchCount|matches)"\s*:\s*(\d{1,7})', re.I)
_WINRATE_JSON_RE = re.compile(r'"(?:winRate|winrate|allWinsRatio)"\s*:\s*(\d{1,3}(?:\.\d+)?)', re.I)
_RANK_NAME_JSON_RE = re.compile(r'"(?:rankTierName|rank_name|rankName|rank)"\s*:\s*"([^"\n]{2,64})"', re.I)
_RANK_TEXT_RE = re.compile(r'(?:Rank|Tier|Ранг)\s*[:\-]?\s*([A-Za-zА-Яа-яЁё]+(?:\s*[-_]\s*\d+)?(?:\s*\d+)?)', re.I)
_MMR_VALUE_RE = re.compile(r'<span[^>]*class="[^"]*rankings-rating__value[^\"]*"[^>]*>([^<]+)</span>', re.I)
_STATS_TEXT_RE = re.compile(r'(\d+)\s+Matches', re.I)
_WINRATE_TEXT_RE = re.compile(r'Win\s*Rate[:\s]*([\d.]+)\s*%', re.I)

# Значок медали на странице игрока (проверено 29.09.2026):
#   <div class="rank-tier-wrapper" rel="tooltip" title="Rank: Archon I">
_RANK_BADGE_RE = re.compile(r'rank-tier-wrapper[^>]*title="Rank:\s*([A-Za-z]+)\s*([IVX]{1,4}|\d)?"', re.I)
_MEDALS_EN = {"herald": 1, "guardian": 2, "crusader": 3, "archon": 4, "legend": 5, "ancient": 6, "divine": 7, "immortal": 8}
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}


def dotabuff_rank_tier(html: str) -> int | None:
    """rank_tier (медаль*10 + звезда) из значка ранга Dotabuff; нет значка — None."""
    m = _RANK_BADGE_RE.search(html or "")
    if not m:
        return None
    medal = _MEDALS_EN.get(m.group(1).lower())
    if medal is None:
        return None
    if medal == 8:
        return 80
    star_raw = (m.group(2) or "").upper()
    star = int(star_raw) if star_raw.isdigit() else _ROMAN.get(star_raw)
    if not star or not 1 <= star <= 5:
        return None
    return medal * 10 + star


def _looks_like_player_page(html: str) -> bool:
    low = html.lower()
    return "player-profile" in low or "__next_data__" in low or "pagedata" in low or "rankings-rating" in low or 'href="/players/' in low

def _parse_html(html: str) -> PlayerStats:
    stats = PlayerStats(source="dotabuff")
    m = _RATING_JSON_RE.search(html)
    if m:
        try:
            val = int(m.group(1))
            if 1 <= val <= 30000:
                stats.mmr = val
        except ValueError:
            pass
    if stats.mmr is None:
        m = _MMR_VALUE_RE.search(html)
        if m:
            digits = re.sub(r"[^\d]", "", m.group(1))
            if digits:
                stats.mmr = int(digits)
    m = _MATCHES_JSON_RE.search(html)
    if m:
        try:
            val = int(m.group(1))
            if 1 <= val <= 1_000_000:
                stats.matches = val
        except ValueError:
            pass
    if stats.matches is None:
        m = _STATS_TEXT_RE.search(html)
        if m:
            stats.matches = int(m.group(1))
    m = _WINRATE_JSON_RE.search(html)
    if m:
        try:
            wr = float(m.group(1))
            if 0.0 < wr <= 1.0:
                wr *= 100.0
            if 0.0 < wr <= 100.0:
                stats.winrate = round(wr, 1)
        except ValueError:
            pass
    if stats.winrate is None:
        m = _WINRATE_TEXT_RE.search(html)
        if m:
            try:
                stats.winrate = float(m.group(1))
            except ValueError:
                pass
    m = _RANK_NAME_JSON_RE.search(html)
    if m:
        rank = m.group(1).strip()
        if rank and rank.lower() not in {"unknown", "unranked", "none"}:
            stats.rank_name = _normalize_rank_name(rank)
    return stats


def extract_account_id(value: str) -> int | None:
    value = value.strip()
    m = DOTABUFF_RE.search(value)
    if m:
        return int(m.group(1))
    if NUMBER_RE.fullmatch(value):
        return int(value)
    # Also accept a bare OpenDota player URL for compatibility with fallback.
    m = re.search(r"(?:https?://)?(?:www\.)?opendota\.com/players/(\d+)", value, re.I)
    if m:
        return int(m.group(1))
    return None


def parse_dotabuff(html: str, account_id: int) -> dict[str, Any]:
    """17:58 parser: HTML/meta/title + legacy stats text."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    nickname = None
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        nickname = str(og["content"]).split(" - ")[0].strip()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if not nickname and title:
        nickname = re.split(r"\s+-\s+(?:Overview|DOTABUFF)", title, maxsplit=1, flags=re.I)[0].strip()

    def row(label):
        m = re.search(rf"{re.escape(label)}\s+(\d[\d,]*)\s+(\d+(?:\.\d+)?)%", text, re.I)
        return (int(m.group(1).replace(',', '')), float(m.group(2))) if m else (None, None)

    total, wr = row("Stats Recorded")
    ranked, rwr = row("Ranked MM")
    rank_name = None
    rank_tier = dotabuff_rank_tier(html)
    if rank_tier is not None:
        rank_name = rank_label_from_tier(rank_tier)
    else:
        # Прежний свободный поиск «Rank …» цеплял слово «Ranked» и давал «ed» —
        # теперь только JSON-поле с именем ранга, и только если оно распознаётся.
        m_rank = _RANK_NAME_JSON_RE.search(html)
        if m_rank:
            tier_guess = rank_name_to_tier(m_rank.group(1).replace("_", " ").strip())
            rank_name = rank_label_from_tier(tier_guess) if tier_guess is not None else None
    out = {
        "account_id": account_id,
        "dotabuff_url": DOTABUFF_PROFILE.format(account_id=account_id),
        "nickname": nickname,
        "total_matches": total,
        "winrate": wr,
        "ranked_matches": ranked,
        "ranked_winrate": rwr,
        "rank_name": rank_name,
    }
    if rank_tier is not None:
        out["rank_tier"] = rank_tier
    return out


async def _read_json(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    body = await resp.text(errors="replace")
    try:
        data = json.loads(body)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("unexpected JSON shape")
    return data


def _normalize_rank_name(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).replace("_", " ").strip()
    if not raw or raw.lower() in {"unknown", "unranked", "none"}:
        return None
    tier = rank_name_to_tier(raw)
    if tier is not None:
        return rank_label_from_tier(tier)
    return raw


def _rank_tier_to_name(value: Any) -> str | None:
    return rank_label_from_tier(value)


class DotabuffProvider(PlayerDataProvider):
    name = "dotabuff"

    # Кеш успешных ответов и «предохранитель» Dotabuff — на экземпляр провайдера
    # (в проде он один на процесс, в тестах — свой на каждый тест).
    CACHE_TTL = 600.0
    BREAKER_THRESHOLD = 3
    BREAKER_SECONDS = 600.0

    def __init__(self, timeout: float = 8.0, session=None, http=None):
        self.timeout = float(timeout)
        self._session = session
        self.http = http
        self._cache: dict[int, tuple[float, PlayerStats]] = {}
        self._db_failures = 0
        self._db_open_until = 0.0

    async def _fetch_dotabuff(self, account_id: int) -> PlayerStats:
        url = DOTABUFF_PROFILE.format(account_id=account_id)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; DotaTournamentBot/2.0)"}
        try:
            if self.http is not None:
                r = await self.http.get(url, headers=headers, timeout=self.timeout)
                status = r.status
                html = r.body
                content_type = (r.headers.get("content-type") or "").lower()
            else:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                    async with s.get(url, allow_redirects=True) as r:
                        status = r.status
                        html = await r.text(errors="ignore")
                        content_type = (r.headers.get("Content-Type") or "").lower()
        except Exception as e:
            raise ProviderUnavailable("dotabuff", f"network error: {e}") from e

        if status == 404:
            raise LookupError(f"Игрок {account_id} не найден на Dotabuff")
        if status != 200:
            logger.warning("[DOTABUFF] HTTP %s account_id=%s", status, account_id)
            raise ProviderUnavailable("dotabuff", f"HTTP {status}")
        if "cloudflare" in html[:2000].lower() and ("challenge" in html[:2000].lower() or "just a moment" in html[:2000].lower()):
            raise ProviderUnavailable("dotabuff", "Cloudflare challenge")
        if "html" not in content_type and "<html" not in html.lower():
            raise ProviderUnavailable("dotabuff", "unexpected payload (not HTML)")

        parsed = parse_dotabuff(html, account_id)
        stats = PlayerStats(source="dotabuff", account_id=account_id, profile_url=url)
        stats.nickname = parsed.get("nickname")
        stats.matches = parsed.get("ranked_matches") or parsed.get("total_matches")
        stats.winrate = parsed.get("ranked_winrate") or parsed.get("winrate")
        stats.rank_name = parsed.get("rank_name")
        stats.raw.update(parsed)
        # A valid profile can contain no stats; that is a source failure for
        # verification purposes so OpenDota gets a chance.
        if stats.matches is None and stats.winrate is None and not stats.nickname:
            raise ProviderUnavailable("dotabuff", "no parsable profile data")
        return stats

    async def _fetch_opendota(self, account_id: int) -> PlayerStats:
        """Fallback with a hard 5-second wall-clock budget."""
        timeout = min(max(self.timeout, 1.0), 5.0)
        logger.info("[OPENDOTA] Fallback started account_id=%s", account_id)

        async def request(path: str) -> dict[str, Any] | None:
            url = path.format(account_id=account_id)
            try:
                if self.http is not None:
                    r = await self.http.get(url, timeout=timeout)
                    if r.status != 200:
                        logger.warning("[OPENDOTA] HTTP %s account_id=%s", r.status, account_id)
                        return None
                    data = json.loads(r.body)
                    return data if isinstance(data, dict) else None
                aio_timeout = aiohttp.ClientTimeout(total=timeout)
                async with aiohttp.ClientSession(timeout=aio_timeout) as s:
                    async with s.get(url) as resp:
                        if resp.status != 200:
                            logger.warning("[OPENDOTA] HTTP %s account_id=%s", resp.status, account_id)
                            return None
                        return await _read_json(resp)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[OPENDOTA] request error account_id=%s: %s", account_id, e)
                return None

        try:
            async with asyncio.timeout(5.0):
                profile = await request(OPENDOTA_PROFILE)
                if not profile:
                    raise ProviderUnavailable("opendota", "profile unavailable")
                wl = await request(OPENDOTA_WL)
                counts = await request(OPENDOTA_COUNTS)
        except asyncio.TimeoutError as e:
            logger.warning("[OPENDOTA] Timeout after 5s account_id=%s", account_id)
            raise ProviderUnavailable("opendota", "timeout after 5s") from e

        stats = PlayerStats(source="opendota", account_id=account_id,
                            profile_url=f"https://www.opendota.com/players/{account_id}")
        stats.nickname = profile.get("personaname") or profile.get("name")
        if isinstance(wl, dict):
            wins = wl.get("win")
            losses = wl.get("lose")
            try:
                wins_i = int(wins) if wins is not None else None
                losses_i = int(losses) if losses is not None else None
                if wins_i is not None and losses_i is not None:
                    total = wins_i + losses_i
                    stats.matches = total
                    if total:
                        stats.winrate = round(wins_i * 100.0 / total, 1)
            except (TypeError, ValueError):
                pass
        # OpenDota player profile may expose MMR fields on some profiles.
        for key in ("mmr_estimate", "mmr"):
            try:
                value = profile.get(key)
                if value is not None:
                    stats.mmr = int(value)
                    break
            except (TypeError, ValueError):
                pass
        # OpenDota exposes rank_tier on many profiles.  It is encoded as
        # medal*10 + star (Herald=1 ... Divine=7; Immortal=8).
        rank_tier = profile.get("rank_tier")
        stats.rank_name = _rank_tier_to_name(rank_tier)
        if stats.rank_name:
            stats.raw["rank_tier"] = int(rank_tier)
            stats.raw["rank_name"] = stats.rank_name
        if profile.get("leaderboard_rank") is not None:
            try:
                stats.leaderboard_rank = int(profile.get("leaderboard_rank"))
                stats.raw["leaderboard_rank"] = stats.leaderboard_rank
            except (TypeError, ValueError):
                pass
        if isinstance(counts, dict):
            stats.raw["counts"] = counts
        stats.raw["opendota_profile"] = profile
        logger.info("[OPENDOTA] OK account_id=%s matches=%s mmr=%s rank=%s", account_id, stats.matches, stats.mmr, stats.rank_name)
        return stats

    async def get_player(self, account_id: int) -> PlayerStats:
        """Цепочка: Dotabuff → OpenDota → ошибка (данные не выдумываются).

        Короткие таймауты, без повторов; успешный ответ кешируется на 10 минут.
        После 3 подряд сбоев Dotabuff (Cloudflare/сеть) он 10 минут не вызывается,
        чтобы запросы игроков не ждали заведомо недоступный источник.
        """
        import time as _time

        cached = self._cache.get(int(account_id))
        if cached and cached[0] > _time.monotonic():
            return cached[1]
        stats = await self._get_player_chain(account_id)
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[int(account_id)] = (_time.monotonic() + self.CACHE_TTL, stats)
        return stats

    async def _dotabuff_guarded(self, account_id: int) -> PlayerStats:
        import time as _time

        if self._db_open_until > _time.monotonic():
            raise ProviderUnavailable("dotabuff", "temporarily skipped after repeated failures")
        try:
            stats = await self._fetch_dotabuff(account_id)
        except ProviderUnavailable:
            self._db_failures += 1
            if self._db_failures >= self.BREAKER_THRESHOLD:
                self._db_open_until = _time.monotonic() + self.BREAKER_SECONDS
                self._db_failures = 0
                logger.warning("[DOTABUFF] %s failures in a row — skipping Dotabuff for %.0fs",
                               self.BREAKER_THRESHOLD, self.BREAKER_SECONDS)
            raise
        self._db_failures = 0
        return stats

    async def _get_player_chain(self, account_id: int) -> PlayerStats:
        try:
            stats = await self._dotabuff_guarded(account_id)
            # OpenDota нужен не только как fallback: rank_tier является
            # источником диапазона exact MMR для турнирной проверки.
            # Медаль со значка Dotabuff достаточна; для Титанов нужен ещё номер в
            # таблице лидеров — его даёт OpenDota.
            if not stats.raw.get("rank_tier") or int(stats.raw.get("rank_tier") or 0) >= 80:
                try:
                    od = await self._fetch_opendota(account_id)
                    if od.raw.get("rank_tier") is not None:
                        stats.raw["rank_tier"] = od.raw["rank_tier"]
                        stats.rank_name = od.rank_name or stats.rank_name
                        if stats.leaderboard_rank is None:
                            stats.leaderboard_rank = od.leaderboard_rank
                        if stats.mmr is None:
                            stats.mmr = od.mmr
                        if stats.nickname is None:
                            stats.nickname = od.nickname
                except (LookupError, ProviderUnavailable) as od_error:
                    logger.warning(
                        "[OPENDOTA] Rank metadata unavailable account_id=%s: %s",
                        account_id, od_error,
                    )
            return stats
        except (LookupError, ProviderUnavailable) as db_error:
            logger.warning("[DOTABUFF] Unavailable; OpenDota fallback account_id=%s", account_id)
            try:
                return await self._fetch_opendota(account_id)
            except LookupError:
                raise db_error
            except ProviderUnavailable as od_error:
                # Preserve Dotabuff failure as the primary diagnostic; OpenDota
                # is strictly optional and must never reject the player.
                raise ProviderUnavailable(
                    "dotabuff+opendota",
                    f"dotabuff={db_error.reason}; opendota={od_error.reason}",
                ) from od_error

    async def close(self) -> None:
        return None


async def fetch_dotabuff(account_id: int):
    """Compatibility function from the 17:58 build."""
    try:
        stats = await DotabuffProvider(timeout=15.0). _fetch_dotabuff(account_id)
        return stats.raw | {"account_id": account_id, "dotabuff_url": DOTABUFF_PROFILE.format(account_id=account_id),
                             "nickname": stats.nickname, "total_matches": stats.raw.get("total_matches"),
                             "ranked_matches": stats.raw.get("ranked_matches"), "ranked_winrate": stats.raw.get("ranked_winrate"),
                             "rank_name": stats.rank_name,
                             "winrate": stats.winrate}
    except Exception:
        return None


async def fetch_opendota(account_id: int):
    try:
        stats = await DotabuffProvider(timeout=5.0)._fetch_opendota(account_id)
        return {"account_id": account_id, "nickname": stats.nickname, "matches": stats.matches,
                "winrate": stats.winrate, "mmr": stats.mmr, "rank_name": stats.rank_name, "source": "OpenDota", **stats.raw}
    except Exception:
        return None


async def lookup_profile(raw: str):
    account_id = extract_account_id(raw)
    if account_id is None:
        return {"ok": False, "error": "Нужна ссылка Dotabuff/OpenDota или числовой account ID."}
    try:
        stats = await DotabuffProvider(timeout=15.0).get_player(account_id)
    except Exception as e:
        return {"ok": False, "error": f"Не удалось получить профиль: {e}"}
    result = {"ok": True, "account_id": account_id,
              "source": "OpenDota" if stats.source == "opendota" else "Dotabuff",
              "dotabuff_url": DOTABUFF_PROFILE.format(account_id=account_id)}
    result.update({k: v for k, v in stats.raw.items() if v not in (None, "")})
    result.update({"nickname": stats.nickname, "matches": stats.matches, "winrate": stats.winrate, "mmr": stats.mmr, "rank_name": stats.rank_name})
    return result
