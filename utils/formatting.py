"""Форматирование текстов для пользователя. Никаких выдуманных значений."""
from __future__ import annotations

from datetime import datetime

UNAVAILABLE = "Недоступно"


def fmt(value, suffix: str = "") -> str:
    """None -> 'Недоступно'. 0 остаётся 0 (это валидное значение)."""
    if value is None:
        return UNAVAILABLE
    return f"{value}{suffix}"


def fmt_dt(dt: datetime | None) -> str:
    """Время проекта — московское (5.2): в БД UTC, пользователю — МСК, как в Mini App."""
    if dt is None:
        return UNAVAILABLE
    try:
        from services.tournament_engine.timeutil import fmt_local

        return fmt_local(dt, label=True) or UNAVAILABLE
    except (ValueError, OverflowError):  # pragma: no cover
        return UNAVAILABLE


STATUS_LABELS = {
    "UNVERIFIED": "⚪ Не проверен",
    "PENDING": "⏳ Ожидает проверки",
    "VERIFIED": "✅ Подтверждён",
    "REJECTED": "❌ Отклонён",
}

TOURNAMENT_STATUS_LABELS = {
    "DRAFT": "📝 Черновик",
    "REGISTRATION": "🟢 Открыта регистрация",
    "ACTIVE": "🔵 Идёт",
    "FINISHED": "🏁 Завершён",
    "CANCELLED": "⛔ Отменён",
}

APPLICATION_STATUS_LABELS = {
    "DRAFT": "📝 Черновик",
    "PENDING": "⏳ На рассмотрении",
    "APPROVED": "✅ Одобрена",
    "REJECTED": "❌ Отклонена",
    "CANCELLED": "⛔ Отменена",
}


def status_label(status) -> str:
    key = getattr(status, "value", str(status))
    return STATUS_LABELS.get(key, key)


def tournament_status_label(status) -> str:
    key = getattr(status, "value", str(status))
    return TOURNAMENT_STATUS_LABELS.get(key, key)


def application_status_label(status) -> str:
    key = getattr(status, "value", str(status))
    return APPLICATION_STATUS_LABELS.get(key, key)


def escape(text: str | None) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def user_mention(username: str | None, telegram_id: int) -> str:
    if username:
        return f"@{escape(username)}"
    return f"ID:{telegram_id}"


def _rank_label(approved_mmr: int | None) -> str:
    """Ранг определяется только по подтверждённому организатором MMR."""
    if approved_mmr is None:
        return "ПТС не утверждён"
    from utils.ranks import rank_label_from_mmr
    return rank_label_from_mmr(approved_mmr) or "ПТС не утверждён"


def profile_card(
    *,
    tg_username: str | None,
    tg_id: int,
    steam_nickname: str | None,
    steam_id64: str | None,
    account_id: int | None,
    verification_status,
    source: str | None,
    mmr: int | None,
    matches: int | None,
    winrate: float | None,
    rank_name: str | None = None,
    team_name: str | None = None,
    application_status=None,
    hours_total: int | None = None,
    exact_mmr: int | None = None,
    approved_mmr: int | None = None,
    mmr_verified: bool = False,
    preferred_positions: str | None = None,
) -> str:
    lines = [
        "👤 <b>Профиль игрока</b>",
        "",
        f"<b>Telegram:</b> {user_mention(tg_username, tg_id)}",
        f"<b>Steam ник:</b> {fmt(escape(steam_nickname)) if steam_nickname else UNAVAILABLE}",
        f"<b>Steam ID64:</b> {fmt(steam_id64)}",
        f"<b>Dota Account ID:</b> {fmt(account_id)}",
        f"<b>Статус проверки:</b> {status_label(verification_status)}",
        f"<b>Источник:</b> {fmt(source)}",
        f"<b>Точный MMR:</b> {fmt(exact_mmr)}",
        f"<b>MMR проверен:</b> {'Да' if mmr_verified else 'Нет'}",
        f"<b>Пул ролей:</b> {preferred_positions or UNAVAILABLE}",
        f"<b>Ранг:</b> {_rank_label(approved_mmr)}",
        f"<b>Матчи:</b> {fmt(matches)}",
        f"<b>Winrate:</b> {fmt(winrate, '%')}",
    ]
    if hours_total is not None:
        lines.append(f"<b>Часов в Dota 2:</b> {fmt(hours_total)}")
    if team_name is not None:
        lines.append(f"<b>Команда:</b> {escape(team_name) or 'Нет'}")
    if application_status is not None:
        lines.append(f"<b>Заявка:</b> {application_status_label(application_status)}")
    return "\n".join(lines)


def verification_request_card(
    *,
    tg_username: str | None,
    tg_id: int,
    steam_profile_url: str | None,
    steam_id64: str | None,
    account_id: int | None,
    nickname: str | None,
    source: str | None,
    mmr: int | None,
    matches: int | None,
    winrate: float | None,
    rank_name: str | None = None,
    leaderboard_rank: int | None = None,
    exact_mmr: int | None = None,
    mmr_verified: bool = False,
) -> str:
    return "\n".join(
        [
            "👤 <b>ПРОВЕРКА ИГРОКА</b>",
            "",
            f"<b>Telegram:</b> {user_mention(tg_username, tg_id)}",
            f"<b>Telegram ID:</b> <code>{tg_id}</code>",
            f"<b>Steam:</b> {escape(steam_profile_url) if steam_profile_url else UNAVAILABLE}",
            f"<b>Steam ID64:</b> {fmt(steam_id64)}",
            f"<b>Dota Account ID:</b> {fmt(account_id)}",
            f"<b>Ник:</b> {fmt(escape(nickname)) if nickname else UNAVAILABLE}",
            f"<b>Источник:</b> {fmt(source)}",
            f"<b>Точный MMR:</b> {fmt(exact_mmr)}",
            f"<b>MMR подтверждён:</b> {'Да' if mmr_verified else 'Нет'}",
            f"<b>Ранг:</b> ПТС не утверждён",
            f"<b>Место в лидерборде:</b> #{leaderboard_rank}" if leaderboard_rank is not None else f"<b>Место в лидерборде:</b> {UNAVAILABLE}",
            f"<b>Матчи:</b> {fmt(matches)}",
            f"<b>Winrate:</b> {fmt(winrate, '%')}",
            "",
            "<b>Статус:</b>",
            "⏳ Ожидает проверки",
        ]
    )


def format_roster(members: list[tuple]) -> str:
    """members: (имя, роль, статус[, позиция])."""
    rows = []
    for item in members:
        name, role, status = item[:3]
        position = item[3] if len(item) > 3 else None
        icon = "👑" if role == "CAPTAIN" else "🎮"
        pos_text = f" — pos {position}" if position in {1, 2, 3, 4, 5} else ""
        rows.append(f"{icon} {name}{pos_text} — {role_label(role)} — {status}")
    return "\n".join(rows) if rows else "Состав пуст."


def role_label(role) -> str:
    key = getattr(role, "value", str(role))
    return {"CAPTAIN": "капитан", "PLAYER": "игрок"}.get(key, key)
