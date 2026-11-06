"""Плашка «MVP по выбору игроков» на главном экране мини-приложения и в приветствии /start.

Админ задаёт её вручную (имя и подпись) — в мини-приложении (Админ-панель → MVP на главной) или командой
/mvp в боте; хранится в runtime/mvp_banner.json и переживает перезапуск. Если вручную ничего не задано,
показывается победитель голосования за MVP последнего завершённого турнира (services/tournament_extras.py).
"""
from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("MVP_BANNER")

PATH = Path(__file__).resolve().parent.parent / "runtime" / "mvp_banner.json"
MAX_NAME = 40
MAX_CAPTION = 120


def manual() -> dict[str, str] | None:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = str(data.get("name") or "").strip() if isinstance(data, dict) else ""
    if not name:
        return None
    return {"name": name[:MAX_NAME], "caption": str(data.get("caption") or "").strip()[:MAX_CAPTION]}


def set_manual(name: str, caption: str = "") -> dict[str, str]:
    name, caption = (name or "").strip(), (caption or "").strip()
    if not name:
        raise ValueError("Укажите имя MVP.")
    if len(name) > MAX_NAME:
        raise ValueError(f"Имя длиннее {MAX_NAME} символов.")
    if len(caption) > MAX_CAPTION:
        raise ValueError(f"Подпись длиннее {MAX_CAPTION} символов.")
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"name": name, "caption": caption}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PATH)
    logger.info("[MVP_BANNER] set manually: %s", name)
    return {"name": name, "caption": caption}


def clear() -> None:
    try:
        PATH.unlink()
    except FileNotFoundError:
        pass


async def current() -> dict[str, Any] | None:
    """{name, caption, source: manual|vote, tournament?, votes?} или None, если показывать некого."""
    m = manual()
    if m:
        return {**m, "source": "manual"}
    try:
        from services import tournament_extras as extras

        for t in await extras.hall_of_fame():
            if t.get("mvp"):
                votes = int(t["mvp"].get("votes") or 0)
                return {"name": t["mvp"]["name"], "caption": f"MVP турнира «{t['name']}» · голосов: {votes}",
                        "source": "vote", "tournament": t["name"], "votes": votes}
    except Exception:  # плашка не должна ронять главный экран
        logger.exception("[MVP_BANNER] auto MVP failed")
    return None


def welcome_line(b: dict[str, Any] | None) -> str:
    """Строка для приветствия /start (HTML); '' — показывать нечего."""
    if not b:
        return ""
    line = f"🏅 <b>MVP матчей по выбору игроков:</b> {html.escape(b['name'])}"
    if b.get("caption"):
        line += f" — {html.escape(b['caption'])}"
    return line
