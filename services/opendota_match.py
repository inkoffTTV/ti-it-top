"""5.13: результат серии по ID матчей Dota через OpenDota.

Капитан вводит ID матчей (или ссылки dotabuff/opendota/stratz). По каждому матчу бот берёт с OpenDota
победителя, длительность и героев, а стороны (Radiant/Dire) сопоставляет с командами по Steam-аккаунтам
игроков составов. Если обе команды опознаны уверенно — результат не требует подтверждения соперника.
Ничего не выдумываем: нет данных — честно говорим, что OpenDota матч ещё не знает.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import PlayerProfile, TeamMember, TournamentMatch
from services.tournament_engine import timeutil

logger = logging.getLogger("OPENDOTA_MATCH")

STEAM64_BASE = 76561197960265728
API = "https://api.opendota.com/api"
_HEROES: dict[int, str] = {}


class MatchDataError(ValueError):
    pass


def parse_ids(raw: Any) -> list[int]:
    """«8012345678, https://www.dotabuff.com/matches/8012345679» → [8012345678, 8012345679] (без повторов)."""
    out: list[int] = []
    for token in re.findall(r"\d{8,12}", str(raw or "")):
        n = int(token)
        if n not in out:
            out.append(n)
    return out[:5]


def account_id(steam64: Any) -> int | None:
    try:
        n = int(str(steam64).strip())
    except (TypeError, ValueError):
        return None
    return n - STEAM64_BASE if n > STEAM64_BASE else n


async def _get(url: str) -> tuple[int, Any]:
    from services.http_client import get_http_client

    resp = await asyncio.wait_for(get_http_client().get(url, timeout=15), timeout=20)
    try:
        return resp.status, json.loads(resp.body)
    except ValueError:
        return resp.status, None


async def heroes() -> dict[int, str]:
    if not _HEROES:
        try:
            st, data = await _get(f"{API}/heroes")
            if st == 200 and isinstance(data, list):
                _HEROES.update({int(h["id"]): str(h.get("localized_name") or h.get("name")) for h in data if isinstance(h, dict) and h.get("id")})
        except Exception as exc:  # noqa: BLE001
            logger.info("[OPENDOTA] heroes failed: %s", exc)
    return _HEROES


async def fetch(match_id: int) -> dict[str, Any]:
    try:
        st, data = await _get(f"{API}/matches/{int(match_id)}")
    except Exception as exc:  # noqa: BLE001
        raise MatchDataError(f"OpenDota недоступен ({type(exc).__name__}). Попробуйте позже или пришлите скриншот.") from None
    if st == 200 and isinstance(data, dict) and data.get("players"):
        return data
    try:  # просим OpenDota забрать матч у Valve — для лобби это обычно занимает несколько минут
        from services.http_client import get_http_client

        await asyncio.wait_for(get_http_client().post_json(f"{API}/request/{int(match_id)}", {}), timeout=20)
    except Exception:  # noqa: BLE001
        pass
    raise MatchDataError(f"OpenDota пока не знает матч {match_id}. Запрос на обработку отправлен — повторите через 5–10 минут.")


async def _team_accounts(session: AsyncSession, team_id: int) -> set[int]:
    rows = (await session.execute(select(PlayerProfile.steam_id64).join(TeamMember, TeamMember.user_id == PlayerProfile.user_id)
                                  .where(TeamMember.team_id == int(team_id)))).scalars().all()
    return {a for a in (account_id(x) for x in rows if x) if a}


def _is_radiant(p: dict[str, Any]) -> bool:
    """Сторона игрока: поле isRadiant, а если его нет — по слоту (0–127 Radiant, 128+ Dire)."""
    if p.get("isRadiant") is not None:
        return bool(p["isRadiant"])
    return int(p.get("player_slot") or 0) < 128


def _side_of(accounts: set[int], radiant: set[int], dire: set[int]) -> tuple[str | None, int]:
    r, d = len(accounts & radiant), len(accounts & dire)
    if r == d:
        return None, 0
    return ("radiant", r) if r > d else ("dire", d)


async def collect(ids: list[int]) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """Скачивает матчи и справочник героев — вне транзакции БД (сеть может отвечать секундами)."""
    if not ids:
        raise MatchDataError("Укажите ID матча Dota (число из Dotabuff, OpenDota или клиента игры).")
    return [await fetch(i) for i in ids], dict(await heroes())


async def analyze(session: AsyncSession, m: TournamentMatch, ids: list[int], data: list[dict[str, Any]],
                  hero: dict[int, str] | None = None) -> dict[str, Any]:
    """Разбор серии по уже скачанным матчам: стороны команд, победитель каждой игры, длительность, герои."""
    if not ids or len(data) != len(ids):
        raise MatchDataError("Укажите ID матча Dota (число из Dotabuff, OpenDota или клиента игры).")
    acc1, acc2 = await _team_accounts(session, m.team1_id), await _team_accounts(session, m.team2_id)
    hero = hero or {}
    games, warnings = [], []
    confident = True
    for no, mid in enumerate(ids, start=1):
        g = data[no - 1]
        players = [p for p in g.get("players") or [] if isinstance(p, dict)]
        radiant = {int(p["account_id"]) for p in players if p.get("account_id") and _is_radiant(p)}
        dire = {int(p["account_id"]) for p in players if p.get("account_id")} - radiant
        s1, n1 = _side_of(acc1, radiant, dire)
        s2, n2 = _side_of(acc2, radiant, dire)
        if s1 and s2 and s1 == s2:
            raise MatchDataError(f"Матч {mid}: игроки обеих команд на одной стороне — это не ваша серия.")
        if not s1 and not s2:
            raise MatchDataError(f"Матч {mid}: среди игроков нет участников этих команд (у них должен быть привязан Steam). Проверьте ID.")
        if not s1:
            s1 = "dire" if s2 == "radiant" else "radiant"
        if not s2:
            s2 = "dire" if s1 == "radiant" else "radiant"
        if min(n1, n2) < 2:
            confident = False
            warnings.append(f"Матч {mid}: опознано мало игроков (команда 1 — {n1}, команда 2 — {n2}), проверьте вручную.")
        radiant_win = bool(g.get("radiant_win"))
        winner = m.team1_id if (s1 == "radiant") == radiant_win else m.team2_id
        start = datetime.fromtimestamp(int(g["start_time"]), tz=timezone.utc) if g.get("start_time") else None
        if start and m.scheduled_at and start < timeutil.as_utc(m.scheduled_at) - timedelta(hours=12):
            confident = False
            warnings.append(f"Матч {mid} сыгран {timeutil.fmt_local(start)} — намного раньше назначенного времени серии.")
        dur = int(g.get("duration") or 0)

        def side_heroes(side: str) -> list[str]:
            return [hero.get(int(p.get("hero_id") or 0), f"герой #{p.get('hero_id')}") for p in players
                    if (side == "radiant") == _is_radiant(p)]

        games.append({"no": no, "dota_match_id": int(mid), "winner_team_id": int(winner), "duration": f"{dur // 60}:{dur % 60:02d}",
                      "start": timeutil.fmt_local(start) if start else None, "team1_side": s1,
                      "heroes1": side_heroes(s1), "heroes2": side_heroes(s2), "identified": [n1, n2]})
    wins1 = sum(1 for g in games if g["winner_team_id"] == m.team1_id)
    return {"games": games, "score1": wins1, "score2": len(games) - wins1, "confident": confident, "warnings": warnings}

