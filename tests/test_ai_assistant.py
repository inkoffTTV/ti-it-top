from services.ai_assistant import TOOL_DEFINITIONS, SYSTEM_PROMPT, _extract_tool_calls, _normalise_history


def test_ai_tool_catalog_has_expected_read_only_tools():
    names = {item["function"]["name"] for item in TOOL_DEFINITIONS}
    assert "get_overview" in names
    assert "search_users" in names
    assert "get_team_mmr_ranking" in names
    assert "get_full_data_snapshot" in names
    assert "list_broadcasts" in names
    assert "list_rank_ranges" in names
    assert len(names) >= 18


def test_tool_call_arguments_are_normalised_from_json_string():
    calls = _extract_tool_calls({
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "search_users", "arguments": '{"query":"inkoff","limit":5}'},
        }]
    })
    assert calls == [{"id": "call_1", "name": "search_users", "arguments": {"query": "inkoff", "limit": 5}}]


def test_malformed_tool_arguments_do_not_crash():
    calls = _extract_tool_calls({
        "tool_calls": [{
            "id": "call_2",
            "function": {"name": "get_overview", "arguments": "not-json"},
        }]
    })
    assert calls[0]["arguments"] == {}


def test_history_is_limited_to_safe_roles_and_length():
    history = [{"role": "system", "content": "secret"}] + [
        {"role": "user", "content": f"q{i}"} for i in range(12)
    ]
    cleaned = _normalise_history(history)
    assert len(cleaned) == 8
    assert all(x["role"] == "user" for x in cleaned)


def test_system_prompt_forbids_side_effect_claims_and_secret_exposure():
    assert "read-only" in SYSTEM_PROMPT
    assert "API key" in SYSTEM_PROMPT
    assert "утверждай" in SYSTEM_PROMPT
