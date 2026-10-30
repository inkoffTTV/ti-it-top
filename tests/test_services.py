"""Тесты сервисов команд, турниров и заявок (без Telegram-слоя)."""
from __future__ import annotations

import pytest

from database.engine import session_scope
from database.models import ApplicationStatus, TournamentStatus, VerificationStatus
from database.repositories import UserRepository
from services.applications import ApplicationService
from services.teams import TeamService
from services.tournaments import TournamentService


async def _mk_user(s, tg_id: int, username: str):
    return await UserRepository(s).upsert_from_telegram(tg_id, username, username.title(), None)


async def _team_with_roster(s, n_players: int = 4):
    """Капитан + n_players игроков (всего состав = n_players + 1)."""
    svc = TeamService(s)
    cap = await _mk_user(s, 5000, "captain")
    team = await svc.create_team(cap.id, "Test Squad")
    users = []
    for i in range(n_players):
        u = await _mk_user(s, 5100 + i, f"player{i}")
        await s.commit()
        await svc.add_player(team.id, cap.id, f"@player{i}")
        await s.commit()
        users.append(u)
    return svc, cap, team, users


# ---------------- teams ----------------

async def test_team_creation_and_captain_membership(db):
    async with session_scope() as s:
        svc = TeamService(s)
        cap = await _mk_user(s, 6000, "cap1")
        team = await svc.create_team(cap.id, "  Alpha   Wolves ")
        assert team.name == "Alpha Wolves"  # нормализация пробелов
        m = await svc.teams.get_member(team.id, cap.id)
        assert m is not None and m.role.value == "CAPTAIN"


async def test_duplicate_team_name_rejected(db):
    async with session_scope() as s:
        svc = TeamService(s)
        c1 = await _mk_user(s, 6001, "c1")
        c2 = await _mk_user(s, 6002, "c2")
        await svc.create_team(c1.id, "Dup")
        with pytest.raises(ValueError):
            await svc.create_team(c2.id, "Dup")


async def test_empty_name_rejected(db):
    async with session_scope() as s:
        svc = TeamService(s)
        c = await _mk_user(s, 6003, "c3")
        with pytest.raises(ValueError):
            await svc.create_team(c.id, "     ")


async def test_duplicate_member_rejected(db):
    async with session_scope() as s:
        svc = TeamService(s)
        cap = await _mk_user(s, 6004, "cap4")
        await _mk_user(s, 6005, "p5")
        team = await svc.create_team(cap.id, "NoDup")
        await svc.add_player(team.id, cap.id, "@p5")
        with pytest.raises(ValueError):
            await svc.add_player(team.id, cap.id, "@p5")


async def test_non_captain_cannot_manage(db):
    async with session_scope() as s:
        svc = TeamService(s)
        cap = await _mk_user(s, 6006, "cap6")
        rival = await _mk_user(s, 6007, "rival")
        team = await svc.create_team(cap.id, "OnlyCap")
        with pytest.raises(PermissionError):
            await svc.add_player(team.id, rival.id, "@rival")
        with pytest.raises(PermissionError):
            await svc.remove_player(team.id, rival.id, "@rival")


async def test_add_unknown_player_message(db):
    async with session_scope() as s:
        svc = TeamService(s)
        cap = await _mk_user(s, 6008, "cap8")
        team = await svc.create_team(cap.id, "Ghost")
        with pytest.raises(ValueError, match="не найден"):
            await svc.add_player(team.id, cap.id, "@nosuchuser")


async def test_remove_member(db):
    async with session_scope() as s:
        svc, cap, team, users = await _team_with_roster(s, n_players=1)
        ok = await svc.remove_player(team.id, cap.id, "@player0")
        assert ok
        roster = await svc.teams.get_roster(team.id)
        assert len(roster) == 1  # только капитан


async def test_cannot_remove_captain(db):
    async with session_scope() as s:
        svc, cap, team, _ = await _team_with_roster(s, n_players=1)
        with pytest.raises(ValueError):
            await svc.remove_player(team.id, cap.id, "@captain")


async def test_player_cannot_join_two_teams(db):
    async with session_scope() as s:
        svc = TeamService(s)
        c1 = await _mk_user(s, 6010, "cc1")
        c2 = await _mk_user(s, 6011, "cc2")
        p = await _mk_user(s, 6012, "shared")
        t1 = await svc.create_team(c1.id, "TeamOne")
        t2 = await svc.create_team(c2.id, "TeamTwo")
        await svc.add_player(t1.id, c1.id, "@shared")
        with pytest.raises(ValueError, match="состоит в (другой )?команде"):
            await svc.add_player(t2.id, c2.id, "@shared")


