"""Суточный бюджет AI-запросов (бесплатные лимиты провайдеров невелики).

Счётчики хранятся в runtime/ai_usage.json и переживают перезапуск.
Лимиты по умолчанию укладываются в бесплатную квоту OpenRouter (~50 запросов в сутки
без купленных кредитов, общая с админским AI-помощником). Поменять — переменными окружения:
  AI_BOT_USER_DAILY    — вопросов в боте на одного пользователя в сутки (по умолчанию 5)
  AI_BOT_GLOBAL_DAILY  — вопросов в боте от всех пользователей в сутки (по умолчанию 30)
  AI_CHECK_DAILY       — AI-проверок игроков в сутки (по умолчанию 15)
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "runtime" / "ai_usage.json"
_lock = threading.Lock()


def _limit(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


def limits() -> dict[str, int]:
    return {
        "bot_user": _limit("AI_BOT_USER_DAILY", 5),
        "bot_global": _limit("AI_BOT_GLOBAL_DAILY", 30),
        "check": _limit("AI_CHECK_DAILY", 15),
    }


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("date") != _today():
        data = {"date": _today(), "bot_total": 0, "check_total": 0, "bot_users": {}}
    data.setdefault("bot_total", 0)
    data.setdefault("check_total", 0)
    data.setdefault("bot_users", {})
    return data


def _save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PATH)


def bot_remaining(user_id: int) -> tuple[int, int]:
    """(осталось вопросов у пользователя, осталось в общем лимите) на сегодня."""
    lim = limits()
    with _lock:
        data = _load()
    used_user = int(data["bot_users"].get(str(int(user_id)), 0))
    return max(0, lim["bot_user"] - used_user), max(0, lim["bot_global"] - int(data["bot_total"]))


def try_consume_bot(user_id: int) -> bool:
    lim = limits()
    with _lock:
        data = _load()
        key = str(int(user_id))
        used = int(data["bot_users"].get(key, 0))
        if used >= lim["bot_user"] or int(data["bot_total"]) >= lim["bot_global"]:
            return False
        data["bot_users"][key] = used + 1
        data["bot_total"] = int(data["bot_total"]) + 1
        _save(data)
        return True


def refund_bot(user_id: int) -> None:
    """Вернуть попытку, если AI не ответил (ошибка провайдера — не вина пользователя)."""
    with _lock:
        data = _load()
        key = str(int(user_id))
        if int(data["bot_users"].get(key, 0)) > 0:
            data["bot_users"][key] = int(data["bot_users"][key]) - 1
            data["bot_total"] = max(0, int(data["bot_total"]) - 1)
            _save(data)


def try_consume_check() -> bool:
    lim = limits()
    with _lock:
        data = _load()
        if int(data["check_total"]) >= lim["check"]:
            return False
        data["check_total"] = int(data["check_total"]) + 1
        _save(data)
        return True


def refund_check() -> None:
    """Вернуть AI-проверку, если провайдер не ответил (429/пустой ответ) — лимит не должен сгорать."""
    with _lock:
        data = _load()
        if int(data["check_total"]) > 0:
            data["check_total"] = int(data["check_total"]) - 1
            _save(data)


def check_remaining() -> int:
    with _lock:
        data = _load()
    return max(0, limits()["check"] - int(data["check_total"]))


def usage() -> dict:
    with _lock:
        data = _load()
    lim = limits()
    return {
        "date": data["date"],
        "bot_total": int(data["bot_total"]),
        "bot_limit": lim["bot_global"],
        "bot_user_limit": lim["bot_user"],
        "check_total": int(data["check_total"]),
        "check_limit": lim["check"],
    }
