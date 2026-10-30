"""5.1: источники медали, выбор достоверного, риск смурфа/твинка, AI-лимиты, помощник, аватарки."""
from __future__ import annotations

import io
import time

import pytest
from sqlalchemy import select

from database.engine import session_scope
from database.models import PlayerCheck, PlayerProfile, User, VerificationStatus
from services import ai_budget
from services import player_checks as pc
from services.player_checks import Reading, medal_consensus, risk_assessment

NOW = time.time()


def R(source, tier, ok=True, **data):
    return Reading(source, ok, None if ok else "HTTP 403", tier, None, data)


# ---------------------------------------------------------------------------
# Выбор медали
# ---------------------------------------------------------------------------

def test_all_sources_agree_high_confidence():
    c = medal_consensus([R("opendota", 41), R("stratz", 41), R("dotabuff", 41)], NOW)
    assert c["tier"] == 41 and c["confidence"] == "high" and c["source"] == "opendota+stratz+dotabuff"


def test_majority_wins_when_one_differs():
    c = medal_consensus([R("opendota", 41), R("stratz", 52), R("dotabuff", 41)], NOW)
    assert c["tier"] == 41 and c["confidence"] == "medium" and set(c["source"].split("+")) == {"opendota", "dotabuff"}


def test_all_differ_fresh_stratz_preferred():
    c = medal_consensus([R("opendota", 41), R("stratz", 52, last_match_at=NOW - 3 * 86400), R("dotabuff", 43)], NOW)
    assert c["tier"] == 52 and c["source"] == "stratz"


def test_all_differ_stale_stratz_falls_back_to_valve_badge():
    c = medal_consensus([R("opendota", 41), R("stratz", 52, last_match_at=NOW - 200 * 86400), R("dotabuff", 43)], NOW)
    assert c["tier"] == 43 and c["source"] == "dotabuff" and c["confidence"] == "low"


def test_failed_sources_never_chosen_and_no_invention():
    c = medal_consensus([R("opendota", None, ok=False), R("stratz", None, ok=False), R("dotabuff", 33)], NOW)
    assert c["tier"] == 33 and c["source"] == "dotabuff"
    assert medal_consensus([R("opendota", None, ok=False)], NOW)["tier"] is None


# ---------------------------------------------------------------------------
# Риск
# ---------------------------------------------------------------------------

def test_risk_signals_add_up_and_unknown_without_data():
    score, level, reasons = risk_assessment({
        "medal_tier": 61, "total_matches": 120, "total_winrate": 68.0, "steam_created_at": NOW - 60 * 86400,
        "stratz_smurf_flag": 1, "declared_mmr": 2000, "medal_mmr_min": 4600,
    }, NOW)
    assert level == "high" and score == 100 and len(reasons) >= 4
    assert risk_assessment({}, NOW)[1] == "unknown"
    assert risk_assessment({"medal_tier": 31, "total_matches": 2500, "total_winrate": 50.2}, NOW)[1] == "low"


# ---------------------------------------------------------------------------
# Оркестрация на БД
# ---------------------------------------------------------------------------

async def _player(tg: int = 777, *, steam: bool = True) -> int:
    async with session_scope() as s:
        u = User(telegram_id=tg, username=f"p{tg}", is_registered=True)
        s.add(u)
        await s.flush()
        if steam:
            s.add(PlayerProfile(user_id=u.id, steam_id64=str(76561198000000000 + tg), account_id=tg,
                                verification_status=VerificationStatus.PENDING, exact_mmr=3000))
        await s.commit()
        return int(u.id)


@pytest.fixture()
def budget_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_budget, "PATH", tmp_path / "ai_usage.json")
    return tmp_path / "ai_usage.json"


