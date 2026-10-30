"""5.13: сторож сервиса и служебные рассылки.

* при запуске — сообщение владельцу: бот запущен, прошлая остановка штатная или аварийная;
* место на диске (меньше 1 ГБ или 5% — тревога, не чаще раза в 6 часов);
* всплеск ошибок в журнале (10+ за час; сетевые сбои Telegram не считаются) — последние ошибки владельцу;
* утренняя сводка организаторам (09:00 МСК, раз в день);
* копия базы владельцу в Telegram (сжатая, не чаще раза в 20 часов) — если сервер умрёт, данные останутся.

Состояние (метки) хранится в state_dir вне папки кода, поэтому деплой его не стирает.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import shutil
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("WATCHDOG")

SendFn = Callable[[str], Awaitable[None]]
NOISE = ("Failed to fetch updates", "TelegramNetworkError", "TelegramServerError", "Unclosed client session",
         "Unclosed connector", "Sleep for", "[WATCHDOG]")


class ErrorCounter(logging.Handler):
    """Считает ошибки журнала (без сетевого шума Telegram) и хранит последние."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.count = 0
        self.last: deque[str] = deque(maxlen=5)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        if any(n in msg for n in NOISE):
            return
        self.count += 1
        self.last.append(f"{record.name}: {msg[:200]}")

    def take(self) -> tuple[int, list[str]]:
        out = (self.count, list(self.last))
        self.count = 0
        self.last.clear()
        return out


ERRORS = ErrorCounter()


def install() -> None:
    root = logging.getLogger()
    if ERRORS not in root.handlers:
        root.addHandler(ERRORS)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def startup_text(state_dir: Path, version: str) -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    clean = state_dir / "clean_shutdown"
    started = state_dir / "last_start"
    if not started.exists():
        how = "первый запуск сторожа"
    elif clean.exists():
        how = "прошлая остановка штатная (деплой или перезапуск)"
    else:
        how = "<b>прошлая остановка аварийная</b> — процесс упал, был убит или сервер перезагрузился"
    clean.unlink(missing_ok=True)
    started.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    return f"Бот TI:IT TOP запущен (версия {version}): {how}."


def mark_clean_shutdown(state_dir: Path) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "clean_shutdown").write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except OSError:
        logger.exception("[WATCHDOG] cannot write shutdown marker")


def disk_alert(path: Path, *, min_free_gb: float = 1.0, min_free_pct: float = 5.0) -> str | None:
    usage = shutil.disk_usage(path)
    free_gb = usage.free / 1024 ** 3
    pct = usage.free * 100 / usage.total if usage.total else 100
    if free_gb < min_free_gb or pct < min_free_pct:
        return f"Мало места на диске сервера: свободно {free_gb:.1f} ГБ ({pct:.0f}%). База и бэкапы могут перестать записываться."
    return None


def pack_backup(db_backup: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (db_backup.name + ".gz")
    with db_backup.open("rb") as src, gzip.open(target, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    return target


def backup_send_due(state_dir: Path, *, hours: float = 20.0) -> bool:
    raw = _read(state_dir / "backup_sent")
    try:
        return time.time() - float(raw) >= hours * 3600
    except ValueError:
        return True


def mark_backup_sent(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "backup_sent").write_text(str(time.time()), encoding="utf-8")


async def ops_loop(send_owner: SendFn, send_admins: SendFn, *, state_dir: Path, disk_path: Path,
                   interval: float = 60.0, on_morning: Callable[[], Awaitable[Any]] | None = None) -> None:
    from services.digest import digest_due

    install()
    last_disk = 0.0
    last_errors = time.time()
    while True:
        try:
            text = await digest_due(state_dir / "digest_last")
            if text:
                await send_admins(text)
            if text is not None and on_morning is not None:  # раз в день вместе со сводкой (свежая проверка рангов)
                try:
                    await on_morning()
                except Exception:  # noqa: BLE001
                    logger.exception("[WATCHDOG] morning job failed")
            now = time.time()
            if now - last_disk >= 1800:
                alert = await asyncio.to_thread(disk_alert, disk_path)
                if alert and now - float(_read(state_dir / "disk_alert") or 0) >= 6 * 3600:
                    await send_owner(alert)
                    (state_dir / "disk_alert").write_text(str(now), encoding="utf-8")
                last_disk = now
            if now - last_errors >= 3600:
                n, last = ERRORS.take()
                if n >= 10:
                    await send_owner(f"За последний час в журнале бота {n} ошибок. Последние:\n" + "\n".join("• " + _esc(x) for x in last))
                last_errors = now
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[WATCHDOG] iteration failed")
        await asyncio.sleep(interval)


def _esc(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
