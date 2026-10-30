"""Часовой пояс проекта: хранение — UTC, ввод и показ — по Москве (настраивается).

TOURNAMENT_TIMEZONE (env, по умолчанию Europe/Moscow) и TOURNAMENT_TZ_LABEL
(по умолчанию «МСК»). Если в системе нет базы tzdata (бывает на Windows),
для Москвы используется фиксированный UTC+3 — Москва без перехода на летнее время с 2014 г.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone, tzinfo

TZ_NAME = (os.getenv("TOURNAMENT_TIMEZONE") or "Europe/Moscow").strip() or "Europe/Moscow"
TZ_LABEL = (os.getenv("TOURNAMENT_TZ_LABEL") or ("МСК" if TZ_NAME == "Europe/Moscow" else TZ_NAME)).strip()

DISPLAY_FORMAT = "%d.%m.%Y %H:%M"
INPUT_FORMAT = "%Y-%m-%dT%H:%M"
_LOCAL_FORMATS = (
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y %H.%M",
    "%d.%m.%Y, %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
)
_HAS_OFFSET = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def _load_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — нет tzdata: безопасный фиксированный сдвиг
        if name == "Europe/Moscow":
            return timezone(timedelta(hours=3), "MSK")
        return timezone.utc


PROJECT_TZ = _load_tz(TZ_NAME)


def tz_info() -> dict[str, str]:
    return {"name": TZ_NAME, "label": TZ_LABEL}


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite отдаёт время без зоны: по соглашению проекта это UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def to_local(value: datetime | None) -> datetime | None:
    utc = as_utc(value)
    return utc.astimezone(PROJECT_TZ) if utc else None


def fmt_local(value: datetime | None, *, label: bool = False) -> str | None:
    """«02.10.2026 17:50» (и « МСК» при label=True)."""
    local = to_local(value)
    if local is None:
        return None
    text = local.strftime(DISPLAY_FORMAT)
    return f"{text} {TZ_LABEL}" if label else text


def input_value(value: datetime | None) -> str | None:
    """Значение для <input type=datetime-local> в часовом поясе проекта."""
    local = to_local(value)
    return local.strftime(INPUT_FORMAT) if local else None


def parse_local(value: object) -> datetime | None:
    """Разбирает время из формы. Строка без зоны — время проекта (МСК); с Z/смещением — как есть.

    Возвращает aware-datetime в UTC или None для пустого значения.
    Бросает ValueError с понятным текстом при неверном формате.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _HAS_OFFSET.search(text) and "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    text = text.split(".")[0] if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+$", text) else text
    for fmt in _LOCAL_FORMATS:
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return _localize(naive).astimezone(timezone.utc)
    raise ValueError(f"Неверная дата «{text[:40]}». Нужен формат ДД.ММ.ГГГГ ЧЧ:ММ ({TZ_LABEL}).")


def _localize(naive: datetime) -> datetime:
    return naive.replace(tzinfo=PROJECT_TZ)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
