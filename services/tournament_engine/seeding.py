"""Посев: порядок слотов первого раунда (размер — ближайшая степень двойки).

* standard — классический посев 1–N, 2–(N−1)…; 1 и 2 в разных половинах;
  BYE достаются верхним посевам (источники: Wikipedia «Single-elimination», «Bye»).
* opening — как в прежней сетке проекта: админ выбирает команды матча №1,
  остальные посеяны по среднему подтверждённому MMR (сильный против слабого),
  BYE — сильнейшим; пары с BYE чередуются с реальными, чтобы команды с BYE
  не играли друг с другом во 2-м раунде.
* pairs — явный список пар (перенос уже опубликованной сетки).
* everyone — все играют в первом раунде (сильный против слабого), BYE только у
  лишней команды при нечётном числе; то же раскладывание использует автодобор пар.
"""
from __future__ import annotations

from typing import Any

MAX_TEAMS = 1024


def next_power_of_two(count: int) -> int:
    if count < 2:
        raise ValueError("Для сетки нужны минимум 2 команды.")
    if count > MAX_TEAMS:
        raise ValueError(f"Сетка поддерживает максимум {MAX_TEAMS} команд.")
    return 1 << (count - 1).bit_length()


def seeded_slot_order(size: int) -> list[int]:
    if size < 2 or size & (size - 1):
        raise ValueError("Размер сетки должен быть степенью двойки.")
    order = [1, 2]
    while len(order) < size:
        nxt = len(order) * 2
        order = [s for old in order for s in (old, nxt + 1 - old)]
    return order


def sort_by_skill(teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """По среднему подтверждённому MMR; без данных — в конец (значения не выдумываются)."""
    return sorted(
        teams,
        key=lambda t: (
            t.get("avg_mmr") is None,
            -(int(t["avg_mmr"]) if t.get("avg_mmr") is not None else -1),
            -int(t.get("mmr_count") or 0),
            str(t.get("name") or "").casefold(),
            int(t["id"]),
        ),
    )


def standard_slots(ordered_ids: list[int]) -> tuple[list[int | None], dict[int, int]]:
    """ordered_ids — команды по посеву (#1 первым)."""
    n = len(ordered_ids)
    size = next_power_of_two(n)
    slots = [ordered_ids[s - 1] if s <= n else None for s in seeded_slot_order(size)]
    return slots, {tid: i + 1 for i, tid in enumerate(ordered_ids)}


def opening_slots(opening_a: int, opening_b: int, rest_ids: list[int]) -> tuple[list[int | None], dict[int, int]]:
    """rest_ids — остальные команды по силе (сильнейшая первой)."""
    n = len(rest_ids) + 2
    size = next_power_of_two(n)
    bye_count = size - n
    byes = list(rest_ids[:bye_count])
    active = list(rest_ids[bye_count:])
    real: list[tuple[int, int | None]] = [(opening_a, opening_b)]
    for i in range(len(active) // 2):
        real.append((active[i], active[-1 - i]))
    if len(active) % 2:
        byes.append(active[len(active) // 2])
    bye_pairs: list[tuple[int, int | None]] = [(t, None) for t in byes]
    pairs: list[tuple[int, int | None]] = []
    while real or bye_pairs:
        if real:
            pairs.append(real.pop(0))
        if bye_pairs:
            pairs.append(bye_pairs.pop(0))
    if len(pairs) != size // 2:
        raise ValueError("Не удалось собрать первый раунд сетки.")
    seeds = {opening_a: 1, opening_b: 2}
    for i, tid in enumerate(rest_ids, start=3):
        seeds[tid] = i
    return [x for p in pairs for x in p], seeds


def everyone_pairs(ids: list[int]) -> tuple[list[list[int]], list[int]]:
    """Все играют в первом раунде: сильный против слабого; при нечётном числе сильнейший получает BYE.
    ids — по силе (сильнейшая первой)."""
    rest = list(ids)
    bye = [rest.pop(0)] if len(rest) % 2 else []
    return [[rest[i], rest[-1 - i]] for i in range(len(rest) // 2)], bye


def layout_pairs(real: list[list[int]], bye: list[int]) -> list[list[int | None]]:
    """Пары первого раунда сетки-степени двойки, где BYE только у тех, кому не хватило соперника.

    Пустая пара (None, None) ставится рядом с реальной — победитель той пары проходит раунд без матча;
    команда с BYE стоит рядом с реальной парой и ждёт её победителя (двойного автопрохода нет).
    """
    n = 2 * len(real) + len(bye)
    size = next_power_of_two(n)
    if size == 2:
        return [list(real[0])] if real else [[bye[0], None]]
    groups = size // 4
    empty = size // 2 - len(real) - len(bye)
    singles = {int(k * groups / empty) for k in range(empty)} if empty else set()
    n_full = 2 * (groups - empty) - len(bye)
    full: list[list[int | None]] = [list(p) for p in real[:n_full]] + [[b, None] for b in bye]
    single: list[list[int | None]] = [list(p) for p in real[n_full:]]
    out: list[list[int | None]] = []
    for g in range(groups):
        out += [single.pop(0), [None, None]] if g in singles else [full.pop(0), full.pop(0)]
    return out


def pairs_slots(pairs: list[list[int | None]], seeds: dict[int, int] | None = None, *,
                allow_empty: bool = False) -> tuple[list[int | None], dict[int, int]]:
    size = len(pairs) * 2
    if size < 2 or size & (size - 1):
        raise ValueError("Число пар первого раунда должно давать степень двойки.")
    slots: list[int | None] = []
    seen: set[int] = set()
    for pair in pairs:
        if len(pair) != 2:
            raise ValueError("Каждая пара — две команды (вторая может быть BYE).")
        for tid in pair:
            if tid is not None:
                tid = int(tid)
                if tid in seen:
                    raise ValueError("Команда встречается в сетке дважды.")
                seen.add(tid)
            slots.append(tid)
        if pair[0] is None and pair[1] is None and not allow_empty:
            raise ValueError("Пустая пара в первом раунде.")
    if len(seen) < 2:
        raise ValueError("Для сетки нужны минимум 2 команды.")
    if seeds is None:
        seeds = {tid: i + 1 for i, tid in enumerate(t for t in slots if t is not None)}
    return slots, dict(seeds)
