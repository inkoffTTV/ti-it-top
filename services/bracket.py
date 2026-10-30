"""Tournament bracket generation with hybrid skill-based seeding.

Bracket state is persisted as JSON under runtime/brackets/; SQLite is not used
for bracket/match state.  The generator works for every approved team count
from 2 through the tournament's configured maximum (up to 1024).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    ApplicationStatus,
    PlayerProfile,
    Team,
    TeamMember,
    Tournament,
    TournamentApplication,
)

logger = logging.getLogger("BRACKET")

MAX_BRACKET_TEAMS = 1024
SEED_PAIRS = (
    (1, 16),
    (8, 9),
    (4, 13),
    (5, 12),
    (2, 15),
    (7, 10),
    (3, 14),
    (6, 11),
)

BRACKET_DIR = Path(__file__).resolve().parent.parent / "runtime" / "brackets"
_file_lock = asyncio.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(tournament_id: int) -> Path:
    BRACKET_DIR.mkdir(parents=True, exist_ok=True)
    return BRACKET_DIR / f"tournament_{int(tournament_id)}.json"


def _read_state(tournament_id: int) -> dict[str, Any] | None:
    path = _state_path(tournament_id)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError) as exc:
        logger.warning("[BRACKET] Failed to read %s: %s", path, exc)
        return None


def _write_state(tournament_id: int, state: dict[str, Any]) -> None:
    path = _state_path(tournament_id)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as fh:
        fh.write(payload)
        tmp_name = fh.name
    os.replace(tmp_name, path)


def _normalize_team(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(team["id"]),
        "name": str(team["name"]),
        "avg_mmr": team.get("avg_mmr"),
        "mmr_count": int(team.get("mmr_count") or 0),
        "mmr_complete": bool(team.get("mmr_complete")),
        "roster_count": int(team.get("roster_count") or 0),
        "avatar": bool(team.get("avatar")),
    }


def _signature(teams: list[dict[str, Any]]) -> str:
    compact = [
        {
            "id": int(t["id"]),
            "avg_mmr": t.get("avg_mmr"),
            "mmr_count": int(t.get("mmr_count") or 0),
            "roster_count": int(t.get("roster_count") or 0),
        }
        for t in sorted(teams, key=lambda x: int(x["id"]))
    ]
    raw = json.dumps(
        compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def next_power_of_two(team_count: int) -> int:
    if team_count < 2:
        raise ValueError("Для турнирной сетки нужны минимум 2 команды.")
    if team_count > MAX_BRACKET_TEAMS:
        raise ValueError(f"Сетка поддерживает максимум {MAX_BRACKET_TEAMS} команд.")
    return 1 << (team_count - 1).bit_length()


def seeded_slot_order(size: int) -> list[int]:
    """Return standard bracket seed slot ordering for any power-of-two size."""
    if size < 2 or size & (size - 1):
        raise ValueError("Размер сетки должен быть степенью двойки.")
    order = [1, 2]
    while len(order) < size:
        next_size = len(order) * 2
        order = [seed for old in order for seed in (old, next_size + 1 - old)]
    return order


def _round_name(match_count: int) -> str:
    if match_count == 1:
        return "Финал"
    if match_count == 2:
        return "1/2 финала"
    return f"1/{match_count} финала"


async def approved_team_stats(
    session: AsyncSession, tournament_id: int
) -> list[dict[str, Any]]:
    """Return approved teams and their confirmed MMR aggregates.

    5.2: один агрегированный запрос движка вместо запроса на каждую команду (N+1);
    правила подсчёта прежние (только MMR с флагом mmr_verified).
    """
    from services.tournament_engine.service import approved_teams

    avatar_dir = (
        Path(__file__).resolve().parent.parent / "miniapp" / "data" / "team_avatars"
    )
    out: list[dict[str, Any]] = []
    for team in await approved_teams(session, tournament_id):
        out.append(
            {
                "id": team["id"],
                "name": team["name"],
                "avg_mmr": team["avg_mmr"],
                "mmr_count": team["mmr_count"],
                "mmr_complete": team["mmr_complete"],
                "roster_count": team["roster_count"],
                "avatar": bool(
                    team["avatar_file"]
                    or (avatar_dir / f"team_{int(team['id'])}.jpg").is_file()
                ),
            }
        )
    return out


def _sort_remaining(teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        teams,
        key=lambda t: (
            t.get("avg_mmr") is None,
            -(int(t.get("avg_mmr")) if t.get("avg_mmr") is not None else -1),
            -int(t.get("mmr_count") or 0),
            str(t.get("name") or "").casefold(),
            int(t["id"]),
        ),
    )


def build_seed_map(
    teams: list[dict[str, Any]],
    manual_seed_1: int,
    manual_seed_2: int,
) -> dict[str, int]:
    """Legacy hybrid seeding kept for compatibility with older callers/tests."""
    by_id = {int(t["id"]): t for t in teams}
    if manual_seed_1 == manual_seed_2:
        raise ValueError("Seed #1 и seed #2 не могут быть одной командой.")
    if manual_seed_1 not in by_id or manual_seed_2 not in by_id:
        raise ValueError("Для ручных seed #1 и #2 выберите допущенные команды.")
    if len(teams) < 2:
        raise ValueError("Для генерации сетки нужны минимум 2 одобренные команды.")
    if len(teams) > MAX_BRACKET_TEAMS:
        raise ValueError(f"Для сетки допущено не более {MAX_BRACKET_TEAMS} команд.")

    remaining = [
        t for t in teams if int(t["id"]) not in {manual_seed_1, manual_seed_2}
    ]
    remaining = _sort_remaining(remaining)
    seed_map: dict[str, int] = {"1": int(manual_seed_1), "2": int(manual_seed_2)}
    for seed_no, team in enumerate(remaining, start=3):
        seed_map[str(seed_no)] = int(team["id"])
    return seed_map


def build_opening_match_seed_map(
    teams: list[dict[str, Any]],
    opening_team_a: int,
    opening_team_b: int,
) -> dict[str, int]:
    """Assign the first two seeds to the explicitly selected opening match.

    The remaining teams receive seeds #3..#N by descending confirmed average MMR.
    This makes the selected teams an actual first-round match instead of two
    independent manual seeds that could be sent to different matches.
    """
    by_id = {int(t["id"]): t for t in teams}
    opening_team_a = int(opening_team_a)
    opening_team_b = int(opening_team_b)
    if opening_team_a == opening_team_b:
        raise ValueError("Команды первого матча должны быть разными.")
    if opening_team_a not in by_id or opening_team_b not in by_id:
        raise ValueError("Для первого матча выберите две допущенные команды.")
    if len(teams) < 2:
        raise ValueError("Для генерации сетки нужны минимум 2 одобренные команды.")
    if len(teams) > MAX_BRACKET_TEAMS:
        raise ValueError(f"Для сетки допущено не более {MAX_BRACKET_TEAMS} команд.")

    remaining = [
        t
        for t in teams
        if int(t["id"]) not in {opening_team_a, opening_team_b}
    ]
    remaining = _sort_remaining(remaining)
    seed_map: dict[str, int] = {"1": opening_team_a, "2": opening_team_b}
    for seed_no, team in enumerate(remaining, start=3):
        seed_map[str(seed_no)] = int(team["id"])
    return seed_map

def _copy_team(
    team: dict[str, Any] | None, seed: int | None = None
) -> dict[str, Any] | None:
    if team is None:
        return None
    value = dict(team)
    if seed is not None:
        value["seed"] = int(seed)
    return value


def build_bracket(
    seed_map: dict[str, int], team_map: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    """Build a complete bracket for 2..1024 teams with automatic byes.

    Seed #1 and #2 are always the explicitly selected opening match. Remaining
    teams are seeded #3..#N by skill. First-round byes are assigned to the
    strongest remaining teams; the rest are paired strongest-vs-weakest so the
    bracket stays skill-distributed without inventing any MMR values.
    """
    team_count = len(seed_map)
    size = next_power_of_two(team_count)
    seed_to_id = {int(k): int(v) for k, v in seed_map.items()}
    id_to_seed = {team_id: seed for seed, team_id in seed_to_id.items()}

    ordered_teams = [
        _copy_team(team_map[team_id], seed)
        for seed, team_id in sorted(seed_to_id.items())
        if team_id in team_map
    ]
    opening_a = _copy_team(team_map.get(seed_to_id.get(1)), 1)
    opening_b = _copy_team(team_map.get(seed_to_id.get(2)), 2)
    if opening_a is None or opening_b is None:
        raise ValueError("В сетке должны быть две команды первого матча.")

    # Seeds 3..N are already sorted by confirmed average MMR by the caller.
    remaining = [t for t in ordered_teams if t.get("seed", 0) >= 3]
    bye_count = size - team_count
    if bye_count > len(remaining):
        raise ValueError("Некорректное число BYE для текущего количества команд.")

    # Top remaining seeds receive byes; the rest are paired from both ends.
    bye_teams = remaining[:bye_count]
    active = remaining[bye_count:]
    pair_count = len(active) // 2
    remaining_matches: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    for i in range(pair_count):
        left = active[i]
        right = active[-1 - i]
        remaining_matches.append((left, right))
    if len(active) % 2:
        bye_teams.append(active[pair_count])

    # The opening match is always Match 1. Other real matches follow, then the
    # automatic byes. With this construction the first round always contains
    # exactly size/2 matches and every real entrant appears exactly once.
    first_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = [
        (opening_a, opening_b),
        *remaining_matches,
        *[(t, None) for t in bye_teams],
    ]
    if len(first_pairs) != size // 2:
        raise ValueError("Не удалось собрать полный первый раунд сетки.")

    rounds: list[dict[str, Any]] = []
    current_matches: list[dict[str, Any]] = []
    for idx, (team_a, team_b) in enumerate(first_pairs, start=1):
        aid = int(team_a["id"]) if team_a else None
        bid = int(team_b["id"]) if team_b else None
        bye = bool(team_a) ^ bool(team_b)
        winner = aid if bye and aid is not None else bid if bye and bid is not None else None
        current_matches.append(
            {
                "id": f"R1M{idx}",
                "label": "Матч 1 · Открывающий" if idx == 1 else f"Матч {idx}",
                "seed_a": team_a.get("seed") if team_a else None,
                "seed_b": team_b.get("seed") if team_b else None,
                "team_a": team_a,
                "team_b": team_b,
                "status": "BYE" if bye else "PENDING",
                "score_a": None,
                "score_b": None,
                "winner": winner,
            }
        )

    rounds.append(
        {
            "key": "round_1",
            "name": _round_name(len(current_matches)),
            "matches": current_matches,
        }
    )

    round_number = 2
    while len(current_matches) > 1:
        next_matches: list[dict[str, Any]] = []
        for idx in range(0, len(current_matches), 2):
            left = current_matches[idx]
            right = current_matches[idx + 1]
            left_winner = int(left["winner"]) if left.get("winner") is not None else None
            right_winner = int(right["winner"]) if right.get("winner") is not None else None
            left_team = (
                _copy_team(team_map.get(left_winner), id_to_seed.get(left_winner))
                if left_winner is not None
                else None
            )
            right_team = (
                _copy_team(team_map.get(right_winner), id_to_seed.get(right_winner))
                if right_winner is not None
                else None
            )
            next_matches.append(
                {
                    "id": f"R{round_number}M{idx // 2 + 1}",
                    "label": f"Матч {idx // 2 + 1}",
                    "source_a": left["id"],
                    "source_b": right["id"],
                    "team_a": left_team,
                    "team_b": right_team,
                    "status": "PENDING",
                    "score_a": None,
                    "score_b": None,
                    "winner": None,
                }
            )
        current_matches = next_matches
        rounds.append(
            {
                "key": f"round_{round_number}",
                "name": _round_name(len(current_matches)),
                "matches": current_matches,
            }
        )
        round_number += 1

    return {
        "rounds": rounds,
        "team_count": team_count,
        "bracket_size": size,
    }

def _public_team(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(team["id"]),
        "name": team["name"],
        "avg_mmr": team.get("avg_mmr"),
        "mmr_count": int(team.get("mmr_count") or 0),
        "mmr_complete": bool(team.get("mmr_complete")),
        "roster_count": int(team.get("roster_count") or 0),
        "avatar": bool(team.get("avatar")),
    }


def _legacy_from_engine(view: dict[str, Any], team_info: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Сетка движка 5.2 в прежнем JSON-формате (для старого UI и старых клиентов)."""

    def team(x: dict[str, Any] | None) -> dict[str, Any] | None:
        if not x:
            return None
        info = team_info.get(int(x["id"]), {})
        return {**_public_team({"id": x["id"], "name": x["name"], **{k: info.get(k) for k in ("avg_mmr", "mmr_count", "mmr_complete", "roster_count", "avatar")}}),
                "seed": x.get("seed")}

    multi = len(view.get("brackets") or []) > 1
    rounds = []
    for b in view.get("brackets") or []:
        for r in b["rounds"]:
            rounds.append({
                "key": f"{b['key']}_{r['round_no']}",
                "name": f"{b['title']} · {r['name']}" if multi else r["name"],
                "matches": [{
                    "id": m["key"],
                    "label": m["label"],
                    "seed_a": (m["team1"] or {}).get("seed"),
                    "seed_b": (m["team2"] or {}).get("seed"),
                    "team_a": team(m["team1"]),
                    "team_b": team(m["team2"]),
                    "status": "FINISHED" if m["status"] == "FINISHED" else "PENDING",
                    "score_a": m["score1"],
                    "score_b": m["score2"],
                    "winner": m["winner_team_id"],
                } for m in r["matches"]],
            })
    return {"rounds": rounds, "team_count": view.get("team_count"), "bracket_size": None}


