"""Тесты парсинга Steam-идентификаторов и валидаторов."""
from __future__ import annotations

import pytest

from utils.validation import (
    SteamInputKind,
    account_id_to_id64,
    id64_to_account_id,
    parse_date,
    parse_steam_input,
    validate_support_message,
    validate_team_name,
)


def test_parse_steam_id64():
    r = parse_steam_input("76561199161987264")
    assert r.ok and r.kind == SteamInputKind.ID64
    assert r.steam_id64 == "76561199161987264"
    assert r.account_id == 1201721536


def test_parse_profile_url():
    r = parse_steam_input("https://steamcommunity.com/profiles/76561199161987264")
    assert r.ok and r.kind == SteamInputKind.PROFILE_URL
    assert r.account_id == 1201721536


def test_parse_dota_account_id():
    r = parse_steam_input("1201721536")
    assert r.ok and r.kind == SteamInputKind.ACCOUNT_ID
    assert r.account_id == 1201721536
    assert r.steam_id64 == "76561199161987264"


def test_parse_vanity_url():
    r = parse_steam_input("https://steamcommunity.com/id/myvanity")
    assert r.ok and r.kind == SteamInputKind.VANITY_URL
    assert r.vanity == "myvanity"


def test_invalid_inputs():
    for bad in ["", "abc", "http://example.com/x", "7656119800573788", "99999999999999999999"]:
        r = parse_steam_input(bad)
        assert not r.ok, bad
        assert r.error


def test_too_big_number_rejected_as_account_id():
    # 11-значное число — не ID64 (не 17 цифр) и не аккаунт (<10 цифр)
    r = parse_steam_input("12345678901")
    assert not r.ok


def test_id_conversions_roundtrip():
    assert id64_to_account_id(76561199161987264) == 1201721536
    assert account_id_to_id64(1201721536) == 76561199161987264


def test_team_name_validation():
    ok, name, err = validate_team_name("   Team   Alpha  ")
    assert ok and name == "Team Alpha"  # нормализация пробелов
    assert not validate_team_name("   ")[0]
    assert not validate_team_name("x" * 100)[0]


def test_support_message_validation():
    ok, text, err = validate_support_message("  help me  ")
    assert ok and text == "help me"
    assert not validate_support_message("")[0]
    assert not validate_support_message("a" * 5000)[0]


def test_parse_date():
    assert parse_date("2026-10-01") is not None
    assert parse_date("01.10.2026") is not None
    assert parse_date("not-a-date") is None
