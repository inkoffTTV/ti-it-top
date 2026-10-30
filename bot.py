"""TI:IT TOP — турнирный Telegram-бот (aiogram 3).

Точка входа: python bot.py
Все секреты — из окружения/.env. Бот работает без STEAM_API_KEY,
STRATZ_API_TOKEN и Google Sheets (fallback на ручную проверку админом).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.fsm.storage.memory import MemoryStorage

from config import Config, GLOBAL_CONFIG, load_config
from database.engine import create_tables, dispose_engine, init_engine, session_scope
from handlers import register_routers
from services.factory import Services, set_services
from utils.logging_setup import configure_logging, install_secret_redaction


async def main() -> None:
    config = load_config()
    configure_logging()
    if not config.is_valid:
        raise SystemExit(
            "BOT_TOKEN не задан. Скопируйте .env.example в .env и укажите токен бота."
        )

    # Секреты не должны попадать в логи ни под каким видом.
    install_secret_redaction([config.bot_token, config.steam_api_key, config.stratz_api_token])

    await init_engine(config.database_url)
    await create_tables()
    # Загружаем постоянный список админов из БД; ADMIN_IDS остаётся bootstrap.
    from services.admins import bootstrap_admins
    async with session_scope() as session:
        await bootstrap_admins(session, config)
    # Все части процесса (бот, handlers и Mini App) должны работать с одним объектом Config.
    # Иначе добавленный из одного интерфейса админ не увидит изменения в другом до рестарта.
    for _field in Config.__dataclass_fields__:  # type: ignore[attr-defined]
        if _field == "admin_ids":
            GLOBAL_CONFIG.admin_ids.clear()
            GLOBAL_CONFIG.admin_ids.update(config.admin_ids)
        else:
            setattr(GLOBAL_CONFIG, _field, getattr(config, _field))
    config = GLOBAL_CONFIG

    services = Services(config)
    set_services(services)

    # Telegram Bot API использует тот же HTTP proxy, что и внешние сервисы.
    # Это особенно важно для запуска из РФ, где прямой доступ к Bot API
    # может быть недоступен, хотя локальный VPN/прокси уже работает.
    bot_session = AiohttpSession(proxy=(config.http_proxy_url or "").strip() or None)
    bot = Bot(config.bot_token, session=bot_session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    register_routers(dp)
    # 5.7: заблокированные пользователи не могут пользоваться ботом.
    from handlers.ban_middleware import BanMiddleware

    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())

    miniapp_task = None
    logger = logging.getLogger("BOT")
    if config.miniapp_url:
        from miniapp.app import run_server
        # Web-сервер Mini App запускается независимо от доступности Bot API.
        miniapp_task = asyncio.create_task(run_server(config), name="tiit-miniapp")
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="TI:IT TOP",
                    web_app=WebAppInfo(url=(config.miniapp_url + ("&" if "?" in config.miniapp_url else "?") + "v=5.15.0")),
                )
            )
            logger.info("[MINIAPP] Menu button configured: %s", config.miniapp_url)
        except Exception as exc:
            logger.warning("[MINIAPP] Menu button was not configured: %s", exc)

    # 5.2: напоминания о матчах турнира (за 30 минут, обеим командам).
    from services.tournament_engine.reminders import reminder_loop

    async def _send_reminder(chat_id: int, text: str, reply_markup=None) -> None:
        await bot.send_message(int(chat_id), text, disable_web_page_preview=True, reply_markup=reply_markup)

    reminder_task = asyncio.create_task(reminder_loop(_send_reminder), name="tiit-match-reminders")
    # 5.6: раз в сутки фоном проверяем новых и давно не проверенных игроков (AI — только подозрительным).
    from services.player_checks import sweep_loop

    sweep_task = asyncio.create_task(sweep_loop(config), name="tiit-player-sweep")
    # 5.13: сторож сервиса, утренняя сводка, копия базы владельцу в Telegram.
    import os as _os
    from pathlib import Path as _Path

    from services import watchdog

    _db = config.database_url.split(":///", 1)[1] if config.database_url.startswith("sqlite") else None
    _base = _Path(_db).resolve().parent.parent if _db else _Path.cwd()
    state_dir = _base / "RUNTIME_STATE"
    try:
        owner_id = int(_os.getenv("OWNER_TELEGRAM_ID", "0") or 0)
    except ValueError:
        owner_id = 0

    async def _to_owner(text: str) -> None:
        for chat in ([owner_id] if owner_id else sorted(config.admin_ids)):
            try:
                await bot.send_message(int(chat), text, disable_web_page_preview=True)
            except Exception as exc:  # noqa: BLE001
                logger.info("[WATCHDOG] owner message to %s failed: %s", chat, exc)

    async def _to_admins(text: str) -> None:
        for chat in sorted(config.admin_ids):
            try:
                await bot.send_message(int(chat), text, disable_web_page_preview=True)
            except Exception as exc:  # noqa: BLE001
                logger.info("[WATCHDOG] admin message to %s failed: %s", chat, exc)

    async def _backup_offsite(path) -> None:
        if not owner_id or not watchdog.backup_send_due(state_dir):
            return
        from aiogram.types import FSInputFile

        packed = await asyncio.to_thread(watchdog.pack_backup, _Path(path), state_dir / "offsite")
        try:
            await bot.send_document(owner_id, FSInputFile(str(packed)),
                                    caption=f"Копия базы TI:IT TOP — {packed.name}. Храните в надёжном месте: тут все данные бота.")
            watchdog.mark_backup_sent(state_dir)
        finally:
            packed.unlink(missing_ok=True)

    async def _morning_sweep():
        from services.player_checks import tournament_day_sweep

        await tournament_day_sweep(config)

    watchdog.install()
    ops_task = asyncio.create_task(watchdog.ops_loop(_to_owner, _to_admins, state_dir=state_dir, disk_path=_base,
                                                     on_morning=_morning_sweep), name="tiit-watchdog")
    try:
        from miniapp_v2 import VERSION as _version
    except Exception:  # noqa: BLE001
        _version = "?"
    asyncio.create_task(_to_owner(watchdog.startup_text(state_dir, _version)), name="tiit-startup-note")

    # 5.8: ежедневная копия базы (sqlite backup API), хранятся последние 14.
    backup_task = None
    if _db:
        from services.tournament_extras import backup_loop

        backup_task = asyncio.create_task(
            backup_loop(_db, _base / "DAILY_BACKUPS", on_done=_backup_offsite), name="tiit-daily-backup")

    logger.info("[BOT] TI:IT TOP started; admins=%s", sorted(config.admin_ids))
    clean_exit = False
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        clean_exit = True  # polling завершился штатно (SIGTERM от systemd при деплое/перезапуске)
    finally:
        if clean_exit:
            watchdog.mark_clean_shutdown(state_dir)
        for task in (miniapp_task, reminder_task, sweep_task, backup_task, ops_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await services.close()
        await bot.session.close()
        await dispose_engine()
        logger.info("[BOT] Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code:
            raise
        print("Bye")
