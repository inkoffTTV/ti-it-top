"""5.2: реальная причина 403 Dotabuff (неподдерживаемый отпечаток) и цепочка источников."""
from __future__ import annotations

import services.http_client as http_client
from services.providers_base import PlayerStats, ProviderUnavailable
from services.dotabuff import DotabuffProvider


def test_dotabuff_rank_badge_parsed_not_ranked_word():
    from services.dotabuff import dotabuff_rank_tier, parse_dotabuff

    html = ('<html><head><title>inkoff - Overview - DOTABUFF</title></head><body>'
            '<div class="rank-tier-wrapper" rel="tooltip" title="Rank: Archon I"></div>'
            '<div>Ranked MM 311 47.59%</div></body></html>')
    parsed = parse_dotabuff(html, 1)
    assert parsed["rank_tier"] == 41 and parsed["rank_name"] == "Герой 1"
    assert dotabuff_rank_tier('title="Rank: Guardian V" class="x"') is None  # нет обёртки значка
    assert dotabuff_rank_tier('<div class="rank-tier-wrapper" title="Rank: Immortal">') == 80
    # Без значка — никакого «ed» из слова «Ranked».
    assert parse_dotabuff("<html><body>Ranked MM 10 50%</body></html>", 1)["rank_name"] is None


def test_unsupported_impersonate_target_falls_back(monkeypatch):
    monkeypatch.setattr(http_client, "_supported_impersonate_targets", lambda: {"chrome131", "safari17_0"})
    assert http_client.resolve_impersonate("chrome126") == "chrome131"
    assert http_client.resolve_impersonate("safari17_0") == "safari17_0"
    # Если версия curl_cffi не даёт список — значение не трогаем.
    monkeypatch.setattr(http_client, "_supported_impersonate_targets", lambda: None)
    assert http_client.resolve_impersonate("chrome126") == "chrome126"


async def test_breaker_skips_dotabuff_and_cache_returns_same_result():
    calls = {"db": 0, "od": 0}

    class Provider(DotabuffProvider):
        async def _fetch_dotabuff(self, account_id):
            calls["db"] += 1
            raise ProviderUnavailable("dotabuff", "HTTP 403")

        async def _fetch_opendota(self, account_id):
            calls["od"] += 1
            return PlayerStats(source="opendota", account_id=account_id, matches=100)

    p = Provider()
    for acc in (1, 2, 3):
        assert (await p.get_player(acc)).source == "opendota"
    assert calls["db"] == 3
    await p.get_player(4)  # предохранитель открыт: Dotabuff не вызывается
    assert calls["db"] == 3 and calls["od"] == 4
    await p.get_player(4)  # кеш
    assert calls["od"] == 4
