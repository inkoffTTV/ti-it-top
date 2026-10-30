"""Настройка логирования. Секреты в логи не попадают."""
from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:  # уже настроено
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    # приглушаем болтливые библиотеки
    for noisy in ("aiogram.dispatcher", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RedactingFilter(logging.Filter):
    """Фильтр, вырезающий значения секретов из сообщений логов."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — формат может быть битым, логируем как есть
            return True
        redacted = msg
        for secret in self.secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "***REDACTED***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def install_secret_redaction(secrets: list[str]) -> None:
    flt = RedactingFilter(secrets)
    logging.getLogger().addFilter(flt)
    for name in (
        "STEAM", "DOTABUFF", "STRATZ", "VERIFICATION", "REGISTRATION",
        "DATABASE", "SUPPORT", "ADMIN", "TEAM", "TOURNAMENT", "APPLICATION",
    ):
        logging.getLogger(name).addFilter(flt)
