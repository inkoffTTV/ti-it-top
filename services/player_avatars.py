"""Аватары игроков: копия аватара Steam (или фото Telegram) на сервере.

Страницы Mini App отдают только локальный файл miniapp/data/player_avatars/user_<id>.jpg,
поэтому загрузка страниц не зависит ни от Steam CDN, ни от Telegram API.
Обновление — в фоне после проверки игрока и по кнопке администратора.
"""
from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO
from pathlib import Path

import aiohttp
from sqlalchemy import select

from database.engine import session_scope
from database.models import PlayerProfile, User

logger = logging.getLogger("PLAYER_AVATARS")

AVATAR_DIR = Path(__file__).resolve().parent.parent / "miniapp" / "data" / "player_avatars"
SIZE = 128
MAX_BYTES = 3 * 1024 * 1024
FRESH_SECONDS = 7 * 86400


def avatar_path(user_id: int) -> Path:
    return AVATAR_DIR / f"user_{int(user_id)}.jpg"


def _normalize(raw: bytes) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(BytesIO(raw)) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (23, 29, 41))
            bg.paste(rgba, mask=rgba.getchannel("A"))
            image = bg
        else:
            image = image.convert("RGB")
        image = ImageOps.fit(image, (SIZE, SIZE), Image.Resampling.LANCZOS)
        out = BytesIO()
        image.save(out, format="JPEG", quality=86, optimize=True)
        return out.getvalue()


def _write(user_id: int, data: bytes) -> None:
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    path = avatar_path(user_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def is_fresh(user_id: int) -> bool:
    try:
        return time.time() - avatar_path(user_id).stat().st_mtime < FRESH_SECONDS
    except OSError:
        return False


async def _download(url: str, proxy: str | None = None, timeout: float = 10.0) -> bytes:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url, proxy=proxy) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            # content.read(n) отдаёт лишь уже пришедший кусок — читаем до конца потока.
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise RuntimeError("слишком большой файл")
                chunks.append(chunk)
            return b"".join(chunks)


async def save_from_url(user_id: int, url: str, proxy: str | None = None) -> bool:
    if not url or not url.startswith("https://"):
        return False
    raw = await _download(url, proxy)
    data = await asyncio.to_thread(_normalize, raw)
    await asyncio.to_thread(_write, int(user_id), data)
    return True


async def _steam_avatar_urls(config, steam_ids: list[str]) -> dict[str, str]:
    """steamid64 -> URL аватара (один запрос на 100 игроков)."""
    key = (getattr(config, "steam_api_key", "") or "").strip()
    if not key or not steam_ids:
        return {}
    out: dict[str, str] = {}
    proxy = (config.http_proxy_url or "").strip() or None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        for i in range(0, len(steam_ids), 100):
            chunk = ",".join(steam_ids[i:i + 100])
            url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
            try:
                async with session.get(url, params={"key": key, "steamids": chunk}, proxy=proxy) as resp:
                    if resp.status != 200:
                        logger.warning("[PLAYER_AVATARS] GetPlayerSummaries HTTP %s", resp.status)
                        continue
                    body = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[PLAYER_AVATARS] GetPlayerSummaries failed: %s", type(exc).__name__)
                continue
            for p in ((body or {}).get("response") or {}).get("players") or []:
                if p.get("steamid") and (p.get("avatarfull") or p.get("avatarmedium")):
                    out[str(p["steamid"])] = p.get("avatarfull") or p.get("avatarmedium")
    return out


async def _telegram_photo(config, telegram_id: int) -> bytes | None:
    """Фото профиля Telegram (если пользователь его не скрыл)."""
    from miniapp_v2 import notify

    if notify.telegram_disabled():
        return None
    bot = notify._get_bot(config)
    photos = await asyncio.wait_for(bot.get_user_profile_photos(int(telegram_id), limit=1), timeout=12)
    if not photos or not photos.photos:
        return None
    sizes = photos.photos[0]
    best = min(sizes, key=lambda s: abs((s.width or 0) - 160))
    return await notify.download_file(config, best.file_id)


async def sync_all(config, *, force: bool = False, budget: float = 50.0, user_ids: list[int] | None = None) -> dict:
    """Обновляет аватары: Steam для привязанных, Telegram — для остальных."""
    started = time.monotonic()
    async with session_scope() as session:
        stmt = select(User.id, User.telegram_id, PlayerProfile.steam_id64).outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
        if user_ids:
            stmt = stmt.where(User.id.in_([int(u) for u in user_ids]))
        rows = (await session.execute(stmt.order_by(User.id))).all()
    todo = [(int(uid), tg, sid) for uid, tg, sid in rows if force or not is_fresh(int(uid))]
    steam_urls = await _steam_avatar_urls(config, sorted({sid for _, _, sid in todo if sid}))
    proxy = (config.http_proxy_url or "").strip() or None
    saved_steam = saved_tg = failed = skipped = 0
    sem = asyncio.Semaphore(4)

    async def one(uid: int, tg: int | None, sid: str | None) -> str:
        if time.monotonic() - started > budget:
            return "skipped"
        async with sem:
            try:
                if sid and steam_urls.get(sid):
                    await save_from_url(uid, steam_urls[sid], proxy)
                    return "steam"
                if tg:
                    raw = await _telegram_photo(config, tg)
                    if raw:
                        data = await asyncio.to_thread(_normalize, raw)
                        await asyncio.to_thread(_write, uid, data)
                        return "telegram"
                return "none"
            except Exception as exc:  # noqa: BLE001
                logger.info("[PLAYER_AVATARS] user %s: %s", uid, type(exc).__name__)
                return "failed"

    results = await asyncio.gather(*(one(*row) for row in todo))
    for r in results:
        if r == "steam":
            saved_steam += 1
        elif r == "telegram":
            saved_tg += 1
        elif r == "failed":
            failed += 1
        elif r == "skipped":
            skipped += 1
    return {"checked": len(todo), "steam": saved_steam, "telegram": saved_tg, "failed": failed,
            "skipped": skipped, "without_photo": len(todo) - saved_steam - saved_tg - failed - skipped}
