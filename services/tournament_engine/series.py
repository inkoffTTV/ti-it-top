"""Правила серий BO1/BO3/BO5: серия — это не одна игра.

Побеждает тот, кто первым взял большинство карт (BO3 — 2, BO5 — 3);
оставшиеся карты не играются. Тех. поражение фиксируется счётом «нужное число:0».
"""
from __future__ import annotations

ALLOWED_BO = (1, 3, 5)


class SeriesError(ValueError):
    pass


def validate_bo(value: object, *, allow_even: bool = False) -> int:
    """BO1/BO3/BO5; в групповых форматах ещё BO2 (возможна ничья 1:1, как в группах Dota)."""
    allowed = ALLOWED_BO + ((2,) if allow_even else ())
    text = "Формат серии: BO1, BO2, BO3 или BO5." if allow_even else "Формат серии: BO1, BO3 или BO5."
    try:
        bo = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise SeriesError(text) from None
    if bo not in allowed:
        raise SeriesError(text)
    return bo


def wins_needed(best_of: int) -> int:
    return int(best_of) // 2 + 1


def allowed_scores(best_of: int) -> list[str]:
    if int(best_of) == 2:
        return ["2:0", "1:1", "0:2"]
    need = wins_needed(best_of)
    wins = [f"{need}:{x}" for x in range(need)]
    return wins + [f"{x}:{need}" for x in range(need)]


def validate_score(best_of: int, score1: object, score2: object, *, allow_draw: bool = False) -> int:
    """Проверяет счёт серии. Возвращает слот победителя (1 или 2), 0 — ничья (только BO2 в группах)."""
    try:
        s1, s2 = int(score1), int(score2)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise SeriesError("Счёт — целые числа.") from None
    if int(best_of) == 2:
        if (s1, s2) in ((2, 0), (0, 2)):
            return 1 if s1 > s2 else 2
        if (s1, s2) == (1, 1) and allow_draw:
            return 0
        raise SeriesError("BO2: допустимый счёт 2:0, 1:1 (ничья) или 0:2.")
    need = wins_needed(best_of)
    if s1 < 0 or s2 < 0 or max(s1, s2) != need or min(s1, s2) >= need or s1 + s2 > best_of:
        raise SeriesError(
            f"BO{best_of}: победитель берёт {need} "
            f"{'карту' if need == 1 else 'карты'}. Допустимый счёт: {', '.join(allowed_scores(best_of))}."
        )
    return 1 if s1 > s2 else 2


def forfeit_score(best_of: int, winner_slot: int) -> tuple[int, int]:
    need = wins_needed(best_of)
    return (need, 0) if int(winner_slot) == 1 else (0, need)
