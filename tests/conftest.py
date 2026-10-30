"""Фикстуры тестов: временная БД и фабрика мок-провайдеров."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from database.engine import create_tables, dispose_engine, init_engine  # noqa: E402
from services.factory import Services  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture()
async def db(tmp_path):
    """Чистая SQLite-БД в tmp-файле на каждый тест. Без удаления боевой БД."""
    import database.engine as engine_mod

    engine_mod._engine = None
    engine_mod._session_factory = None
    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    await init_engine(url)
    await create_tables()
    import services.bans as _bans

    _bans.invalidate()
    yield
    await dispose_engine()
    engine_mod._engine = None
    engine_mod._session_factory = None


@pytest.fixture()
def config() -> Config:
    return Config(
        bot_token="test:token",
        admin_ids={100, 200},
        database_url="sqlite+aiosqlite:///./test_memory.db",
        steam_api_key="",
        stratz_api_token="",
        profile_cache_ttl=60,
        profile_cache_path=":memory:",
    )


class NoopDotaClient:
    """Заглушка: не дергает сеть, всегда недоступна (как без ключа)."""
    name = "steam_dota"

    async def get_player_by_id64(self, steam_id64):
        from services.providers_base import ProviderUnavailable
        raise ProviderUnavailable("steam_dota", "noop")

    async def close(self):
        pass


class FakeSteam:
    name = "steam"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.api_key = ""

    async def get_player_by_id64(self, steam_id64):
        from services.providers_base import PlayerStats
        if self.error:
            raise self.error
        stats = self.result or PlayerStats(source="steam")
        stats.steam_id64 = steam_id64
        return stats

    async def resolve_vanity(self, vanity):
        return None

    async def close(self):
        pass


class FakeDotaClient:
    name = "steam_dota"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    async def get_player_by_id64(self, steam_id64):
        from services.providers_base import PlayerStats
        if self.error:
            raise self.error
        stats = self.result or PlayerStats(source="steam_dota")
        stats.steam_id64 = steam_id64
        return stats

    async def close(self):
        pass


class FakeDotabuff:
    name = "dotabuff"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    async def get_player(self, account_id):
        if self.error:
            raise self.error
        return self.result

    async def close(self):
        pass


class FakeStratz:
    name = "stratz"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    async def get_player(self, account_id):
        if self.error:
            raise self.error
        return self.result

    async def close(self):
        pass


@pytest.fixture()
def make_verification(config):
    """Фабрика PlayerVerificationService с подменёнными провайдерами."""
    from services.cache import ProfileCache
    from services.player_verification import PlayerVerificationService

    def _make(steam=None, dotabuff=None, stratz=None, cache=True, dota_client=None):
        from services.providers_base import ProviderUnavailable
        return PlayerVerificationService(
            steam=steam or FakeSteam(),
            dotabuff=dotabuff or FakeDotabuff(error=ProviderUnavailable("dotabuff", "mocked off")),
            stratz=stratz or FakeStratz(error=ProviderUnavailable("stratz", "mocked off")),
            cache=ProfileCache(path=":memory:", ttl=60) if cache else None,
            dota_client=dota_client or FakeDotaClient(error=ProviderUnavailable("steam_dota", "mocked off")),
        )

    return _make
