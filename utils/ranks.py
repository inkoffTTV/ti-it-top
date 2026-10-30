"""Единый справочник рангов Dota 2 и преобразования MMR.

Важно: Valve не публикует постоянную формулу выдачи медали по MMR. Поэтому
MMR -> ранг ниже является ОЦЕНОЧНЫМ справочником, а сохранённый OpenDota
rank_tier считается фактической медалью аккаунта, когда он доступен.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

MAX_MMR = 9500  # после июльского 2026 squish верхняя граница MMR = 9500

MEDAL_NAMES_RU = {
    1: "Рекрут",
    2: "Страж",
    3: "Рыцарь",
    4: "Герой",
    5: "Легенда",
    6: "Властелин",
    7: "Божество",
    8: "Титан",
}

MEDAL_NAMES_EN = {
    "herald": 1,
    "guardian": 2,
    "crusader": 3,
    "archon": 4,
    "legend": 5,
    "ancient": 6,
    "divine": 7,
    "immortal": 8,
}
MEDAL_NAMES_ALIASES = {
    "рекрут": 1, "геральд": 1,
    "страж": 2, "хранитель": 2,
    "рыцарь": 3, "крестоносец": 3,
    "герой": 4, "архонт": 4,
    "легенда": 5,
    "властелин": 6, "древний": 6,
    "божество": 7, "божественный": 7,
    "титан": 8, "бессмертный": 8,
}

# Порог следующей звезды. Для Herald–Ancient шаг ~154 MMR,
# для Divine — 200 MMR. Границы трактуются как [min, next_min).
_THRESHOLDS = [
    0, 154, 308, 462, 616, 770,
    924, 1078, 1232, 1386, 1540,
    1694, 1848, 2002, 2156, 2310,
    2464, 2618, 2772, 2926, 3080,
    3234, 3388, 3542, 3696, 3850,
    4004, 4158, 4312, 4466, 4620,
    4820, 5020, 5220, 5420, 5620,
]

@dataclass(frozen=True, slots=True)
class RankInfo:
    rank_tier: int
    medal: int
    name: str
    star: int
    mmr_min: int
    mmr_max: int
    estimated: bool = False


def _range_for_tier(tier: int) -> tuple[int, int]:
    if tier == 80:
        return 5620, MAX_MMR
    if not 10 <= tier <= 75 or tier % 10 not in range(0, 6):
        raise ValueError(f"invalid rank_tier: {tier}")
    medal = tier // 10
    star = tier % 10
    # tier 10/20/... are zero-star compatibility states. Use the medal's first threshold.
    if star == 0:
        lo = _THRESHOLDS[(medal - 1) * 5]
        hi = (_THRESHOLDS[medal * 5] - 1) if medal <= 7 else 5619
        return lo, hi
    idx = (medal - 1) * 5 + (star - 1)
    lo = _THRESHOLDS[idx]
    hi = (_THRESHOLDS[idx + 1] - 1) if idx + 1 < len(_THRESHOLDS) else MAX_MMR
    return lo, hi


def rank_info_from_tier(value: int | str | None) -> RankInfo | None:
    try:
        tier = int(value)
    except (TypeError, ValueError):
        return None
    if tier == 80:
        lo, hi = _range_for_tier(tier)
        return RankInfo(tier, 8, MEDAL_NAMES_RU[8], 0, lo, hi, False)
    if tier < 10:
        return None
    medal, star = divmod(tier, 10)
    if medal not in MEDAL_NAMES_RU or star not in range(0, 6):
        return None
    lo, hi = _range_for_tier(tier)
    return RankInfo(tier, medal, MEDAL_NAMES_RU[medal], star, lo, hi, False)


def rank_label_from_tier(value: int | str | None) -> str | None:
    info = rank_info_from_tier(value)
    if not info:
        return None
    return info.name if info.medal == 8 or info.star == 0 else f"{info.name} {info.star}"


def rank_info_from_mmr(mmr: int | str | None) -> RankInfo | None:
    try:
        value = int(mmr)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > MAX_MMR:
        return None
    # Immortal starts at 5620. For lower ranks find the highest threshold <= value.
    if value >= 5620:
        return RankInfo(80, 8, MEDAL_NAMES_RU[8], 0, 5620, MAX_MMR, True)
    # thresholds 0..5420 are star starts, index 0..34
    idx = 0
    for i, threshold in enumerate(_THRESHOLDS):
        if threshold <= value:
            idx = i
        else:
            break
    medal = idx // 5 + 1
    star = idx % 5 + 1
    lo = _THRESHOLDS[idx]
    hi = _THRESHOLDS[idx + 1] - 1
    tier = medal * 10 + star
    return RankInfo(tier, medal, MEDAL_NAMES_RU[medal], star, lo, hi, True)


def rank_label_from_mmr(mmr: int | str | None) -> str | None:
    info = rank_info_from_mmr(mmr)
    return rank_label_from_tier(info.rank_tier) if info else None


def rank_name_to_tier(value: str | None) -> int | None:
    if not value:
        return None
    raw = str(value).strip().lower().replace("ё", "е").replace("_", " ")
    raw = re.sub(r"\s+", " ", raw)
    m = re.search(r"(\d+)\s*$", raw)
    star = int(m.group(1)) if m else 0
    base = re.sub(r"\s*\d+\s*$", "", raw).strip()
    medal = MEDAL_NAMES_ALIASES.get(base)
    if medal is None:
        medal = MEDAL_NAMES_EN.get(base)
    if medal is None:
        return None
    if medal == 8:
        return 80
    if star == 0:
        # Zero-star tier values are accepted for compatibility with OpenDota.
        return medal * 10
    if 1 <= star <= 5:
        return medal * 10 + star
    return None


def rank_range_rows() -> list[tuple[int, str, int, int, int]]:
    rows: list[tuple[int, str, int, int, int]] = []
    for medal in range(1, 8):
        for star in range(1, 6):
            info = rank_info_from_tier(medal * 10 + star)
            assert info
            rows.append((info.rank_tier, info.name, info.star, info.mmr_min, info.mmr_max))
    rows.append((80, MEDAL_NAMES_RU[8], 0, 5620, MAX_MMR))
    return rows
