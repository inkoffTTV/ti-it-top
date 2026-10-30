"""Валидация и парсинг Steam-идентификаторов, названий команд и дат."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

STEAM_ID64_MIN = 76561197960265728


class SteamInputKind(str, Enum):
    ID64 = "id64"
    ACCOUNT_ID = "account_id"
    PROFILE_URL = "profile_url"
    VANITY_URL = "vanity_url"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class SteamParseResult:
    kind: SteamInputKind
    steam_id64: str | None = None
    account_id: int | None = None
    vanity: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.kind != SteamInputKind.UNKNOWN


def id64_to_account_id(steam_id64: int | str) -> int:
    return int(steam_id64) - STEAM_ID64_MIN


def account_id_to_id64(account_id: int | str) -> int:
    return int(account_id) + STEAM_ID64_MIN


_PROFILE_URL_RE = re.compile(
    r"^https?://(?:www\.)?steamcommunity\.com/profiles/(\d{17})/?$", re.IGNORECASE
)
_VANITY_URL_RE = re.compile(
    r"^https?://(?:www\.)?steamcommunity\.com/id/([A-Za-z0-9_-]{2,32})/?$", re.IGNORECASE
)
_ID64_RE = re.compile(r"^\d{17}$")
_ACCOUNT_ID_RE = re.compile(r"^\d{1,10}$")


def parse_steam_input(raw: str) -> SteamParseResult:
    """Различает Steam ID64, Dota account ID, profile URL и vanity URL.

    Правила различия:
      * 17-значное число, начинающееся с 7656119 (и >= STEAM_ID64_MIN) — Steam ID64;
      * число короче 10 знаков и меньше 2^31 — Dota account ID;
      * https://steamcommunity.com/profiles/<ID64> — profile URL;
      * https://steamcommunity.com/id/<vanity> — vanity URL (требует разрешения через Steam).
    """
    text = (raw or "").strip()
    if not text:
        return SteamParseResult(kind=SteamInputKind.UNKNOWN, error="Пустое значение.")

    m = _PROFILE_URL_RE.match(text)
    if m:
        sid = m.group(1)
        try:
            return SteamParseResult(
                kind=SteamInputKind.PROFILE_URL,
                steam_id64=sid,
                account_id=id64_to_account_id(sid),
            )
        except ValueError as e:  # pragma: no cover
            return SteamParseResult(kind=SteamInputKind.UNKNOWN, error=str(e))

    m = _VANITY_URL_RE.match(text)
    if m:
        return SteamParseResult(kind=SteamInputKind.VANITY_URL, vanity=m.group(1))

    if text.startswith("http"):
        return SteamParseResult(
            kind=SteamInputKind.UNKNOWN,
            error="Неподдерживаемая ссылка. Используйте профиль вида "
                  "steamcommunity.com/profiles/7656119XXXXXXXXXX или steamcommunity.com/id/<ник>",
        )

    if _ID64_RE.match(text):
        try:
            value = int(text)
        except ValueError:
            return SteamParseResult(kind=SteamInputKind.UNKNOWN, error="Некорректное число.")
        if value < STEAM_ID64_MIN:
            return SteamParseResult(
                kind=SteamInputKind.UNKNOWN,
                error=f"Число {text} не является корректным Steam ID64 "
                      f"(должно быть >= {STEAM_ID64_MIN}).",
            )
        return SteamParseResult(
            kind=SteamInputKind.ID64,
            steam_id64=text,
            account_id=id64_to_account_id(text),
        )

    if _ACCOUNT_ID_RE.match(text):
        value = int(text)
        if value >= 2**31:
            return SteamParseResult(
                kind=SteamInputKind.UNKNOWN,
                error="Слишком большое число для Dota account ID. "
                      "Если это Steam ID64 — введите все 17 цифр.",
            )
        return SteamParseResult(
            kind=SteamInputKind.ACCOUNT_ID,
            account_id=value,
            steam_id64=str(account_id_to_id64(value)),
        )

    return SteamParseResult(
        kind=SteamInputKind.UNKNOWN,
        error="Не распознано. Поддерживаются: Steam ID64 (7656119XXXXXXXXXX), "
              "Dota account ID, ссылка на профиль Steam.",
    )


# ---------------------------------------------------------------------------
# Текстовые валидаторы
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def validate_team_name(raw: str) -> tuple[bool, str, str | None]:
    name = normalize_text(raw)
    if not name:
        return False, "", "Название команды не может быть пустым."
    if len(name) > 64:
        return False, name, "Название команды слишком длинное (максимум 64 символа)."
    forbidden = set("<>&`\\\"'{}[]|;")
    if any(ch in forbidden for ch in name):
        return False, name, "Название содержит недопустимые символы."
    return True, name, None


DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M")


def parse_date(raw: str) -> datetime | None:
    """Парсит дату запуска турнира. Ввод — московское время; возвращает aware-datetime в UTC или None."""
    from services.tournament_engine.timeutil import PROJECT_TZ

    text = normalize_text(raw)
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=PROJECT_TZ).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


MAX_MESSAGE_LEN = 3000


def validate_support_message(raw: str) -> tuple[bool, str, str | None]:
    text = (raw or "").strip()
    if not text:
        return False, "", "Сообщение не может быть пустым."
    if len(text) > MAX_MESSAGE_LEN:
        return False, text, f"Сообщение слишком длинное (максимум {MAX_MESSAGE_LEN} символов)."
    return True, text, None
