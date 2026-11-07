"""Плашка «MVP по выбору игроков» на главном экране мини-приложения и в приветствии /start.

Админ задаёт её вручную (имя и подпись) — в мини-приложении (Админ-панель → MVP на главной) или командой
/mvp в боте; хранится в runtime/mvp_banner.json и переживает перезапуск. Если вручную ничего не задано,
показывается победитель голосования за MVP последнего завершённого турнира (services/tournament_extras.py).
Фон карточки — горизонтальная картинка runtime/mvp_banner.jpg (обрезается по краям под размер карточки):
загрузка в мини-приложении или фото с подписью «/mvp фон» в боте.
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
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_MAX_SIDE = 1600


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


def image_path() -> Path:
    return PATH.with_suffix(".jpg")


def image_url() -> str | None:
    """Адрес фона с версией по времени изменения (кэш браузера обновится после замены)."""
    p = image_path()
    try:
        return f"/api/v2/mvp-banner/image?v={int(p.stat().st_mtime)}"
    except OSError:
        return None


def prepare_image(raw: bytes) -> bytes:
    """Проверяет картинку и сохраняет в JPEG, длинная сторона до IMAGE_MAX_SIDE; ValueError — не картинка."""
    from io import BytesIO

    from PIL import Image, ImageOps

    if not raw:
        raise ValueError("Файл пустой.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Картинка больше 10 МБ.")
    try:
        with Image.open(BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
            out = BytesIO()
            image.save(out, format="JPEG", quality=85, optimize=True, progressive=True)
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Не удалось прочитать картинку. Нужен JPG, PNG или WebP.") from exc


def set_image(raw: bytes) -> str:
    data = prepare_image(raw)
    p = image_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".jpg.tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
    logger.info("[MVP_BANNER] background image set (%d bytes)", len(data))
    return image_url() or ""


def clear_image() -> None:
    try:
        image_path().unlink()
    except FileNotFoundError:
        pass


async def current() -> dict[str, Any] | None:
    """{name, caption, source: manual|vote, tournament?, votes?} или None, если показывать некого."""
    b = await _current()
    if b is not None:
        b["image"] = image_url()
    return b


async def _current() -> dict[str, Any] | None:
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
