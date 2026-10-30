"""Тесты PlayerVerificationService с мок-провайдерами.

Живые Dotabuff/STRATZ не используются: только моки.
Ключевые требования ТЗ:
  * недоступность провайдеров НЕ отклоняет игрока -> PENDING;
  * ошибки API не попадают в кэш как успех;
  * успешная проверка кэшируется и читается из кэша.
"""
from __future__ import annotations

from tests.conftest import FakeDotabuff, FakeSteam, FakeStratz, NoopDotaClient
from services.cache import CacheEntry, ProfileCache
from services.player_verification import PlayerVerificationService
from services.providers_base import PlayerStats, ProviderUnavailable


def _stats(**kw):
    return PlayerStats(**kw)


SID = "76561199161987264"
AID = 1201721536


async def test_verified_when_steam_and_dotabuff_ok(make_verification):
    svc = make_verification(
        steam=FakeSteam(result=_stats(nickname="Cap", profile_url="https://steamcommunity.com/profiles/x")),
        dotabuff=FakeDotabuff(result=_stats(mmr=6500, matches=1234, winrate=57.3, nickname="Cap")),
    )
    r = await svc.verify_ids(SID, AID)
    assert r.status == "VERIFIED"
    assert r.mmr == 6500
    assert r.matches == 1234
    assert r.winrate == 57.3
    assert r.source == "dotabuff"


async def test_pending_when_all_providers_down(make_verification):
    """Steam unavailable + Dotabuff 403 + STRATZ 403 => PENDING, не REJECTED."""
    svc = make_verification(
        steam=FakeSteam(error=ProviderUnavailable("steam", "timeout")),
        dotabuff=FakeDotabuff(error=ProviderUnavailable("dotabuff", "HTTP 403 challenge")),
        stratz=FakeStratz(error=ProviderUnavailable("stratz", "HTTP 403")),
    )
    r = await svc.verify_ids(SID, AID)
    assert r.status == "PENDING"
    assert r.error is None
    assert r.is_valid_identity


async def test_dotabuff_403_does_not_crash(make_verification):
    svc = make_verification(
        steam=FakeSteam(result=_stats(nickname="N")),
        dotabuff=FakeDotabuff(error=ProviderUnavailable("dotabuff", "403 Cf-Mitigated: challenge")),
    )
    r = await svc.verify_ids(SID, AID)
    assert r.status in ("PENDING", "VERIFIED")


async def test_stratz_403_does_not_crash(make_verification):
    svc = make_verification(stratz=FakeStratz(error=ProviderUnavailable("stratz", "403")))
    r = await svc.verify_ids(SID, AID)
    assert r.status != "REJECTED"


async def test_no_fake_stats_when_unavailable(make_verification):
    """Если статистика недоступна — поля остаются None (не 0 и не выдумка)."""
    svc = make_verification(
        steam=FakeSteam(error=ProviderUnavailable("steam", "down")),
        dotabuff=FakeDotabuff(error=ProviderUnavailable("dotabuff", "403")),
        stratz=FakeStratz(error=ProviderUnavailable("stratz", "5xx")),
    )
    r = await svc.verify_ids(SID, AID)
    assert r.status == "PENDING"
    assert r.mmr is None and r.matches is None and r.winrate is None


async def test_invalid_id_input(make_verification):
    r = await make_verification().verify_input("abc-not-an-id")
    assert r.status == "INVALID_ID"
    assert r.error


async def test_steam_profile_not_found_is_invalid(make_verification):
    svc = make_verification(steam=FakeSteam(error=LookupError("not found")))
    r = await svc.verify_ids(SID, AID)
    assert r.status == "INVALID_ID"


async def test_cache_hit_returns_verified_without_providers():
    cache = ProfileCache(path=":memory:", ttl=3600)
    cache.put(CacheEntry(
        steam_id64=SID, account_id=AID, nickname="Cached", mmr=5000,
        matches=100, winrate=55.0, profile_url=None, source="dotabuff", verified=True,
    ))

    class ExplodingSteam:
        name = "steam"

        async def get_player_by_id64(self, steam_id64):
            raise AssertionError("providers must not be called on cache hit")

        async def close(self):
            pass

    svc = PlayerVerificationService(
        steam=ExplodingSteam(), cache=cache, dota_client=NoopDotaClient()
    )
    r = await svc.verify_ids(SID, AID)
    assert r.status == "VERIFIED"
    assert r.from_cache and r.nickname == "Cached"


async def test_errors_are_not_cached_as_success(make_verification):
    cache = ProfileCache(path=":memory:", ttl=3600)
    svc = PlayerVerificationService(
        steam=FakeSteam(error=ProviderUnavailable("steam", "down")),
        dotabuff=FakeDotabuff(error=ProviderUnavailable("dotabuff", "403")),
        stratz=FakeStratz(error=ProviderUnavailable("stratz", "5xx")),
        cache=cache,
    )
    r = await svc.verify_ids(SID, AID)
    assert r.status == "PENDING"
    entry = cache.get(steam_id64=SID)
    assert entry is None or not entry.verified


