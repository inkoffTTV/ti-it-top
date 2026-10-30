"""Интерфейс провайдера данных об игроке и общий результат проверки."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class PlayerStats:
    """Данные, реально полученные от конкретного провайдера. None = недоступно."""

    nickname: str | None = None
    steam_id64: str | None = None
    account_id: int | None = None
    mmr: int | None = None
    rank_name: str | None = None
    leaderboard_rank: int | None = None
    matches: int | None = None
    winrate: float | None = None
    profile_url: str | None = None
    source: str = ""
    raw: dict = field(default_factory=dict)


class ProviderUnavailable(Exception):
    """Провайдер недоступен (403/Cloudflare/timeout/5xx/битый HTML). Не фатально."""

    def __init__(self, provider: str, reason: str = ""):
        super().__init__(f"{provider}: {reason}" if reason else provider)
        self.provider = provider
        self.reason = reason


class PlayerDataProvider(ABC):
    """Независимый провайдер данных об игроке. HTTP-запросы — только здесь."""

    name: str = "base"

    @abstractmethod
    async def get_player(self, account_id: int) -> PlayerStats:
        """Возвращает статистику или бросает ProviderUnavailable / LookupError."""

    async def close(self) -> None:  # pragma: no cover - по умолчанию ничего не нужно
        pass
