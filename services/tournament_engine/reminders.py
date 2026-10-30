"""Напоминания о матчах: за MATCH_REMINDER_MINUTES (по умолчанию 30) до начала — обеим командам.

Одно напоминание на назначенное время: факт отправки записывается в журнал матча
(action="reminder", new_json.scheduled_at). После переноса матча время меняется —
напоминание придёт заново. Схема БД не меняется.
Отключить: MATCH_REMINDERS_DISABLED=1.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from database.engine import session_scope
from database.models import Notification, Tournament, TournamentMatch, TournamentMatchLog
from services.tournament_engine import timeutil
from services.tournament_engine.service import _team_names, match_team_members

logger = logging.getLogger("MATCH_REMINDERS")

SendFn = Callable[..., Awaitable[None]]


def _minutes() -> int:
    try:
        return max(5, min(240, int(os.getenv("MATCH_REMINDER_MINUTES", "30"))))
    except ValueError:
        return 30


def disabled() -> bool:
    return os.getenv("MATCH_REMINDERS_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _esc(value: object) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def run_once(send: SendFn | None, *, now=None) -> int:
    """Отправляет напоминания о ближайших матчах. Возвращает число матчей с напоминанием."""
    now = now or timeutil.now_utc()
    window = timedelta(minutes=_minutes())
    outgoing: list[tuple[list[int], str, Any]] = []
    async with session_scope() as session:
        rows = (await session.execute(
            select(TournamentMatch).where(
                TournamentMatch.status.in_(("SCHEDULED", "POSTPONED")),
                TournamentMatch.scheduled_at.is_not(None),
                TournamentMatch.team1_id.is_not(None),
                TournamentMatch.team2_id.is_not(None),
            )
        )).scalars().all()
        due = []
        for m in rows:
            at = timeutil.as_utc(m.scheduled_at)
            if at and now - timedelta(minutes=5) <= at <= now + window:
                due.append((m, at))
        if not due:
            return 0
        sent_logs = (await session.execute(
            select(TournamentMatchLog.match_id, TournamentMatchLog.new_json).where(
                TournamentMatchLog.action == "reminder",
                TournamentMatchLog.match_id.in_([m.id for m, _ in due]),
            )
        )).all()
        already = set()
        for match_id, raw in sent_logs:
            try:
                already.add((int(match_id), (json.loads(raw or "{}") or {}).get("scheduled_at")))
            except ValueError:
                continue
        count = 0
        for m, at in due:
            key = at.isoformat()
            if (int(m.id), key) in already:
                continue
            names = await _team_names(session, {m.team1_id, m.team2_id})
            t = await session.get(Tournament, m.tournament_id)
            left = max(0, int((at - now).total_seconds() // 60))
            title = f"Скоро матч: {names.get(m.team1_id, '?')} — {names.get(m.team2_id, '?')}"
            when = timeutil.fmt_local(at, label=True)
            body = f"{t.name if t else ''} · {m.round_name} · BO{m.best_of} · {when}"
            members = await match_team_members(session, m)
            for user_id, _tg in members:
                session.add(Notification(user_id=user_id, kind="match_reminder", title=title[:160], body=body[:1000]))
            session.add(TournamentMatchLog(match_id=m.id, stage_id=m.stage_id, action="reminder",
                                           new_json=json.dumps({"scheduled_at": key, "recipients": len(members)})))
            from config import GLOBAL_CONFIG
            from services.tournament_extras import lobby_for

            lobby = lobby_for(m, GLOBAL_CONFIG.bot_token)
            text = (f"⏰ <b>{_esc(title)}</b>\nНачало через {left} мин — {_esc(when)}\n"
                    f"{_esc(t.name if t else '')} · {_esc(m.round_name)} · BO{m.best_of}\n\n"
                    f"<b>Лобби:</b> <code>{_esc(lobby['name'])}</code>\n<b>Пароль:</b> <code>{_esc(lobby['password'])}</code>\n"
                    f"Лобби создаёт капитан команды «{_esc(names.get(m.team1_id, ''))}».\n\n"
                    "Нажмите «Мы готовы», когда команда в сборе.")
            markup = None
            try:
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Мы готовы", callback_data=f"ready:{m.id}")]])
            except Exception:  # noqa: BLE001
                markup = None
            outgoing.append(([tg for _, tg in members], text, markup))
            # 5.9: болельщики команд — напоминание без лобби и без кнопки готовности.
            from services.audience import follower_telegram_ids

            fans = await follower_telegram_ids(session, [m.team1_id, m.team2_id], {uid for uid, _ in members})
            if fans:
                outgoing.append((fans, (f"<b>Скоро матч: {_esc(names.get(m.team1_id, '?'))} — {_esc(names.get(m.team2_id, '?'))}</b>\n"
                                        f"Начало через {left} мин — {_esc(when)} · {_esc(m.round_name)} · BO{m.best_of}\n"
                                        "Вы болеете за одну из команд. Трансляция — на главной Mini App."), None))
            count += 1
        await session.commit()
    if send is not None:
        for recipients, text, markup in outgoing:
            for tg in recipients:
                try:
                    await asyncio.wait_for(send(tg, text, markup), timeout=10)
                except Exception as exc:  # noqa: BLE001 — один недоступный игрок не мешает остальным
                    logger.info("[REMINDER] send to %s failed: %s", tg, exc)
    if count:
        logger.info("[REMINDER] reminders for %s match(es)", count)
    return count


async def _noshow_notify(send: SendFn) -> None:
    """Матч идёт 15 минут, а команда не нажала «Мы готовы» — админам кнопка «Присудить победу» (один раз)."""
    from config import GLOBAL_CONFIG
    from services.tournament_extras import noshow_alerts

    for alert in await noshow_alerts():
        markup = None
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"tl:{alert['match_id']}:{slot}")] for label, slot in alert["buttons"]])
        except Exception:  # noqa: BLE001
            markup = None
        for admin in sorted(GLOBAL_CONFIG.admin_ids):
            try:
                await asyncio.wait_for(send(admin, "<b>Возможная неявка</b>\n" + _esc(alert["text"]), markup), timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.info("[REMINDER] noshow notify %s failed: %s", admin, exc)


async def _open_chats_notify(send: SendFn) -> None:
    """За час до матча открывается закрытый чат матча (лобби внутри) — игрокам кнопка «Открыть чат»."""
    from config import GLOBAL_CONFIG
    from services.audience import open_due_chats

    for chat in await open_due_chats():
        markup = None
        if GLOBAL_CONFIG.miniapp_url:
            try:
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

                url = GLOBAL_CONFIG.miniapp_url + ("&" if "?" in GLOBAL_CONFIG.miniapp_url else "?") + f"match={chat['match_id']}"
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть чат матча", web_app=WebAppInfo(url=url))]])
            except Exception:  # noqa: BLE001
                markup = None
        text = (f"<b>Открыт чат матча {chat['match_no']}</b>: {_esc(chat['text'])}\nНачало — {_esc(chat['when'])}.\n"
                "Там название и пароль лобби. Чат видят только игроки двух команд и организаторы.")
        for tg in chat["telegram_ids"]:
            try:
                await asyncio.wait_for(send(tg, text, markup), timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.info("[REMINDER] chat notify %s failed: %s", tg, exc)


async def fan_start_check(now=None) -> list[tuple[list[int], str, int]]:
    """5.13: матч начался (время пришло или админ поставил «Идёт») — болельщикам команд один раз ссылка на Twitch.
    Возвращает [(telegram_ids, текст, match_id)]."""
    from services.audience import TWITCH_URL, follower_telegram_ids

    now = now or timeutil.now_utc()
    out = []
    async with session_scope() as session:
        rows = (await session.execute(select(TournamentMatch).where(
            TournamentMatch.status.in_(("SCHEDULED", "LIVE")), TournamentMatch.team1_id.is_not(None),
            TournamentMatch.team2_id.is_not(None)))).scalars().all()
        for m in rows:
            at = timeutil.as_utc(m.scheduled_at)
            started = timeutil.as_utc(m.started_at) or at
            due = (m.status == "LIVE" and (started is None or now - started <= timedelta(minutes=30))) or \
                  (at is not None and at <= now <= at + timedelta(minutes=20))
            if not due:
                continue
            if await session.scalar(select(TournamentMatchLog.id).where(TournamentMatchLog.match_id == m.id,
                                                                         TournamentMatchLog.action == "fan_start")):
                continue
            session.add(TournamentMatchLog(match_id=m.id, stage_id=m.stage_id, action="fan_start",
                                           new_json=json.dumps({"at": now.isoformat()})))
            members = await match_team_members(session, m)
            fans = await follower_telegram_ids(session, [m.team1_id, m.team2_id], {uid for uid, _ in members})
            if not fans:
                continue
            names = await _team_names(session, {m.team1_id, m.team2_id})
            out.append((fans, (f"<b>Начинается матч: {_esc(names.get(m.team1_id, '?'))} — {_esc(names.get(m.team2_id, '?'))}</b>\n"
                               f"{_esc(m.round_name)} · BO{m.best_of}. Смотрите трансляцию: {TWITCH_URL}"), int(m.id)))
        await session.commit()
    return out


async def _fan_start_notify(send: SendFn) -> None:
    from services.audience import TWITCH_URL

    markup = None
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Смотреть на Twitch", url=TWITCH_URL)]])
    except Exception:  # noqa: BLE001
        markup = None
    for fans, text, _ in await fan_start_check():
        for tg in fans:
            try:
                await asyncio.wait_for(send(tg, text, markup), timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.info("[REMINDER] fan start notify %s failed: %s", tg, exc)


_REG_NOTIFIED: set[tuple[int, str]] = set()


async def registration_close_check(now=None) -> list[str]:
    """За час до старта регистрация ещё открыта — текст админам (один раз на турнир и время старта)."""
    from database.models import TournamentStatus
    from services.tournament_engine.service import autopair_leftovers

    now = now or timeutil.now_utc()
    out = []
    async with session_scope() as session:
        rows = (await session.execute(select(Tournament).where(
            Tournament.status == TournamentStatus.REGISTRATION, Tournament.start_date.is_not(None)))).scalars().all()
        for t in rows:
            start = timeutil.as_utc(t.start_date)
            key = (int(t.id), start.isoformat())
            if key in _REG_NOTIFIED or not (start - timedelta(hours=1) <= now < start):
                continue
            _REG_NOTIFIED.add(key)
            try:
                plan = await autopair_leftovers(session, t, dry_run=True)
                preview = plan.get("text") or ""
            except Exception:  # noqa: BLE001
                preview = ""
            out.append(f"Турнир «{_esc(t.name)}» начнётся в {_esc(timeutil.fmt_local(start, label=True))}, а регистрация ещё открыта.\n"
                       "Закройте её (Mini App → Админ → Турниры → статус «Идёт»): команды без соперника сразу встанут в пары, "
                       "лишняя получит BYE, капитанам придут уведомления." + (f"\nСейчас: {_esc(preview)}" if preview else ""))
    return out


async def _registration_close_notify(send: SendFn) -> None:
    from config import GLOBAL_CONFIG

    for text in await registration_close_check():
        for admin in sorted(GLOBAL_CONFIG.admin_ids):
            try:
                await asyncio.wait_for(send(admin, text, None), timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.info("[REMINDER] registration notify %s failed: %s", admin, exc)


async def reminder_loop(send: SendFn, *, interval: float = 60.0) -> None:
    """Фоновый цикл в процессе бота. Ошибка одной итерации не останавливает цикл."""
    if disabled():
        logger.info("[REMINDER] disabled by MATCH_REMINDERS_DISABLED")
        return
    logger.info("[REMINDER] started: %s min before match", _minutes())
    while True:
        try:
            await run_once(send)
            await _noshow_notify(send)
            await _open_chats_notify(send)
            await _registration_close_notify(send)
            await _fan_start_notify(send)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[REMINDER] iteration failed")
        await asyncio.sleep(interval)
