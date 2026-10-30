"""План этапа: граф матчей со связями «куда идёт победитель/проигравший».

Форматы строят «виртуальную» полную сетку (размер — степень двойки), где
у слота может не быть соперника. collapse() убирает такие пары: команда
с BYE проходит дальше без матча — фиктивных матчей в БД нет.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class Src:
    """Источник слота матча: конкретная команда или победитель/проигравший другого матча."""

    kind: str  # "team" | "winner" | "loser"
    ref: Any  # team_id (int) или ключ матча плана (str)


def team(team_id: int | None) -> Src | None:
    return Src("team", int(team_id)) if team_id is not None else None


def winner(key: str) -> Src:
    return Src("winner", key)


def loser(key: str) -> Src:
    return Src("loser", key)


@dataclass
class MatchSpec:
    key: str
    bracket: str
    round_no: int
    position: int
    round_name: str
    best_of: int
    phase: float
    src1: Src | None
    src2: Src | None


@dataclass
class StagePlan:
    matches: list[MatchSpec]
    seeds: dict[int, int] = field(default_factory=dict)  # team_id -> seed
    meta: dict[str, Any] = field(default_factory=dict)


def collapse(virtual: list[MatchSpec]) -> list[MatchSpec]:
    """Удаляет пары без соперника и пересобирает источники.

    Список должен идти в порядке зависимостей (источник раньше потребителя).
    """
    win_alias: dict[str, Src | None] = {}
    lose_alias: dict[str, Src | None] = {}

    def resolve(src: Src | None) -> Src | None:
        if src is None:
            return None
        if src.kind == "team":
            return src
        table = win_alias if src.kind == "winner" else lose_alias
        if src.ref not in table:
            raise ValueError(f"План сетки: матч {src.ref} используется раньше, чем объявлен.")
        return table[src.ref]

    real: list[MatchSpec] = []
    for spec in virtual:
        a, b = resolve(spec.src1), resolve(spec.src2)
        if a is not None and b is not None:
            real.append(replace(spec, src1=a, src2=b))
            win_alias[spec.key] = winner(spec.key)
            lose_alias[spec.key] = loser(spec.key)
        else:
            win_alias[spec.key] = a or b  # автопроход без матча
            lose_alias[spec.key] = None
    return real


def order_matches(specs: list[MatchSpec]) -> list[MatchSpec]:
    """Порядок игры: по фазе, затем по позиции в раунде."""
    return sorted(specs, key=lambda s: (s.phase, s.bracket != "upper", s.position))
