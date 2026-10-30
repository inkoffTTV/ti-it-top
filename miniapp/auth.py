from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


@dataclass(slots=True)
class TelegramUser:
    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> TelegramUser:
    if not init_data:
        raise ValueError("Telegram authorization data is missing")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise ValueError("Telegram authorization hash is missing")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise ValueError("Telegram authorization signature is invalid")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise ValueError("Telegram authorization timestamp is invalid") from exc
    if auth_date <= 0 or time.time() - auth_date > max_age:
        raise ValueError("Telegram authorization data is expired")
    try:
        raw_user = json.loads(pairs.get("user", "{}"))
        user = TelegramUser(
            id=int(raw_user["id"]),
            username=raw_user.get("username"),
            first_name=raw_user.get("first_name"),
            last_name=raw_user.get("last_name"),
            language_code=raw_user.get("language_code"),
        )
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Telegram user data is invalid") from exc
    return user
