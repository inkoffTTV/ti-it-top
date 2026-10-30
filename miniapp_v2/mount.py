"""Подключение Mini App 5.0 к существующему FastAPI-приложению (miniapp/app.py)."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import Config
from miniapp_v2.common import STATIC_DIR, configure

logger = logging.getLogger("MINIAPP_V2")

NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"}


def index_response() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers=NO_STORE)


def mount_v2(app: FastAPI, config: Config) -> None:
    from miniapp_v2.routes_admin import build_admin_router
    from miniapp_v2.routes_matches import build_matches_router
    from miniapp_v2.routes_records import build_records_files_router, build_records_router
    from miniapp_v2.routes_user import build_user_router

    # Сначала всё, что может упасть, и только потом регистрация в app:
    # подключение либо целиком, либо никак (старая Mini App остаётся рабочей).
    if not (STATIC_DIR / "index.html").is_file():
        raise RuntimeError(f"Mini App 5.0 static files not found in {STATIC_DIR}")
    static_app = StaticFiles(directory=STATIC_DIR)
    user_router = build_user_router()
    admin_router = build_admin_router()
    matches_router = build_matches_router()
    records_router = build_records_router()
    # 5.15: записи трансляций. В production файлы отдаёт nginx (location /rec/), это запасной путь.
    records_files = build_records_files_router()

    configure(config)
    app.include_router(user_router)
    app.include_router(admin_router)
    app.include_router(matches_router)
    app.include_router(records_router)
    app.include_router(records_files)
    # Статика v2 версионируется через ?v=..., поэтому может кэшироваться браузером.
    app.mount("/v2/static", static_app, name="miniapp-v2-static")

    @app.get("/v2", include_in_schema=False)
    @app.get("/v2/", include_in_schema=False)
    async def v2_index():
        return index_response()

    logger.info("[MINIAPP_V2] Mounted /api/v2 and /v2/ (static: %s)", STATIC_DIR)