async def _engine_admin_state(session: AsyncSession, tournament: Tournament) -> dict[str, Any] | None:
    from services.tournament_engine.service import stage_view

    view = await stage_view(session, tournament)
    if view is None:
        return None
    teams = [_normalize_team(x) for x in await approved_team_stats(session, tournament.id)]
    info = {int(t["id"]): t for t in teams}
    return {
        "exists": True,
        "locked": bool(view["locked"]),
        "stale": bool(view["stale"]),
        "team_count": len(teams),
        "teams": teams,
        "opening_team_a": None,
        "opening_team_b": None,
        "manual_seed_1": None,
        "manual_seed_2": None,
        "seed_map": {},
        "bracket": _legacy_from_engine(view, info),
        "generated_at": view.get("generated_at"),
        "data_signature": "",
        "engine": view,
    }


async def get_admin_state(
    session: AsyncSession, tournament: Tournament
) -> dict[str, Any]:
    engine_state = await _engine_admin_state(session, tournament)
    if engine_state is not None:
        return engine_state
    teams = [_normalize_team(x) for x in await approved_team_stats(session, tournament.id)]
    state = _read_state(tournament.id)
    current_signature = _signature(teams)
    if not state:
        return {
            "exists": False,
            "locked": False,
            "stale": False,
            "team_count": len(teams),
            "teams": teams,
            "manual_seed_1": None,
            "manual_seed_2": None,
            "seed_map": {},
            "bracket": None,
            "generated_at": None,
        }

    stored_signature = str(state.get("data_signature") or "")
    seed_map = {str(k): int(v) for k, v in (state.get("seed_map") or {}).items()}
    team_map = {int(t["id"]): _public_team(t) for t in teams}
    try:
        state_bracket = build_bracket(seed_map, team_map) if seed_map else None
    except (ValueError, KeyError) as exc:
        # Команду сняли после генерации: показываем «устарела», а не ошибку.
        logger.warning("[BRACKET] tournament=%s stored bracket no longer matches teams: %s", tournament.id, exc)
        state_bracket = None
        stored_signature = "stale"
    opening_a = state.get("opening_team_a")
    opening_b = state.get("opening_team_b")
    if opening_a is None or opening_b is None:
        try:
            first = (state_bracket or {}).get("rounds", [{}])[0].get("matches", [])[0]
            opening_a = (first.get("team_a") or {}).get("id")
            opening_b = (first.get("team_b") or {}).get("id")
        except (IndexError, AttributeError, TypeError):
            opening_a = opening_b = None
    return {
        "exists": bool(seed_map),
        "locked": bool(state.get("locked")),
        "stale": stored_signature != current_signature,
        "team_count": len(teams),
        "teams": teams,
        "opening_team_a": opening_a,
        "opening_team_b": opening_b,
        # Legacy fields retained so older Mini App builds do not crash.
        "manual_seed_1": state.get("manual_seed_1", opening_a),
        "manual_seed_2": state.get("manual_seed_2", opening_b),
        "seed_map": seed_map,
        "bracket": state_bracket,
        "generated_at": state.get("generated_at"),
        "data_signature": stored_signature,
    }


