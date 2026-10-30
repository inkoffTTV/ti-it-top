"""STRATZ: ранг в steamAccount (актуальная схема) и совместимость со старым ответом."""
from __future__ import annotations

import json

from services.stratz import _QUERY, StratzProvider


class _Resp:
    def __init__(self, payload):
        self.status = 200
        self._body = json.dumps(payload)

    async def read_text(self):
        return self._body


def _provider(payload):
    class P(StratzProvider):
        async def _post(self, payload_, headers):
            assert "seasonRank" in payload_["query"]
            return _Resp(payload)

    return P(api_token="x", http=object())


def test_query_asks_rank_inside_steam_account():
    inner = _QUERY.split("steamAccount {", 1)[1].split("}", 1)[0]
    assert "seasonRank" in inner and "seasonLeaderboardRank" in inner
    assert "matchCount" in _QUERY and "winCount" in _QUERY


async def test_parses_new_schema():
    p = _provider({"data": {"player": {
        "steamAccount": {"id": 1, "name": "inkoff", "seasonRank": 41, "seasonLeaderboardRank": None, "smurfFlag": 0, "isAnonymous": False},
        "matchCount": 1797, "winCount": 906}}})
    st = await p.get_player(1)
    assert st.raw["rank_tier"] == 41 and st.matches == 1797 and st.winrate == 50.4
    assert st.raw["smurf_flag"] == 0 and st.nickname == "inkoff"


async def test_parses_old_schema_still():
    p = _provider({"data": {"player": {"steamAccount": {"id": 1, "name": "a"}, "seasonRank": 55, "seasonLeaderboardRank": 900}}})
    st = await p.get_player(1)
    assert st.raw["rank_tier"] == 55 and st.raw["leaderboard_rank"] == 900
