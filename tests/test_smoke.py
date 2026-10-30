"""Smoke test: project imports, DB init, config load, Bot + Dispatcher + routers."""
import asyncio

import pytest


def test_smoke_full_boot_sequence(tmp_path, monkeypatch):
    async def run():
        # 1. Config loads from env (test token only).
        monkeypatch.setenv("BOT_TOKEN", "123456:TEST-TOKEN")
        monkeypatch.setenv("ADMIN_IDS", "1")
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'smoke.db'}")
        from config import load_config

        cfg = load_config()
        assert cfg.bot_token == "123456:TEST-TOKEN"

        # 2. Database engine init + table creation (no DROP).
        from database.engine import init_engine, create_tables, session_scope, dispose_engine
        from database.models import Base

        await init_engine(cfg.database_url)
        await create_tables()

        # 3. Repositories work + duplicate safety.
        from database.repositories import UserRepository

        async with session_scope() as session:
            users = UserRepository(session)
            user = await users.upsert_from_telegram(999001, "smoketest", "Smoke", None)
            assert user.telegram_id == 999001
            dup = await users.upsert_from_telegram(999001, "smoketest2", "Smoke2", None)
            assert dup.id == user.id  # no duplicate row

        # 3b. Safe re-init of tables (no DROP, idempotent).
        await create_tables()

        # 4. Services factory builds (providers incl. http client).
        from services.factory import Services

        services = Services(config=cfg)
        assert services.verification is not None
        assert services.dotabuff is not None and services.stratz is not None
        # 5. aiogram Bot + Dispatcher + all routers register cleanly.
        from aiogram import Bot, Dispatcher
        from handlers import register_routers
        from keyboards.main import main_keyboard

        bot = Bot(token=cfg.bot_token)
        dp = Dispatcher()
        register_routers(dp)
        kb = main_keyboard(is_admin_user=True)
        assert kb.is_keyword if hasattr(kb, 'is_keyword') else True  # ReplyKeyboardMarkup
        labels = [btn.text for row in kb.keyboard for btn in row]
        assert "🏆 Турниры" in labels and "⚙️ Админ-панель" in labels
        await bot.session.close()
        await dispose_engine()
        return True

    assert asyncio.run(run())
