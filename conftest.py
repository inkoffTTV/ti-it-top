"""Корневой conftest: изоляция конфигурации для тестов."""
import os

os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("ADMIN_IDS", "100,200")
os.environ.pop("STEAM_API_KEY", None)
os.environ.pop("STRATZ_API_TOKEN", None)