async def test_verified_result_gets_cached(make_verification):
    cache = ProfileCache(path=":memory:", ttl=3600)
    svc = PlayerVerificationService(
        steam=FakeSteam(result=_stats(nickname="Cap")),
        dotabuff=FakeDotabuff(result=_stats(mmr=4321)),
        stratz=FakeStratz(error=ProviderUnavailable("stratz", "off")),
        cache=cache,
    )
    r = await svc.verify_ids(SID, AID)
    assert r.status == "VERIFIED"
    entry = cache.get(steam_id64=SID)
    assert entry is not None and entry.verified and entry.mmr == 4321


async def test_missing_api_keys_do_not_break(config):
    """Без STEAM_API_KEY и STRATZ_API_TOKEN сервис создаётся и не падает."""
    from services.dotabuff import DotabuffProvider
    from services.steam import SteamProvider
    from services.stratz import StratzProvider

    steam = SteamProvider(api_key="", timeout=2.0)
    db = DotabuffProvider(timeout=2.0)
    st = StratzProvider(api_token="", url="https://api.stratz.com/graphql", timeout=2.0)
    svc = PlayerVerificationService(
        steam=steam, dotabuff=db, stratz=st, cache=ProfileCache(path=":memory:", ttl=60)
    )
    try:
        r = await svc.verify_input(SID)
        # сеть может отвечать что угодно — статус обязан быть валидным значением
        assert r.status in ("VERIFIED", "PENDING", "INVALID_ID")
    finally:
        await svc.close()


# ---------------- DotaSteamClientProvider (официальные данные Valve) ----------------

from tests.conftest import FakeDotaClient  # noqa: E402


async def test_verified_via_official_steam_data_when_third_parties_down(make_verification):
    """Dotabuff 403 + STRATZ 403, но Steam API отдал профиль и часы Dota => VERIFIED."""
    svc = make_verification(
        steam=FakeSteam(result=_stats(nickname="Spiry", profile_url="https://steamcommunity.com/profiles/" + SID)),
        dota_client=FakeDotaClient(result=_stats(
            nickname="Spiry",
            profile_url="https://steamcommunity.com/profiles/" + SID,
            raw={"dota_hours_total": 1234},
        )),
    )
    res = await svc.verify_ids(SID, AID)
    assert res.status == "VERIFIED"
    assert res.source in ("steam_dota", "dotabuff", "stratz", "steam")
    assert res.nickname == "Spiry"
    # MMR честно отсутствует — никаких фейков.
    assert res.mmr is None


async def test_pending_when_steam_ok_but_no_dota_data_anywhere(make_verification):
    """Личность видна, но ни один источник данных не ответил => PENDING (не reject)."""
    from services.providers_base import ProviderUnavailable
    svc = make_verification(
        steam=FakeSteam(result=_stats(nickname="Ghost")),
        dota_client=FakeDotaClient(error=ProviderUnavailable("steam_dota", "HTTP 500")),
    )
    res = await svc.verify_ids(SID, AID)
    assert res.status == "PENDING"


async def test_pending_when_steam_unavailable_even_if_dota_client_has_cache(make_verification):
    from services.providers_base import ProviderUnavailable
    svc = make_verification(
        steam=FakeSteam(error=ProviderUnavailable("steam", "network")),
        dota_client=FakeDotaClient(error=ProviderUnavailable("steam_dota", "network")),
    )
    res = await svc.verify_ids(SID, AID)
    assert res.status == "PENDING"
    assert res.error is None


async def test_invalid_steam_profile_not_found(make_verification):
    svc = make_verification(steam=FakeSteam(error=LookupError("nope")))
    res = await svc.verify_ids(SID, AID)
    assert res.status == "INVALID_ID"


async def test_dota_client_lookup_error_does_not_crash(make_verification):
    svc = make_verification(
        steam=FakeSteam(result=_stats(nickname="X")),
        dota_client=FakeDotaClient(error=LookupError("not found")),
    )
    res = await svc.verify_ids(SID, AID)
    # steam ок, dota_client не найден, третьесторонние легли => PENDING без падения
    assert res.status == "PENDING"


async def test_dota_steam_client_provider_without_key_raises_unavailable():
    from services.dota_steamclient import DotaSteamClientProvider
    from services.providers_base import ProviderUnavailable
    p = DotaSteamClientProvider(api_key="")
    try:
        await p.get_player_by_id64(SID)
    except ProviderUnavailable as e:
        assert "STEAM_API_KEY" in e.reason
    else:
        raise AssertionError("expected ProviderUnavailable")
