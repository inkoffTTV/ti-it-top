"""5.8: лобби, готовность/неявка, результат от капитанов, MVP, статистика, зал славы, бэкап."""
from __future__ import annotations

import io
import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy import select

pytest.importorskip("httpx")

from database.engine import session_scope  # noqa: E402
from database.models import Team, Tournament, TournamentMatch, User  # noqa: E402
from services import tournament_extras as extras  # noqa: E402
from services.tournament_engine import service as engine  # noqa: E402
from services.tournament_engine import timeutil  # noqa: E402
from tests.test_miniapp_v2 import ADMIN_TG, client, h  # noqa: E402,F401
from tests.test_tournament_engine import _setup  # noqa: E402


async def _captains(team_ids):
    async with session_scope() as s:
        out = {}
        for tid in team_ids:
            t = await s.get(Team, tid)
            out[tid] = (await s.get(User, t.captain_user_id)).telegram_id
        return out


async def _match(tid, key):
    async with session_scope() as s:
        return await s.scalar(select(TournamentMatch).where(TournamentMatch.tournament_id == tid, TournamentMatch.plan_key == key))


def _png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (50, 30), (10, 200, 10)).save(buf, "PNG")
    return buf.getvalue()


async def test_captain_report_flow_and_lobby(client, tmp_path, monkeypatch):
    monkeypatch.setattr(extras, "REPORT_DIR", tmp_path / "reports")
    tid, teams = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "single_elim", "bo_default": 3, "bo_final": 3})).status_code == 200
    m = await _match(tid, "R1-1")
    caps = await _captains([m.team1_id, m.team2_id])
    d = (await client.get(f"/api/v2/matches/{m.id}", headers=h(caps[m.team1_id]))).json()
    assert d["lobby"]["name"].startswith("TIIT") and len(d["lobby"]["password"]) == 6 and d["i_am_captain"]
    # Чужой (не капитан матча) не может прислать результат.
    other = (await _captains([t for t in teams if t not in (m.team1_id, m.team2_id)])).popitem()[1]
    r = await client.post(f"/api/v2/matches/{m.id}/report", headers=h(other), data={"score1": 2, "score2": 1})
    assert r.status_code == 403
    r = await client.post(f"/api/v2/matches/{m.id}/report", headers=h(caps[m.team1_id]), data={"score1": 2, "score2": 1},
                          files={"screenshot": ("s.png", _png(), "image/png")})
    assert r.status_code == 200, r.text
    rid = r.json()["report_id"]
    assert (await client.get(f"/api/v2/match-reports/{rid}/screenshot", headers=h(caps[m.team2_id]))).status_code == 200
    assert (await client.post(f"/api/v2/match-reports/{rid}/respond", headers=h(caps[m.team1_id]), json={"confirm": True})).status_code == 403
    assert (await client.post(f"/api/v2/match-reports/{rid}/respond", headers=h(caps[m.team2_id]), json={"confirm": True})).status_code == 200
    items = (await client.get("/api/v2/admin/match-reports", headers=h(ADMIN_TG))).json()["items"]
    assert items[0]["status"] == "CONFIRMED"
    assert (await client.post(f"/api/v2/admin/match-reports/{rid}", headers=h(ADMIN_TG), json={"approve": True})).status_code == 200
    m2 = await _match(tid, "R1-1")
    assert m2.status == "FINISHED" and m2.score1 == 2 and m2.winner_team_id == m.team1_id


async def test_ready_and_noshow_alert(db):
    tid, teams = await _setup(4)
    async with session_scope() as s:
        await engine.generate(s, await s.get(Tournament, tid), fmt_key="single_elim", settings={"bo_default": 1, "bo_final": 1})
        await s.commit()
    m = await _match(tid, "R1-1")
    caps = await _captains([m.team1_id, m.team2_id])
    now = timeutil.now_utc()
    async with session_scope() as s:
        await engine.set_schedule(s, m.id, (now - timedelta(minutes=2)).isoformat(), force=True)
        await s.commit()
    ok, _ = await extras.mark_ready(m.id, caps[m.team1_id])
    assert ok
    assert await extras.noshow_check(now) == []  # 5.13: ждём 15 минут после начала
    later = now + timedelta(minutes=14)
    alerts = await extras.noshow_alerts(later)
    assert len(alerts) == 1 and "Не подтвердили" in alerts[0]["text"]
    assert [slot for _, slot in alerts[0]["buttons"]] == [1]  # победу можно присудить только готовой команде
    assert await extras.noshow_check(later) == []  # один раз


