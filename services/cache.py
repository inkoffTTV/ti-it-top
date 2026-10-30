"""Кэш результатов проверки профилей с TTL.

Правила:
  * кэшируются только успешные результаты;
  * ошибки провайдеров (ProviderUnavailable) не становятся «успешным кэшем»;
  * при недоступности провайдеров старые валидные данные отдаются как cached
    с пометкой источника ("cache").
Хранилище — JSON-файл (profile_cache.json, в .gitignore).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field

logger = logging.getLogger("VERIFICATION")


@dataclass(slots=True)
class CacheEntry:
    steam_id64: str | None = None
    account_id: int | None = None
    nickname: str | None = None
    mmr: int | None = None
    rank_name: str | None = None
    matches: int | None = None
    winrate: float | None = None
    profile_url: str | None = None
    hours_total: int | None = None  # часы в Dota 2 (официальные данные Valve)
    medal_rank: int | None = None   # dotaRank (медаль) — официальный ранг Valve
    leaderboard_rank: int | None = None
    source: str = ""
    verified: bool = False
    cached_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CacheEntry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class ProfileCache:
    def __init__(self, path: str = "profile_cache.json", ttl: int = 1800):
        self.path = path
        self.ttl = max(60, int(ttl))
        self._data: dict[str, dict] = {}
        self._loaded = False

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    self._data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[VERIFICATION] Cache load failed (%s), starting empty", e)
            self._data = {}

    def _save(self) -> None:
        if self.path == ":memory:":
            return
        try:
            dir_name = os.path.dirname(os.path.abspath(self.path))
            fd, tmp = tempfile.mkstemp(dir=dir_name or ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            logger.warning("[VERIFICATION] Cache save failed: %s", e)

    # -- API -----------------------------------------------------------------

    @staticmethod
    def _key(steam_id64: str | None, account_id: int | None) -> str | None:
        if steam_id64:
            return f"sid:{steam_id64}"
        if account_id is not None:
            return f"aid:{account_id}"
        return None

    def get(self, steam_id64: str | None = None, account_id: int | None = None) -> CacheEntry | None:
        """Возвращает запись кэша, если она есть и не истекла по TTL."""
        self._load()
        key = self._key(steam_id64, account_id)
        if key is None:
            return None
        raw = self._data.get(key)
        if not raw:
            return None
        entry = CacheEntry.from_dict(raw)
        if time.time() - entry.cached_at > self.ttl:
            logger.info("[VERIFICATION] Cache entry expired key=%s", key)
            self._data.pop(key, None)
            self._save()
            return None
        return entry

    def put(self, entry: CacheEntry) -> None:
        """Сохраняет ТОЛЬКО успешные результаты (verified или есть данные Steam)."""
        self._load()
        key = self._key(entry.steam_id64, entry.account_id)
        if key is None:
            return
        if not entry.verified and not any(
            (entry.nickname, entry.mmr, entry.matches, entry.winrate)
        ):
            # Пустой/ошибочный результат не кэшируем как успех.
            logger.debug("[VERIFICATION] Skipping non-cacheable result key=%s", key)
            return
        entry.cached_at = time.time()
        self._data[key] = entry.to_dict()
        self._save()
        logger.info("[VERIFICATION] Cached profile key=%s source=%s", key, entry.source)

    def invalidate(self, steam_id64: str | None = None, account_id: int | None = None) -> None:
        self._load()
        key = self._key(steam_id64, account_id)
        if key and key in self._data:
            self._data.pop(key)
            self._save()
