"""Фоновые уведомления в Telegram для Mini App 5.0.

Правила:
* HTTP-ответ Mini App никогда не ждёт Telegram API: отправка ставится в фон;
* у каждого вызова Bot API короткий таймаут (SEND_TIMEOUT);
* ошибка Telegram (403/таймаут/Connection reset) только пишется в лог;
* один общий Bot-клиент с тем же HTTP_PROXY_URL, что и у бота
  (раньше на каждое уведомление создавалась новая сессия без таймаута);
* MINIAPP_V2_TELEGRAM_DISABLED=1 отключает отправку (локальные тесты/стейджинг).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

logger = logging.getLogger("MINIAPP_V2.NOTIFY")

SEND_TIMEOUT = 10.0
FILE_TIMEOUT = 15.0

_bot: Any = None
_tasks: set[asyncio.Task] = set()


def telegram_disabled() -> bool:
    return os.getenv("MINIAPP_V2_TELEGRAM_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _get_bot(config):
    """Ленивый общий Bot-клиент (создаётся при первом уведомлении)."""
    global _bot
    if _bot is None:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.enums import ParseMode

        proxy = (config.http_proxy_url or "").strip() or None
        session = AiohttpSession(proxy=proxy, timeout=SEND_TIMEOUT)
        _bot = Bot(config.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return _bot


async def close() -> None:
    global _bot
    for task in list(_tasks):
        task.cancel()
    if _bot is not None:
        try:
            await _bot.session.close()
        except Exception:  # noqa: BLE001
            pass
        _bot = None


def pending_tasks() -> int:
    return len(_tasks)


def fire(job: Callable[[], Awaitable[Any]], *, label: str) -> asyncio.Task | None:
    """Запускает job в фоне. Никогда не бросает исключение вызывающему коду."""
    if telegram_disabled():
        logger.info("[NOTIFY] Telegram disabled, skipped: %s", label)
        return None

    async def runner() -> None:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[NOTIFY] %s failed: %s: %s", label, type(exc).__name__, exc)

    try:
        task = asyncio.get_running_loop().create_task(runner(), name=f"miniapp-v2:{label}")
    except RuntimeError:
        logger.warning("[NOTIFY] No running loop, skipped: %s", label)
        return None
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def send_message(config, chat_id: int, text: str, reply_markup=None) -> None:
    bot = _get_bot(config)
    await asyncio.wait_for(
        bot.send_message(int(chat_id), text, reply_markup=reply_markup, disable_web_page_preview=True),
        timeout=SEND_TIMEOUT + 2,
    )


def notify_user(config, chat_id: int | None, text: str, reply_markup=None, *, label: str = "user") -> None:
    if not chat_id:
        return
    fire(lambda: send_message(config, chat_id, text, reply_markup), label=f"{label}->{chat_id}")


def notify_admins(config, text: str, reply_markup=None, *, label: str = "admins") -> None:
    admin_ids = sorted(int(x) for x in config.admin_ids)
    if not admin_ids:
        logger.warning("[NOTIFY] No admins configured for: %s", label)
        return

    async def job() -> None:
        delivered = 0
        for admin_id in admin_ids:
            try:
                await send_message(config, admin_id, text, reply_markup)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 — админ мог заблокировать бота
                logger.warning("[NOTIFY] %s -> admin %s failed: %s", label, admin_id, exc)
        logger.info("[NOTIFY] %s delivered to %s/%s admins", label, delivered, len(admin_ids))

    fire(job, label=label)


async def send_photo(config, chat_id: int, data: bytes, caption: str, filename: str = "results.png") -> None:
    from aiogram.types import BufferedInputFile

    bot = _get_bot(config)
    await asyncio.wait_for(bot.send_photo(int(chat_id), BufferedInputFile(data, filename), caption=caption), timeout=30)


def send_autopair(config, report: dict | None) -> None:
    """Капитанам — их новый соперник или BYE, админам — сводка автодобора пар."""
    if not report or report.get("action") not in ("rebuilt", "filled"):
        return
    for msg in report.get("messages") or []:
        notify_user(config, msg.get("telegram_id"), msg.get("text") or "", label="autopair")
    if report.get("admin_text"):
        notify_admins(config, report["admin_text"], label="autopair")


async def download_file(config, file_id: str) -> bytes:
    """Скачивает файл Telegram по file_id (только для явных админских действий)."""
    from io import BytesIO

    if telegram_disabled():
        raise RuntimeError("Отправка/загрузка через Telegram отключена на этом сервере.")
    bot = _get_bot(config)
    tg_file = await asyncio.wait_for(bot.get_file(file_id), timeout=SEND_TIMEOUT + 2)
    if not tg_file.file_path:
        raise RuntimeError("Telegram не вернул путь к файлу.")
    buffer = BytesIO()
    await asyncio.wait_for(
        bot.download_file(tg_file.file_path, destination=buffer, timeout=int(FILE_TIMEOUT)),
        timeout=FILE_TIMEOUT + 3,
    )
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Рассылка всем пользователям — фоновая задача с прогрессом (один запуск за раз).
# ---------------------------------------------------------------------------

broadcast_state: dict[str, Any] = {"running": False}


def start_broadcast(config, text: str, admin_telegram_id: int, recipients: list[int]) -> dict[str, Any]:
    from datetime import datetime, timezone

    if broadcast_state.get("running"):
        raise RuntimeError("Рассылка уже идёт. Дождитесь её завершения.")
    if telegram_disabled():
        raise RuntimeError("Отправка в Telegram отключена на этом сервере.")
    state = {
        "running": True,
        "total": len(recipients),
        "sent": 0,
        "blocked": 0,
        "failed": 0,
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "finished_at": None,
        "error": None,
    }
    broadcast_state.clear()
    broadcast_state.update(state)

    async def job() -> None:
        from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

        try:
            for chat_id in recipients:
                for attempt in range(2):
                    try:
                        await send_message(config, chat_id, text)
                        broadcast_state["sent"] += 1
                        break
                    except TelegramForbiddenError:
                        broadcast_state["blocked"] += 1
                        break
                    except TelegramRetryAfter as exc:
                        if attempt == 0:
                            await asyncio.sleep(float(exc.retry_after) + 0.5)
                            continue
                        broadcast_state["failed"] += 1
                    except Exception:  # noqa: BLE001
                        broadcast_state["failed"] += 1
                        break
                await asyncio.sleep(0.05)  # ~20 сообщений/с, ниже лимита Telegram
        except Exception as exc:  # noqa: BLE001
            broadcast_state["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            broadcast_state["running"] = False
            broadcast_state["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            try:
                from database.engine import session_scope
                from database.models import BroadcastLog

                async with session_scope() as session:
                    session.add(BroadcastLog(
                        admin_user_id=int(admin_telegram_id),
                        content_type="текст (Mini App)",
                        total=int(broadcast_state["total"]),
                        sent=int(broadcast_state["sent"]),
                        blocked=int(broadcast_state["blocked"]),
                        failed=int(broadcast_state["failed"]),
                    ))
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[NOTIFY] Broadcast log was not saved: %s", exc)

    task = fire(job, label="broadcast")
    if task is None:
        broadcast_state["running"] = False
        raise RuntimeError("Не удалось запустить рассылку.")
    return dict(broadcast_state)