# ---------------- tournaments ----------------

async def test_tournament_creation_and_public_list(db):
    async with session_scope() as s:
        svc = TournamentService(s)
        t = await svc.create("TI:IT TOP Spring", "Куп", 16, None, open_registration=True)
        assert t.status == TournamentStatus.REGISTRATION
        public = await svc.public_list()
        assert any(x.id == t.id for x in public)


async def test_tournament_validation(db):
    async with session_scope() as s:
        svc = TournamentService(s)
        with pytest.raises(ValueError):
            await svc.create("", None, 8, None)
        with pytest.raises(ValueError):
            await svc.create("OK", None, 1, None)  # max_teams < 2


async def test_tournament_status_flow(db):
    async with session_scope() as s:
        svc = TournamentService(s)
        t = await svc.create("Flow Cup", None, 4, None, open_registration=False)
        assert t.status == TournamentStatus.DRAFT
        t = await svc.set_status(t.id, TournamentStatus.REGISTRATION)
        assert t.registration_open is True
        t = await svc.set_status(t.id, TournamentStatus.ACTIVE)
        assert t.registration_open is False
        ok, err = TournamentService.can_register(t)
        assert not ok


# ---------------- applications ----------------

async def test_application_submit_and_no_duplicates(db):
    async with session_scope() as s:
        tsvc = TournamentService(s)
        tour = await tsvc.create("App Cup", None, 8, None, open_registration=True)
        svc, cap, team, _ = await _team_with_roster(s, n_players=4)  # состав 5
        asvc = ApplicationService(s)
        app = await asvc.submit(cap.id, tour.id)
        assert app.status == ApplicationStatus.PENDING
        with pytest.raises(ValueError, match="уже подавала"):
            await asvc.submit(cap.id, tour.id)


async def test_application_requires_five_players(db):
    async with session_scope() as s:
        tsvc = TournamentService(s)
        tour = await tsvc.create("Small Cup", None, 8, None, open_registration=True)
        svc, cap, team, _ = await _team_with_roster(s, n_players=2)  # состав 3
        asvc = ApplicationService(s)
        ok, msg = await asvc.validate_registration(cap.id, tour.id)
        assert not ok and "минимум 5" in msg


async def test_application_requires_captain(db):
    async with session_scope() as s:
        tsvc = TournamentService(s)
        tour = await tsvc.create("Cap Cup", None, 8, None, open_registration=True)
        async with session_scope() as s2:
            pass
        loner = await _mk_user(s, 6099, "loner")
        asvc = ApplicationService(s)
        ok, msg = await asvc.validate_registration(loner.id, tour.id)
        assert not ok and "капитан" in msg.lower()


async def test_application_rejected_when_registration_closed(db):
    async with session_scope() as s:
        tsvc = TournamentService(s)
        tour = await tsvc.create("Closed Cup", None, 8, None, open_registration=False)
        svc, cap, team, _ = await _team_with_roster(s, n_players=4)
        asvc = ApplicationService(s)
        ok, msg = await asvc.validate_registration(cap.id, tour.id)
        assert not ok


async def test_rejected_player_blocks_application(db):
    from database.repositories import ProfileRepository

    async with session_scope() as s:
        tsvc = TournamentService(s)
        tour = await tsvc.create("Verify Cup", None, 8, None, open_registration=True)
        svc, cap, team, users = await _team_with_roster(s, n_players=4)
        pr = ProfileRepository(s)
        p = await pr.get_or_create(users[0].id)
        await pr.set_status(p, VerificationStatus.REJECTED)
        await s.commit()
        asvc = ApplicationService(s)
        ok, msg = await asvc.validate_registration(cap.id, tour.id)
        assert not ok and "отклонён" in msg


async def test_decision_updates_application(db):
    async with session_scope() as s:
        tsvc = TournamentService(s)
        tour = await tsvc.create("Decide Cup", None, 8, None, open_registration=True)
        svc, cap, team, _ = await _team_with_roster(s, n_players=4)
        asvc = ApplicationService(s)
        app = await asvc.submit(cap.id, tour.id)
        from database.repositories import ApplicationRepository

        ar = ApplicationRepository(s)
        app = await ar.decide(app, ApplicationStatus.APPROVED, 100, "Удачи!")
        assert app.status == ApplicationStatus.APPROVED
        assert app.admin_comment == "Удачи!"


