from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import Config
from miniapp.api import MiniAppContext, build_router

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"}

logger = logging.getLogger("MINIAPP")


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="TI:IT TOP Mini App", docs_url=None, redoc_url=None)
    app.state.config = config
    app.include_router(build_router(MiniAppContext(config)))

    # Mini App 5.0 (miniapp_v2/) работает параллельно со старой Mini App.
    # Если новый пакет не импортируется, старая Mini App продолжает работать.
    v2_mounted = False
    try:
        from miniapp_v2.mount import mount_v2

        mount_v2(app, config)
        v2_mounted = True
    except Exception:  # noqa: BLE001
        logger.exception("[MINIAPP] Mini App 5.0 was not mounted; legacy UI stays active")
    app.state.v2_mounted = v2_mounted

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    def legacy_index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers=NO_STORE)

    @app.get("/", include_in_schema=False)
    async def index():
        # runtime/MINIAPP_UI: «v2» (по умолчанию) или «legacy» — переключение без рестарта.
        if v2_mounted:
            from miniapp_v2.common import active_ui
            from miniapp_v2.mount import index_response

            if active_ui() == "v2":
                return index_response()
        return legacy_index()

    @app.get("/legacy", include_in_schema=False)
    @app.get("/legacy/", include_in_schema=False)
    async def legacy():
        return legacy_index()

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        path = STATIC / "favicon.svg"
        return FileResponse(path, media_type="image/svg+xml")

    return app


async def run_server(config: Config) -> None:
    app = create_app(config)
    uv_config = uvicorn.Config(
        app,
        host=config.miniapp_host,
        port=config.miniapp_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)
    try:
        await server.serve()
    finally:
        try:
            from miniapp_v2 import notify

            await notify.close()
        except Exception:  # noqa: BLE001
            pass