async def generate(
    session: AsyncSession,
    tournament: Tournament,
    opening_team_a: int,
    opening_team_b: int,
    lock: bool = False,
) -> dict[str, Any]:
    from services.tournament_engine import service as engine

    stage = await engine.get_stage(session, tournament.id)
    if stage is not None:
        # Турнир уже в движке 5.2: пересобираем там же, сохраняя формат и BO.
        settings = engine._settings(stage)
        try:
            await engine.generate(
                session, tournament, fmt_key=stage.format, seeding_mode="opening",
                settings=settings, opening=(int(opening_team_a), int(opening_team_b)), lock=lock,
            )
        except engine.EngineError as exc:
            raise ValueError(str(exc)) from None
        await session.flush()
        return await get_admin_state(session, tournament)

    teams = [_normalize_team(x) for x in await approved_team_stats(session, tournament.id)]
    if len(teams) < 2:
        raise ValueError("Для генерации сетки нужно минимум 2 одобренные команды.")
    if len(teams) > int(tournament.max_teams):
        raise ValueError("Количество одобренных команд превышает лимит турнира.")

    incomplete = [t for t in teams if not t["mmr_complete"]]
    if lock and incomplete:
        names = ", ".join(t["name"] for t in incomplete[:5])
        extra = "…" if len(incomplete) > 5 else ""
        raise ValueError(
            "Нельзя зафиксировать сетку: у каждой команды нужно минимум 5 игроков "
            f"с подтверждённым MMR. Проблемные команды: {names}{extra}."
        )

    seed_map = build_opening_match_seed_map(teams, int(opening_team_a), int(opening_team_b))
    team_map = {int(t["id"]): _public_team(t) for t in teams}
    bracket = build_bracket(seed_map, team_map)
    state = {
        "version": 2,
        "tournament_id": int(tournament.id),
        "locked": bool(lock),
        "opening_team_a": int(opening_team_a),
        "opening_team_b": int(opening_team_b),
        # Legacy aliases retained for compatibility with an older frontend.
        "manual_seed_1": int(opening_team_a),
        "manual_seed_2": int(opening_team_b),
        "seed_map": seed_map,
        "data_signature": _signature(teams),
        "generated_at": utc_now_iso(),
        "bracket": bracket,
    }
    async with _file_lock:
        _write_state(tournament.id, state)
    logger.info(
        "[BRACKET] tournament=%s generated locked=%s teams=%s opening=(%s,%s)",
        tournament.id,
        lock,
        len(teams),
        opening_team_a,
        opening_team_b,
    )
    return await get_admin_state(session, tournament)


