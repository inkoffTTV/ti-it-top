"""Запуск ТОЛЬКО веб-части Mini App (без Telegram-бота) — для тестов и стейджинга.

Бот (polling) здесь НЕ запускается: второй polling с тем же BOT_TOKEN сломал бы
production-бота («Conflict: terminated by other getUpdates request»).

Пример (стейджинг на КОПИИ базы, отдельный порт):

    MINIAPP_V2_TELEGRAM_DISABLED=1 \
    .venv/bin/python -m miniapp_v2.devserver \
        --db /tmp/stage/tournament_copy.db --port 8091

По умолчанию отправка в Telegram отключена (MINIAPP_V2_TELEGRAM_DISABLED=1).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


async def _main(args: argparse.Namespace) -> None:
    os.environ.setdefault("MINIAPP_V2_TELEGRAM_DISABLED", "1")
    from config import GLOBAL_CONFIG, Config, load_config
    from database.engine import create_tables, init_engine, session_scope
    from services.admins import bootstrap_admins
    from services.factory import Services, set_services

    config = load_config()
    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        raise SystemExit(f"База не найдена: {db_path}")
    if args.forbid_path and db_path == Path(args.forbid_path).resolve():
        raise SystemExit("Отказ: devserver нельзя запускать на production-базе. Используйте копию.")
    config.database_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    config.miniapp_host = args.host
    config.miniapp_port = args.port
    if args.bot_token is not None:
        config.bot_token = args.bot_token

    await init_engine(config.database_url)
    await create_tables()
    async with session_scope() as session:
        await bootstrap_admins(session, config)
    for field in Config.__dataclass_fields__:  # type: ignore[attr-defined]
        if field == "admin_ids":
            GLOBAL_CONFIG.admin_ids.clear()
            GLOBAL_CONFIG.admin_ids.update(config.admin_ids)
        else:
            setattr(GLOBAL_CONFIG, field, getattr(config, field))
    set_services(Services(GLOBAL_CONFIG))

    from miniapp.app import run_server

    logging.getLogger("MINIAPP_V2").info("[DEVSERVER] http://%s:%s  db=%s", args.host, args.port, db_path)
    await run_server(GLOBAL_CONFIG)


def main() -> None:
    parser = argparse.ArgumentParser(description="TI:IT TOP Mini App web-only server (no Telegram polling)")
    parser.add_argument("--db", required=True, help="Путь к КОПИИ tournament.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--bot-token", default=None, help="Токен только для проверки подписи initData в тестах")
    parser.add_argument("--forbid-path", default=None, help="Путь production-БД, на которой запуск запрещён")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
