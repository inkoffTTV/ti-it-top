"""Диагностика внешних API (Steam / Dota client / Dotabuff / STRATZ).

Позволяет админу прямо из Telegram понять, ПОЧЕМУ статистика не тянется:
не задан токен, Cloudflare-фильтр по TLS/IP, сеть не проходит и т.п.
Ничего не отклоняет и не меняет — только проверяет доступность.
"""
from __future__ import annotations

import html
import json as _json
import logging

from services.http_client import HAS_CURL_CFFI


def esc(text: object) -> str:
    """Экранирует текст для HTML-разметки Telegram.

    Сообщения исключений могут содержать '<urlopen error ...>' — без
    экранирования Telegram падает с 'Unsupported start tag'.
    """
    return html.escape(str(text), quote=False)

logger = logging.getLogger("DIAGNOSTICS")

# Тестовый аккаунт Valve (публичный, всегда существует).
TEST_ACCOUNT_ID = 80329641          # account id
TEST_STEAM_ID64 = "76561198040592369"


async def run_diagnostics(services) -> str:
    """Возвращает текстовый отчёт для админа."""
    lines = ["🔎 <b>Диагностика внешних API</b>", ""]
    http = services.dotabuff.http
    cfg = services.config

    proxy = await http.ensure_local_proxy()
    detected = list(getattr(http, "_local_proxies", None) or [])
    lines.append(
        f"▪ curl_cffi (браузерный TLS): <b>{'установлен ✅' if HAS_CURL_CFFI else 'НЕ установлен ⚠️'}</b>"
    )
    lines.append(f"▪ Прокси (используется): <code>{proxy or 'не обнаружен (прямое подключение)'}</code>")
    if detected and proxy in detected:
        lines.append("  <i>Прокси найден авто-сканом локальных VPN-портов.</i>")
    if detected:
        lines.append("▪ Локальные VPN-порты вайрусятся: " + ", ".join(f"<code>{x}</code>" for x in detected))
    if not HAS_CURL_CFFI:
        lines.append(
            "  <i>pip install curl-cffi — сильно повышает шанс пройти Cloudflare.</i>"
        )
    if not proxy:
        lines.append(
            "  <i>Если браузер с VPN открывает сайты, а бот нет — укажи HTTP_PROXY_URL "
            "в .env (например http://127.0.0.1:7890) или включи системный прокси.</i>"
        )
    lines.append(f"▪ TLS-отпечаток: <code>{http.impersonate}</code>")
    lines.append("")

    # 0. Сетевой выход (наш IP) — Cloudflare банит целые диапазоны датацентров,
    # и никакой код это не исправит; нужен другой IP (домашний/VPN/резидентный).
    try:
        r_ip = await http.get("https://api.ipify.org?format=json")
        if r_ip.status == 200:
            ip = (_json.loads(r_ip.body) or {}).get("ip", "?")
            lines.append(f"▪ Внешний IP бота: <code>{ip}</code>")
            lines.append(
                "  <i>Если этот IP ≠ IP твоего VPN из браузера — бот идёт в обход VPN. "
                "Включи системный прокси или укажи HTTP_PROXY_URL.</i>"
            )
            lines.append("")
    except Exception as e:  # noqa: BLE001
        logger.debug("[DIAG] ip check failed: %s", e)

    # 1. Steam Web API
    if cfg.steam_api_key:
        try:
            st = await services.steam.get_player_by_id64(TEST_STEAM_ID64)
            lines.append(f"✅ Steam Web API: ok (ник={esc(st.nickname)})")
        except Exception as e:  # noqa: BLE001
            lines.append(f"❌ Steam Web API: {esc(e)}")
    else:
        lines.append("⚠️ Steam Web API: STEAM_API_KEY не задан (часы/ник недоступны)")

    # 2. Dota client (GetOwnedGames)
    if cfg.steam_api_key:
        try:
            dc = await services.dota_client.get_player_by_id64(TEST_STEAM_ID64)
            hours = dc.raw.get("dota_hours_total")
            lines.append(f"✅ Valve GetOwnedGames: ok (hours={esc(hours)})")
        except Exception as e:  # noqa: BLE001
            lines.append(f"❌ Valve GetOwnedGames: {esc(e)}")
    else:
        lines.append("⚠️ Valve GetOwnedGames: нужен STEAM_API_KEY")

    # 3. Dotabuff
    try:
        db = await services.dotabuff.get_player(TEST_ACCOUNT_ID)
        lines.append(
            f"✅ Dotabuff: ok (mmr={esc(db.mmr)}, matches={esc(db.matches)}, winrate={esc(db.winrate)})"
        )
    except LookupError:
        lines.append("✅ Dotabuff: сеть проходит (тестовый игрок не найден — это ок)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"⚠️ Dotabuff: {esc(e)}")

    # 4. STRATZ
    if not cfg.stratz_api_token:
        lines.append("⚠️ STRATZ: STRATZ_API_TOKEN не задан → запросы не идут вообще")
    else:
        try:
            st = await services.stratz.get_player(TEST_ACCOUNT_ID)
            lines.append(
                f"✅ STRATZ: ok (mmr={esc(st.mmr)}, matches={esc(st.matches)}, winrate={esc(st.winrate)})"
            )
        except LookupError:
            lines.append("✅ STRATZ: GraphQL проходит (токен валиден)")
        except Exception as e:  # noqa: BLE001
            lines.append(f"⚠️ STRATZ: {esc(e)}")

    lines.append("")
    lines.append("<i>Недоступность источников НЕ ломает регистрацию: игрок уходит в PENDING на ручную проверку.</i>")
    return "\n".join(lines)