async def lock_existing(
    session: AsyncSession, tournament: Tournament
) -> dict[str, Any]:
    from services.tournament_engine import service as engine

    if await engine.get_stage(session, tournament.id) is not None:
        try:
            await engine.lock_stage(session, tournament)
        except engine.EngineError as exc:
            raise ValueError(str(exc)) from None
        await session.flush()
        return await get_admin_state(session, tournament)

    state = _read_state(tournament.id)
    if not state:
        raise ValueError("Сначала сгенерируйте сетку.")
    if bool(state.get("locked")):
        return await get_admin_state(session, tournament)
    opening_a = state.get("opening_team_a")
    opening_b = state.get("opening_team_b")
    if opening_a is None or opening_b is None:
        seed_map = {str(k): int(v) for k, v in (state.get("seed_map") or {}).items()}
        opening_a = seed_map.get("1")
        opening_b = seed_map.get("2")
    if opening_a is None or opening_b is None:
        raise ValueError("Не удалось определить первый матч старой сетки.")
    return await generate(
        session,
        tournament,
        int(opening_a),
        int(opening_b),
        lock=True,
    )


async def public_state(
    session: AsyncSession, tournament: Tournament
) -> dict[str, Any]:
    state = await get_admin_state(session, tournament)
    if state.get("engine"):
        return {
            "exists": True,
            "locked": bool(state["locked"]),
            "stale": bool(state["stale"]),
            "team_count": int(state["team_count"]),
            "teams": state["teams"],
            "seed_map": {},
            "bracket": state["bracket"],
            "generated_at": state["generated_at"],
            "engine": state["engine"],
        }
    return {
        "exists": bool(state["exists"] and state["seed_map"]),
        "locked": bool(state["locked"]),
        "stale": bool(state["stale"]),
        "team_count": int(state["team_count"]),
        "teams": state["teams"],
        "seed_map": state["seed_map"],
        "bracket": state["bracket"],
        "generated_at": state["generated_at"],
    }
