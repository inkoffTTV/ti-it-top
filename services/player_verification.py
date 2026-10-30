"""PlayerVerificationService — единая точка верификации игроков.

Telegram handler -> Steam identity -> OpenDota rank metadata -> optional providers.

OpenDota rank_tier используется как источник диапазона exact MMR.
Недоступность внешней статистики не должна ломать регистрацию.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from services.cache import CacheEntry, ProfileCache
from services.dota_steamclient import DotaSteamClientProvider
from services.dotabuff import DotabuffProvider
from services.providers_base import PlayerDataProvider, ProviderUnavailable
from services.steam import SteamProvider
from services.stratz import StratzProvider
from utils.ranks import rank_label_from_tier
from utils.validation import SteamInputKind, parse_steam_input

logger = logging.getLogger("VERIFICATION")


@dataclass(slots=True)
class VerificationResult:
    status: str                     # VERIFIED | PENDING | REJECTED | INVALID_ID
    account_id: int | None = None
    steam_id64: str | None = None
    nickname: str | None = None
    source: str | None = None       # steam / dotabuff / stratz / cache
    mmr: int | None = None
    rank_name: str | None = None
    matches: int | None = None
    winrate: float | None = None
    profile_url: str | None = None
    error: str | None = None        # текст для пользователя при INVALID_ID
    from_cache: bool = False
    hours_total: int | None = None  # часы в Dota 2 (официальные данные Valve)
    medal_rank: int | None = None   # dotaRank (медаль) — официальный ранг Valve
    leaderboard_rank: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_valid_identity(self) -> bool:
        return self.status in ("VERIFIED", "PENDING")


class PlayerVerificationService:
    def __init__(
        self,
        steam: SteamProvider,
        dotabuff: DotabuffProvider | None = None,
        stratz: StratzProvider | None = None,
        cache: ProfileCache | None = None,
        dota_client: DotaSteamClientProvider | None = None,
    ):
        self.steam = steam
        self.dota_client = dota_client or DotaSteamClientProvider(api_key=steam.api_key)
        self.dotabuff = dotabuff or DotabuffProvider()
        self.stratz = stratz or StratzProvider()
        self.cache = cache

    # ------------------------------------------------------------------ main

    async def verify_input(self, raw_input: str) -> VerificationResult:
        """Полный цикл: парсинг -> identity -> провайдеры -> результат."""
        parsed = parse_steam_input(raw_input)
        if not parsed.ok:
            return VerificationResult(
                status="INVALID_ID", error=parsed.error or "Некорректный ввод."
            )

        if parsed.kind == SteamInputKind.VANITY_URL:
            return await self._verify_vanity(parsed.vanity or "")

        assert parsed.steam_id64 and parsed.account_id is not None
        return await self.verify_ids(parsed.steam_id64, parsed.account_id)

    async def verify_ids(self, steam_id64: str, account_id: int) -> VerificationResult:
        # 0. Кэш успешных проверок
        cached = self.cache.get(steam_id64=steam_id64) if self.cache else None
        if cached is not None and cached.verified:
            logger.info("[VERIFICATION] Cache hit (verified) sid=%s", steam_id64)
            return VerificationResult(
                status="VERIFIED",
                account_id=cached.account_id or account_id,
                steam_id64=cached.steam_id64 or steam_id64,
                nickname=cached.nickname,
                source=f"cache:{cached.source}",
                mmr=cached.mmr,
                rank_name=cached.rank_name,
                matches=cached.matches,
                winrate=cached.winrate,
                profile_url=cached.profile_url,
                hours_total=cached.hours_total,
                medal_rank=cached.medal_rank,
                leaderboard_rank=cached.leaderboard_rank,
                from_cache=True,
            )

        notes: list[str] = []
        nickname = cached.nickname if cached else None
        profile_url = cached.profile_url if cached else None
        mmr = cached.mmr if cached else None
        rank_name = cached.rank_name if cached else None
        matches = cached.matches if cached else None
        winrate = cached.winrate if cached else None
        stats_source = cached.source if cached else None

        # 1. Steam identity (валидность ID64 <-> аккаунт)
        identity_ok = False
        try:
            s_stats = await self.steam.get_player_by_id64(steam_id64)
            identity_ok = True
            nickname = s_stats.nickname or nickname
            profile_url = s_stats.profile_url or profile_url
            notes.append("steam: ok")
        except LookupError as e:
            # Профиля с таким ID не существует — это ошибка ввода, а не недоступность.
            logger.info("[VERIFICATION] Steam profile not found: %s", e)
            return VerificationResult(
                status="INVALID_ID",
                error=f"Steam-профиль с ID {steam_id64} не найден. Проверьте ввод.",
            )
        except ProviderUnavailable as e:
            notes.append(f"steam: unavailable ({e.reason})")
            logger.warning("[VERIFICATION] Steam unavailable: %s", e)

        # 1.5. Официальные данные Valve по Dota 2 (часы игры) — тоже optional.
        dota_data_ok = False
        hours_total: int | None = cached.hours_total if cached else None
        medal_rank: int | None = cached.medal_rank if cached else None
        leaderboard_rank: int | None = cached.leaderboard_rank if cached else None
        try:
            dc = await self.dota_client.get_player_by_id64(steam_id64)
            dota_data_ok = True
            nickname = nickname or dc.nickname
            profile_url = profile_url or dc.profile_url
            extra = []
            if dc.raw.get("dota_hours_total") is not None:
                hours_total = dc.raw["dota_hours_total"]
                extra.append(f"hours={dc.raw['dota_hours_total']}")
            if dc.raw.get("dota_rank"):
                medal_rank = dc.raw["dota_rank"]
                extra.append(f"rank={dc.raw['dota_rank']}")
            notes.append("steam_dota: ok" + (f" ({', '.join(extra)})" if extra else ""))
            if dc.raw.get("private_profile"):
                notes.append("steam_dota: профиль приватный")
        except LookupError as e:
            notes.append("steam_dota: player not found")
            logger.info("[STEAM] Dota client lookup failed: %s", e)
        except ProviderUnavailable as e:
            notes.append(f"steam_dota: unavailable ({e.reason})")
            logger.warning("[STEAM] Dota client unavailable: %s", e)

        # 2. Dotabuff (optional)
        db_ok = False
        try:
            db = await self.dotabuff.get_player(account_id)
            db_ok = True
            nickname = db.nickname or nickname
            mmr = db.mmr if db.mmr is not None else mmr
            matches = db.matches if db.matches is not None else matches
            winrate = db.winrate if db.winrate is not None else winrate
            if db.rank_name:
                rank_name = db.rank_name
            if db.raw.get("leaderboard_rank") is not None:
                try:
                    leaderboard_rank = int(db.raw["leaderboard_rank"])
                except (TypeError, ValueError):
                    pass
            if db.raw.get("rank_tier") is not None:
                try:
                    medal_rank = int(db.raw["rank_tier"])
                except (TypeError, ValueError):
                    pass
            stats_source = db.source or "dotabuff"
            if db.source == "opendota":
                notes.append("dotabuff: unavailable; opendota: ok")
            else:
                notes.append("dotabuff: ok")
        except LookupError as e:
            notes.append("dotabuff: player not found")
            logger.info("[VERIFICATION] Dotabuff lookup failed: %s", e)
        except ProviderUnavailable as e:
            notes.append(f"dotabuff: unavailable ({e.reason})")
            logger.warning("[DOTABUFF] Unavailable: %s", e)

        # 3. STRATZ (optional)
        st_ok = False
        try:
            st = await self.stratz.get_player(account_id)
            st_ok = True
            nickname = nickname or st.nickname
            if st.mmr is not None and mmr is None:
                mmr = st.mmr
            profile_url = profile_url or f"https://stratz.com/players/{account_id}"
            if stats_source is None:
                stats_source = "stratz"
            notes.append("stratz: ok")
        except LookupError as e:
            notes.append("stratz: player not found")
            logger.info("[VERIFICATION] STRATZ lookup failed: %s", e)
        except ProviderUnavailable as e:
            notes.append(f"stratz: unavailable ({e.reason})")
            logger.warning("[STRATZ] Unavailable: %s", e)

        got_stats = db_ok or st_ok

        # 4. Итоговый статус.
        #    Identity подтверждена Steam + есть какие-либо подтверждённые данные
        #    (авто-статистика Dotabuff/STRATZ или официальные данные Valve)
        #    => VERIFIED. Всё остальное (включая «Steam недоступен») => PENDING.
        if identity_ok and (got_stats or dota_data_ok):
            status = "VERIFIED"
            source = stats_source or ("steam_dota" if dota_data_ok else "steam")
        else:
            status = "PENDING"
            source = stats_source or ("steam" if identity_ok else "user_input")

        result = VerificationResult(
            status=status,
            account_id=account_id,
            steam_id64=steam_id64,
            nickname=nickname,
            source=source,
            mmr=mmr,
            rank_name=rank_label_from_tier(medal_rank) if medal_rank is not None else rank_name,
            matches=matches,
            winrate=winrate,
            profile_url=profile_url,
            hours_total=hours_total,
            medal_rank=medal_rank,
            leaderboard_rank=leaderboard_rank,
            notes=notes,
            from_cache=False,
        )

        # 5. Кэшируем только успех.
        if self.cache is not None and status == "VERIFIED":  # кэш только успеха
            self.cache.put(
                CacheEntry(
                    steam_id64=steam_id64,
                    account_id=account_id,
                    nickname=nickname,
                    mmr=mmr,
                    rank_name=rank_name,
                    matches=matches,
                    winrate=winrate,
                    profile_url=profile_url,
                    hours_total=hours_total,
                    medal_rank=medal_rank,
                    leaderboard_rank=leaderboard_rank,
                    source=source,
                    verified=True,
                )
            )
        elif self.cache is not None and got_stats is False and cached is not None:
            # Провайдеры легли, но у нас есть свежие кэшированные данные —
            # показываем их как cached (с пометкой источника).
            result.from_cache = True
            result.source = f"cache:{cached.source}"

        logger.info(
            "[VERIFICATION] sid=%s aid=%s -> %s (source=%s, notes=%s)",
            steam_id64, account_id, status, source, "; ".join(notes),
        )
        return result

    async def _verify_vanity(self, vanity: str) -> VerificationResult:
        try:
            sid = await self.steam.resolve_vanity(vanity)
        except ProviderUnavailable as e:
            return VerificationResult(
                status="PENDING",
                nickname=None,
                source="vanity_unresolved",
                error=None,
                notes=[f"vanity resolve unavailable: {e.reason}"],
            )
        if not sid:
            return VerificationResult(
                status="INVALID_ID",
                error=f"Не удалось разрешить vanity-ссылку '{vanity}'. "
                      "Введите Steam ID64 или ссылку вида steamcommunity.com/profiles/...",
            )
        from utils.validation import id64_to_account_id

        try:
            account_id = id64_to_account_id(sid)
        except ValueError as e:
            return VerificationResult(status="INVALID_ID", error=str(e))
        return await self.verify_ids(sid, account_id)

    async def recheck(self, steam_id64: str, account_id: int) -> VerificationResult:
        """🔄 Повторная проверка: сбрасываем кэш и идём заново."""
        if self.cache is not None:
            self.cache.invalidate(steam_id64=steam_id64, account_id=account_id)
        return await self.verify_ids(steam_id64, account_id)

    async def close(self) -> None:
        for provider in (self.steam, self.dota_client, self.dotabuff, self.stratz):
            try:
                await provider.close()
            except Exception as e:  # noqa: BLE001 — закрытие не должно ронять shutdown
                logger.warning("[VERIFICATION] Provider close error: %s", e)