async def test_run_check_saves_row_updates_profile_public_view_hides_risk(db, monkeypatch, budget_file):
    uid = await _player()

    async def fake_collect(config, account_id, steam_id64):
        return [R("opendota", 41), R("stratz", 41, match_count=900, win_count=450), R("dotabuff", 41),
                Reading("steam", True, data={"steam_created_at": int(NOW - 5 * 365 * 86400), "steam_level": 20})]

    monkeypatch.setattr(pc, "collect", fake_collect)
    from config import Config

    out = await pc.run_player_check(Config(), uid, trigger="test", use_ai=False)
    assert out["medal"]["tier"] == 41 and out["medal"]["confidence"] == "high"
    assert out["risk"]["level"] == "low" and [s["source"] for s in out["sources"]] == ["opendota", "stratz", "dotabuff"]
    async with session_scope() as s:
        prof = await s.scalar(select(PlayerProfile).where(PlayerProfile.user_id == uid))
        assert prof.medal_rank == 41 and prof.matches == 900 and prof.winrate == 50.0
        assert (await s.scalar(select(PlayerCheck).where(PlayerCheck.user_id == uid))).trigger == "test"
    public = (await pc.latest_checks([uid], admin=False))[uid]
    assert "risk" not in public and "ai" not in public and public["medal"]["name"]


async def test_ai_can_only_pick_real_source_and_budget_is_respected(db, monkeypatch, budget_file):
    uid = await _player(778)

    async def fake_collect(config, account_id, steam_id64):
        return [R("opendota", 41), R("stratz", None, ok=False), R("dotabuff", 43)]

    picks = iter(["stratz", "dotabuff"])

    async def fake_ai(config, facts):
        return {"medal": {"source": next(picks), "confidence": "high", "why": "тест"}, "summary": "ok"}, "test-model", None

    monkeypatch.setattr(pc, "collect", fake_collect)
    monkeypatch.setattr(pc, "ai_verdict", fake_ai)
    monkeypatch.setenv("AI_CHECK_DAILY", "2")

    async def no_reuse(user_id):
        return None

    monkeypatch.setattr(pc, "_recent_ai", no_reuse)
    from config import Config

    first = await pc.run_player_check(Config(), uid, use_ai=True)
    assert first["medal"]["source"] != "stratz"  # у STRATZ не было медали — выбор AI отклонён
    second = await pc.run_player_check(Config(), uid, use_ai=True)
    assert second["medal"]["source"] == "dotabuff" and second["medal"]["tier"] == 43
    third = await pc.run_player_check(Config(), uid, use_ai=True)
    assert "лимит" in (third["ai_error"] or "")


def test_new_smurf_signals():
    from services.player_checks import recent_features

    recent = [{"kills": 12, "deaths": 2, "assists": 10, "gold_per_min": 640, "xp_per_min": 700, "hero_id": 1 + (i % 2),
               "party_size": 3, "lobby_type": 7, "player_slot": 0, "radiant_win": i % 5 != 0} for i in range(20)]
    rf = recent_features(recent)
    assert rf["recent_avg_kda"] == 11.0 and rf["recent_avg_gpm"] == 640 and rf["recent_unique_heroes"] == 2
    assert rf["recent_party_share"] == 1.0 and rf["recent_ranked_share"] == 1.0
    feats = {"medal_tier": 51, "recent_matches": 20, "recent_wins": 16, "total_matches": 900, "dota_hours": 80,
             "account_level": 10, "first_match_at": NOW - 3 * 365 * 86400, "public_steam_profile": False, "behavior_score": 3000, **rf}
    score, level, reasons = risk_assessment(feats, NOW)
    texts = " ".join(r["text"] for r in reasons)
    for needle in ("KDA", "героя", "пати", "ч в Dota", "не сходятся", "скрыт", "поведения"):
        assert needle in texts, needle
    assert level == "high" and score >= 90
    # Опытный игрок с обычной статистикой — риск низкий.
    calm = {"medal_tier": 43, "recent_matches": 20, "recent_wins": 10, "total_matches": 3000, "dota_hours": 2500,
            "recent_avg_kda": 3.1, "recent_avg_gpm": 460, "recent_unique_heroes": 12, "recent_top_hero_share": 0.15}
    assert risk_assessment(calm, NOW)[1] == "low"


