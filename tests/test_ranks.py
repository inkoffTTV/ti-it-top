from utils.ranks import (
    MAX_MMR,
    rank_info_from_mmr,
    rank_info_from_tier,
    rank_label_from_mmr,
    rank_label_from_tier,
    rank_name_to_tier,
    rank_range_rows,
)


def test_rank_tier_names_and_zero_star_compatibility():
    assert rank_label_from_tier(10) == "Рекрут"
    assert rank_label_from_tier(11) == "Рекрут 1"
    assert rank_label_from_tier(25) == "Страж 5"
    assert rank_label_from_tier(45) == "Герой 5"
    assert rank_label_from_tier(75) == "Божество 5"
    assert rank_label_from_tier(80) == "Титан"


def test_rank_mmr_boundaries():
    cases = {
        0: "Рекрут 1",
        153: "Рекрут 1",
        154: "Рекрут 2",
        769: "Рекрут 5",
        770: "Страж 1",
        1540: "Рыцарь 1",
        2310: "Герой 1",
        3080: "Легенда 1",
        3850: "Властелин 1",
        4620: "Божество 1",
        4820: "Божество 2",
        5020: "Божество 3",
        5220: "Божество 4",
        5420: "Божество 5",
        5620: "Титан",
        9500: "Титан",
    }
    for mmr, expected in cases.items():
        assert rank_label_from_mmr(mmr) == expected


def test_rank_info_ranges_are_contiguous():
    rows = rank_range_rows()
    assert len(rows) == 36
    assert rows[0] == (11, "Рекрут", 1, 0, 153)
    assert rows[30] == (71, "Божество", 1, 4620, 4819)
    assert rows[34] == (75, "Божество", 5, 5420, 5619)
    assert rows[-1] == (80, "Титан", 0, 5620, MAX_MMR)
    for a, b in zip(rows, rows[1:]):
        assert b[3] == a[4] + 1


def test_rank_name_parsing():
    assert rank_name_to_tier("Archon 5") == 45
    assert rank_name_to_tier("Архонт 5") == 45
    assert rank_name_to_tier("Божественный 4") == 74
    assert rank_name_to_tier("Immortal") == 80
    assert rank_info_from_tier(80).star == 0
    assert rank_info_from_mmr(5620).rank_tier == 80
