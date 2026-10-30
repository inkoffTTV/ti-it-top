from services.bracket import (
    SEED_PAIRS,
    build_bracket,
    build_opening_match_seed_map,
    build_seed_map,
    next_power_of_two,
    seeded_slot_order,
)


def _teams(n=16):
    return [
        {"id": i, "name": f"Team {i}", "avg_mmr": 1000 + i * 100, "mmr_count": 5, "mmr_complete": True}
        for i in range(1, n + 1)
    ]


def test_hybrid_seed_manual_first_two_and_skill_sorted_rest():
    teams = _teams()
    seed_map = build_seed_map(teams, manual_seed_1=7, manual_seed_2=12)
    assert seed_map["1"] == 7
    assert seed_map["2"] == 12
    remaining = [16, 15, 14, 13, 11, 10, 9, 8, 6, 5, 4, 3, 2, 1]
    for seed, team_id in zip(range(3, 17), remaining):
        assert seed_map[str(seed)] == team_id


def test_opening_match_is_exactly_the_two_selected_teams():
    teams = _teams()
    seed_map = build_opening_match_seed_map(teams, opening_team_a=7, opening_team_b=12)
    assert seed_map["1"] == 7
    assert seed_map["2"] == 12
    assert seed_map["3"] == 16
    assert seed_map["16"] == 1

    team_map = {t["id"]: t for t in teams}
    bracket = build_bracket(seed_map, team_map)
    first = bracket["rounds"][0]["matches"]
    assert first[0]["team_a"]["id"] == 7
    assert first[0]["team_b"]["id"] == 12
    assert first[0]["label"] == "Матч 1 · Открывающий"


def test_opening_match_scales_and_keeps_all_teams_once():
    for n in (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 16, 17, 24, 31, 32, 33, 64, 100):
        teams = _teams(n)
        seed_map = build_opening_match_seed_map(teams, opening_team_a=1, opening_team_b=2)
        team_map = {t["id"]: t for t in teams}
        bracket = build_bracket(seed_map, team_map)
        size = next_power_of_two(n)
        first = bracket["rounds"][0]["matches"]
        assert bracket["team_count"] == n
        assert bracket["bracket_size"] == size
        assert len(first) == size // 2
        present = [m["team_a"] for m in first if m.get("team_a")] + [m["team_b"] for m in first if m.get("team_b")]
        assert len(present) == n
        ids = sorted(t["id"] for t in present)
        assert ids == list(range(1, n + 1))
        assert first[0]["team_a"]["id"] == 1
        assert first[0]["team_b"]["id"] == 2


def test_skill_sort_puts_missing_mmr_last_without_inventing_values():
    teams = [
        {"id": 1, "name": "known", "avg_mmr": 2500, "mmr_count": 5, "mmr_complete": True},
        {"id": 2, "name": "unknown", "avg_mmr": None, "mmr_count": 0, "mmr_complete": False},
        {"id": 3, "name": "partial", "avg_mmr": 2000, "mmr_count": 4, "mmr_complete": False},
        {"id": 4, "name": "high", "avg_mmr": 3000, "mmr_count": 5, "mmr_complete": True},
        {"id": 5, "name": "five", "avg_mmr": 1000, "mmr_count": 5, "mmr_complete": True},
        {"id": 6, "name": "six", "avg_mmr": 900, "mmr_count": 5, "mmr_complete": True},
        {"id": 7, "name": "seven", "avg_mmr": 800, "mmr_count": 5, "mmr_complete": True},
        {"id": 8, "name": "eight", "avg_mmr": 700, "mmr_count": 5, "mmr_complete": True},
        {"id": 9, "name": "nine", "avg_mmr": 600, "mmr_count": 5, "mmr_complete": True},
        {"id": 10, "name": "ten", "avg_mmr": 500, "mmr_count": 5, "mmr_complete": True},
        {"id": 11, "name": "eleven", "avg_mmr": 400, "mmr_count": 5, "mmr_complete": True},
        {"id": 12, "name": "twelve", "avg_mmr": 300, "mmr_count": 5, "mmr_complete": True},
        {"id": 13, "name": "thirteen", "avg_mmr": 200, "mmr_count": 5, "mmr_complete": True},
        {"id": 14, "name": "fourteen", "avg_mmr": 100, "mmr_count": 5, "mmr_complete": True},
        {"id": 15, "name": "fifteen", "avg_mmr": 50, "mmr_count": 5, "mmr_complete": True},
        {"id": 16, "name": "sixteen", "avg_mmr": None, "mmr_count": 3, "mmr_complete": False},
    ]
    seed_map = build_seed_map(teams, manual_seed_1=1, manual_seed_2=2)
    assert seed_map["3"] == 4
    assert seed_map["15"] == 15
    assert seed_map["16"] == 16


def test_bracket_has_expected_bye_count():
    for n in (3, 5, 6, 7, 9, 10, 13, 17, 24, 31):
        teams = _teams(n)
        seed_map = build_opening_match_seed_map(teams, 1, 2)
        team_map = {t["id"]: t for t in teams}
        bracket = build_bracket(seed_map, team_map)
        size = next_power_of_two(n)
        first_round = bracket["rounds"][0]["matches"]
        assert sum(1 for m in first_round if m["status"] == "BYE") == size - n


def test_seed_slot_order_matches_standard_16_bracket():
    assert seeded_slot_order(16) == [1, 16, 8, 9, 4, 13, 5, 12, 2, 15, 7, 10, 3, 14, 6, 11]
