"""5.13: картинка с итогами турнира (призёры, MVP, приз зрителей) и её рассылка после финала."""
from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import session_scope
from database.models import ApplicationStatus, TeamMember, Tournament, TournamentApplication, User

logger = logging.getLogger("RESULTS_CARD")

FONT_CANDIDATES = {
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
           "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf"],
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf"],
}


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    for path in FONT_CANDIDATES[bold]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _uname(u: User | None) -> str:
    if u is None:
        return "—"
    return "@" + u.username if u.username else (" ".join(x for x in (u.first_name, u.last_name) if x) or f"id{u.telegram_id}")


async def results_data(session: AsyncSession, tournament: Tournament) -> dict[str, Any] | None:
    from services import tournament_extras as extras
    from services.tournament_engine import service as engine

    stages = await engine.get_stages(session, tournament.id)
    if not stages:
        return None
    places = await extras.final_places(session, stages[-1])
    if not places:
        return None
    names = await engine._team_names(session, {t for _, t in places})
    mvp = await extras.mvp_results(session, tournament.id)
    aud = await extras.audience_results(session, tournament.id)
    mvp_user = await session.get(User, mvp[0]["user_id"]) if mvp else None
    aud_user = await session.get(User, aud[0]["user_id"]) if aud else None
    return {"name": tournament.name, "places": [(p, names.get(t, "?")) for p, t in places],
            "mvp": (_uname(mvp_user), mvp[0]["votes"]) if mvp else None,
            "audience": (_uname(aud_user), aud[0]["votes"]) if aud else None}


def render(data: dict[str, Any]) -> bytes:
    from PIL import Image, ImageDraw

    w, h = 1200, 675
    img = Image.new("RGB", (w, h), (11, 14, 20))
    d = ImageDraw.Draw(img)
    for y in range(h):  # мягкий вертикальный градиент
        k = y / h
        d.line([(0, y), (w, y)], fill=(int(11 + 16 * (1 - k)), int(14 + 20 * (1 - k)), int(20 + 32 * (1 - k))))
    d.rectangle([0, 0, w, 8], fill=(255, 122, 69))

    def text(xy, s, size, color=(238, 242, 248), bold=False, anchor="la", max_w=None):
        f = _font(size, bold)
        s = str(s)
        while max_w and d.textlength(s, font=f) > max_w and len(s) > 4:
            s = s[:-2].rstrip() + "…" if not s.endswith("…") else s[:-3].rstrip() + "…"
        d.text(xy, s, font=f, fill=color, anchor=anchor)

    text((w // 2, 52), "ИТОГИ ТУРНИРА", 24, (142, 153, 173), True, "ma")
    text((w // 2, 88), data["name"], 50, bold=True, anchor="ma", max_w=1100)
    colors = {1: (244, 195, 90), 2: (200, 208, 220), 3: (205, 127, 50)}
    slots = {2: (90, 250, 330, 300), 1: (435, 205, 330, 345), 3: (780, 270, 330, 280)}
    by_place: dict[int, list[str]] = {}
    for p, n in data["places"]:
        by_place.setdefault(int(p), []).append(n)
    for place, (x, y, bw, bh) in slots.items():
        teams = by_place.get(place)
        if not teams:
            continue
        c = colors[place]
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=22, fill=(21, 27, 38), outline=c, width=4)
        text((x + bw // 2, y + 24), f"{place} МЕСТО", 26, c, True, "ma")
        for i, n in enumerate(teams[:2]):
            text((x + bw // 2, y + 82 + i * 52), n, 34 if len(teams) == 1 else 28, bold=True, anchor="ma", max_w=bw - 30)
    y = 575
    if data.get("mvp"):
        text((90, y), f"MVP: {data['mvp'][0]}  ·  голосов: {data['mvp'][1]}", 28, (244, 195, 90), True, max_w=560)
    if data.get("audience"):
        text((w - 90, y), f"Приз зрителей: {data['audience'][0]}", 28, (90, 167, 239), True, "ra", max_w=520)
    text((w // 2, h - 40), "TI:IT TOP · twitch.tv/inkoff_ttv", 20, (142, 153, 173), anchor="ma")
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def card_for(tournament_id: int) -> tuple[bytes, dict[str, Any]] | None:
    async with session_scope() as session:
        t = await session.get(Tournament, int(tournament_id))
        if t is None:
            return None
        data = await results_data(session, t)
    if data is None:
        return None
    return await asyncio.to_thread(render, data), data


async def recipients(tournament_id: int) -> list[int]:
    """Админы + игроки одобренных команд + болельщики этих команд."""
    from config import GLOBAL_CONFIG
    from database.models import TeamFollow

    async with session_scope() as session:
        team_ids = (await session.execute(select(TournamentApplication.team_id).where(
            TournamentApplication.tournament_id == int(tournament_id), TournamentApplication.status == ApplicationStatus.APPROVED))).scalars().all()
        players = (await session.execute(select(User.telegram_id).join(TeamMember, TeamMember.user_id == User.id)
                                         .where(TeamMember.team_id.in_(team_ids)))).scalars().all() if team_ids else []
        fans = (await session.execute(select(User.telegram_id).join(TeamFollow, TeamFollow.user_id == User.id)
                                      .where(TeamFollow.team_id.in_(team_ids)))).scalars().all() if team_ids else []
    out: list[int] = []
    for tg in [*sorted(GLOBAL_CONFIG.admin_ids), *players, *fans]:
        if tg and int(tg) not in out:
            out.append(int(tg))
    return out


async def publish(config, tournament_id: int) -> int:
    """Рассылает картинку итогов. Возвращает число получателей (0 — итогов ещё нет)."""
    from miniapp_v2 import notify

    card = await card_for(tournament_id)
    if card is None:
        return 0
    png, data = card
    champion = next((n for p, n in data["places"] if p == 1), "?")
    caption = f"Итоги «{data['name']}». Победитель — {champion}! Спасибо всем командам и зрителям."
    targets = await recipients(tournament_id)

    async def job() -> None:
        for tg in targets:
            try:
                await notify.send_photo(config, tg, png, caption)
            except Exception as exc:  # noqa: BLE001
                logger.info("[RESULTS] send to %s failed: %s", tg, exc)
            await asyncio.sleep(0.06)

    notify.fire(job, label=f"results:{tournament_id}")
    return len(targets)