async def test_mvp_stats_hall_of_fame(client):
    tid, teams = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "single_elim", "bo_default": 3, "bo_final": 3})).status_code == 200
    caps = await _captains(teams)
    assert (await client.get(f"/api/v2/tournaments/{tid}/mvp", headers=h(caps[teams[0]]))).json()["open"] is False
    for key in ("R1-1", "R1-2", "R2-1"):
        m = await _match(tid, key)
        assert (await client.post(f"/api/v2/admin/matches/{m.id}/result", headers=h(ADMIN_TG), json={"score1": 2, "score2": 1})).status_code == 200
    stats = (await client.get(f"/api/v2/tournaments/{tid}/stats", headers=h(caps[teams[0]]))).json()
    assert stats["series_played"] == 3 and stats["maps_played"] == 9 and stats["longest"]
    mvp = (await client.get(f"/api/v2/tournaments/{tid}/mvp", headers=h(caps[teams[0]]))).json()
    assert mvp["open"] and mvp["can_vote"]
    async with session_scope() as s:
        me = await s.scalar(select(User.id).where(User.telegram_id == caps[teams[0]]))
    target = next(c["user_id"] for c in mvp["candidates"] if c["user_id"] != me)
    assert (await client.post(f"/api/v2/tournaments/{tid}/mvp", headers=h(caps[teams[0]]), json={"candidate_user_id": me})).status_code == 400
    assert (await client.post(f"/api/v2/tournaments/{tid}/mvp", headers=h(caps[teams[0]]), json={"candidate_user_id": target})).status_code == 200
    hall = (await client.get("/api/v2/hall-of-fame", headers=h(caps[teams[0]]))).json()["items"]
    assert hall[0]["places"][0]["place"] == 1 and len(hall[0]["places"]) == 4 and hall[0]["mvp"]["votes"] == 1


def test_daily_backup_keeps_last_n(tmp_path):
    src = tmp_path / "t.db"
    con = sqlite3.connect(src)
    con.execute("create table x(a)")
    con.commit()
    con.close()
    dest = tmp_path / "bk"
    for i in range(3):
        (dest).mkdir(exist_ok=True)
        (dest / f"tournament_2026010{i}_0000.db").write_bytes(b"")
    path = extras.backup_once(str(src), dest, keep=2)
    assert path.exists() and len(list(dest.glob("tournament_*.db"))) == 2


async def test_audience_award_viewers_vote_fresh_accounts_blocked(client):
    tid, teams = await _setup(4)
    assert (await client.post(f"/api/v2/admin/tournaments/{tid}/engine/generate", headers=h(ADMIN_TG),
                              json={"format": "single_elim", "bo_default": 1, "bo_final": 1})).status_code == 200
    for key in ("R1-1", "R1-2", "R2-1"):
        m = await _match(tid, key)
        assert (await client.post(f"/api/v2/admin/matches/{m.id}/result", headers=h(ADMIN_TG), json={"score1": 1, "score2": 0})).status_code == 200
    await client.get("/api/v2/me", headers=h(9301))  # зритель, аккаунт создан ПОСЛЕ старта турнира (старт 02.10.2026 в прошлом? нет)
    async with session_scope() as s:
        t = await s.get(Tournament, tid)
        t.start_date = timeutil.now_utc() + timedelta(days=1)  # турнир «ещё не начался» — аккаунт старый
        await s.commit()
    state = (await client.get(f"/api/v2/tournaments/{tid}/mvp", headers=h(9301))).json()
    aud = state["audience"]
    assert aud["open"] and aud["can_vote"] and state["can_vote"] is False  # MVP — только игроки
    cand = aud["candidates"][0]["user_id"]
    assert (await client.post(f"/api/v2/tournaments/{tid}/audience-vote", headers=h(9301), json={"candidate_user_id": cand})).status_code == 200
    hall = (await client.get("/api/v2/hall-of-fame", headers=h(9301))).json()["items"]
    assert hall[0]["audience"]["votes"] == 1
    async with session_scope() as s:
        t = await s.get(Tournament, tid)
        t.start_date = timeutil.now_utc() - timedelta(days=1)  # свежий аккаунт (создан после старта) — не голосует
        await s.commit()
    await client.get("/api/v2/me", headers=h(9302))
    r = await client.post(f"/api/v2/tournaments/{tid}/audience-vote", headers=h(9302), json={"candidate_user_id": cand})
    assert r.status_code == 403