async def test_ai_failure_refunds_and_success_is_reused(db, monkeypatch, budget_file):
    uid = await _player(779)

    async def fake_collect(config, account_id, steam_id64):
        return [R("opendota", 41), R("stratz", 41), R("dotabuff", 43)]  # расхождение — AI нужен

    calls = {"n": 0}

    async def flaky_ai(config, facts):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, None, "AI API HTTP 429"
        return {"summary": "ок"}, "test-model", None

    monkeypatch.setattr(pc, "collect", fake_collect)
    monkeypatch.setattr(pc, "ai_verdict", flaky_ai)
    from config import Config

    await pc.run_player_check(Config(), uid, use_ai=True)
    assert ai_budget.usage()["check_total"] == 0  # сбой провайдера вернул попытку
    await pc.run_player_check(Config(), uid, use_ai=True)
    third = await pc.run_player_check(Config(), uid, use_ai=True)
    assert calls["n"] == 2 and ai_budget.usage()["check_total"] == 1  # третий раз — повтор вывода без запроса
    assert "повтор" in third["ai_model"]


def test_bot_budget_per_user_and_refund(budget_file, monkeypatch):
    monkeypatch.setenv("AI_BOT_USER_DAILY", "2")
    monkeypatch.setenv("AI_BOT_GLOBAL_DAILY", "3")
    assert ai_budget.try_consume_bot(1) and ai_budget.try_consume_bot(1)
    assert not ai_budget.try_consume_bot(1)
    ai_budget.refund_bot(1)
    assert ai_budget.try_consume_bot(1)
    assert ai_budget.try_consume_bot(2) is True  # третий вопрос за сутки
    assert ai_budget.try_consume_bot(3) is False  # общий лимит 3 исчерпан
    assert ai_budget.usage()["bot_total"] == 3


def test_default_ai_limits_fit_free_quota(monkeypatch):
    for k in ("AI_BOT_USER_DAILY", "AI_BOT_GLOBAL_DAILY", "AI_CHECK_DAILY"):
        monkeypatch.delenv(k, raising=False)
    lim = ai_budget.limits()
    assert lim["bot_global"] + lim["check"] <= 50


async def test_assistant_context_has_own_team_matches_only(db):
    from datetime import datetime, timezone

    from services.ai_user_assistant import build_context
    from services.tournament_engine import service as engine
    from tests.test_tournament_engine import _setup

    tid, teams = await _setup(4)
    async with session_scope() as s:
        from database.models import Tournament

        await engine.generate(s, await s.get(Tournament, tid), fmt_key="single_elim", settings={"bo_default": 3, "bo_final": 3})
        await s.commit()
        captain_tg = (await s.get(User, (await s.get(__import__("database.models", fromlist=["Team"]).Team, teams[0])).captain_user_id)).telegram_id
    ctx = await build_context(captain_tg)
    matches = ctx["me"]["team"]["matches"]
    assert matches and all(m["best_of"] == 3 for m in matches)
    assert "МСК" in (ctx["tournaments"][0]["start"] or "")
    assert datetime.now(timezone.utc)  # sanity


def test_player_avatar_normalized_to_small_jpeg(tmp_path, monkeypatch):
    from PIL import Image

    from services import player_avatars

    buf = io.BytesIO()
    Image.new("RGBA", (400, 300), (200, 30, 30, 255)).save(buf, "PNG")
    data = player_avatars._normalize(buf.getvalue())
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG" and max(img.size) <= 128


async def test_ai_skipped_when_sources_agree_and_risk_low(db, monkeypatch, budget_file):
    uid = await _player(780)

    async def fake_collect(config, account_id, steam_id64):
        return [R("opendota", 41), R("stratz", 41, match_count=2000, win_count=1000), R("dotabuff", 41)]

    async def must_not_call(config, facts):
        raise AssertionError("AI не должен вызываться")

    monkeypatch.setattr(pc, "collect", fake_collect)
    monkeypatch.setattr(pc, "ai_verdict", must_not_call)
    from config import Config

    out = await pc.run_player_check(Config(), uid, use_ai=True)
    assert "экономия" in out["ai_error"] and ai_budget.usage()["check_total"] == 0
