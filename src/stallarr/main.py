"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import config as config_module
from .logging_setup import setup as setup_logging
from .service import StallarrService
from .web.metrics import render as render_metrics
from .web.routes import build_router, require_auth

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = config_module.load()
    setup_logging(cfg.log_level)
    log.info("stallarr starting (data dir: %s)", config_module.DATA_DIR)

    service = StallarrService(cfg)
    app.state.service = service
    await service.start()
    try:
        yield
    finally:
        log.info("stallarr shutting down")
        await service.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="stallarr",
        description="Torrent health monitor and auto-recovery",
        version="0.1.0",
        lifespan=lifespan,
        # Served by hand below instead. FastAPI mounts its built-in docs outside
        # the router, so `docs_url=` here would leave the schema and the "try it
        # out" console readable without the password that guards everything else.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.include_router(build_router())

    @app.get("/api/openapi.json", include_in_schema=False, dependencies=[Depends(require_auth)])
    async def openapi_schema() -> dict:
        return app.openapi()

    @app.get("/api/docs", include_in_schema=False, dependencies=[Depends(require_auth)])
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="stallarr API")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        service: StallarrService = app.state.service
        # Checked per request, not at startup: the setting is editable in the
        # UI and a reload must be able to close this endpoint again.
        if not service.cfg.web.metrics_enabled:
            raise HTTPException(status_code=404, detail="metrics are not enabled")
        body = await render_metrics(service)
        return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        service: StallarrService = app.state.service
        return {
            "status": "ok",
            "client_connected": bool(service.client_status.get("connected")),
            "last_scan_at": service.last_scan.finished_at,
        }

    return app


app = create_app()