# ---------- Парсеры статистики: Dotabuff SSR / legacy, STRATZ matchCounts, оценка из часов Valve ----------

def test_dotabuff_parses_next_data_ssr():
    from services.dotabuff import _parse_html, _looks_like_player_page
    html = '''<html><body class="player-profile">
    <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{
      "profile":{"name":"Topson","leaderboardRating":6432,"rankTierName":"Immortal"},
      "summary":{"matchesPlayed":2154,"winRate":58.7}}}}</script></body></html>'''
    st = _parse_html(html)
    assert st.mmr == 6432
    assert st.matches == 2154
    assert abs(st.winrate - 58.7) < 0.01
    assert _looks_like_player_page(html)



def test_dotabuff_parses_rank_name():
    from services.dotabuff import _parse_html
    html = '''<html><body class="player-profile">
    <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{
      "profile":{"name":"inkoff","leaderboardRating":5000,"rankTierName":"Divine 5"}}}}</script>
    </body></html>'''
    st = _parse_html(html)
    assert st.rank_name == "Божество 5"


def test_opendota_rank_tier_to_name():
    from services.dotabuff import _rank_tier_to_name
    assert _rank_tier_to_name(75) == "Божество 5"
    assert _rank_tier_to_name(80) == "Титан"

def test_dotabuff_legacy_css_fallback():
    from services.dotabuff import _parse_html
    html = ('<html><body><span class="rankings-rating__value">4213</span>'
            '<p>1337 Matches</p>Win Rate: 52.3%</body></html>')
    st = _parse_html(html)
    assert st.mmr == 4213 and st.matches == 1337 and abs(st.winrate - 52.3) < 0.01


def test_dotabuff_cloudflare_page_yields_nothing():
    from services.dotabuff import _parse_html
    cf = ('<html><head><title>Just a moment...</title>'
          '<script src="https://challenges.cloudflare.com"></script></head><body></body></html>')
    st = _parse_html(cf)
    assert st.mmr is None and st.matches is None and st.winrate is None


def test_dotabuff_fractional_winrate_normalized():
    from services.dotabuff import _parse_html
    st = _parse_html('<html><body class="player-profile"><script>{"winRate": 0.55, "rating": 3100}</script></body></html>')
    assert st.mmr == 3100 and abs(st.winrate - 55.0) < 0.01


def test_stratz_match_counts_helpers():
    from services.stratz import _count_matches, _count_wins
    mc = [{"name": "ranked", "counts": [{"resultType": "WIN", "count": 100},
                                        {"resultType": "LOSS", "count": 100}]},
          {"name": "pub", "counts": [{"resultType": "WIN", "count": 50}]}]
    assert _count_matches(mc) == 250
    assert _count_wins(mc) == 150
    assert _count_matches(None) is None
    assert _count_wins("garbage") is None


async def test_no_fake_match_count_from_valve_hours_when_providers_down():
    """Часы Valve не превращаются в выдуманное число матчей."""
    from unittest.mock import AsyncMock
    from services.player_verification import PlayerVerificationService
    from services.providers_base import ProviderUnavailable

    steam = AsyncMock()
    steam.get_player_by_id64 = AsyncMock(
        return_value=type("S", (), {"nickname": "inkoff", "profile_url": "u"})()
    )
    dota = AsyncMock()
    dota.get_player_by_id64 = AsyncMock(
        return_value=type("D", (), {"nickname": None, "profile_url": None,
                                    "raw": {"dota_hours_total": 500}})()
    )
    db = AsyncMock()
    db.get_player = AsyncMock(side_effect=ProviderUnavailable("dotabuff", "HTTP 403 (challenge)"))
    stt = AsyncMock()
    stt.get_player = AsyncMock(side_effect=ProviderUnavailable("stratz", "HTTP 403"))
    svc = PlayerVerificationService(steam=steam, dotabuff=db, stratz=stt, cache=None, dota_client=dota)
    res = await svc.verify_ids("76561199161987264", 1201721536)
    assert res.status == "VERIFIED"
    assert res.hours_total == 500
    assert res.matches is None
    assert res.mmr is None and res.winrate is None
