import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.account.routes import router as account_router
from app.admin.routes import router as admin_router
from app.auth.routes import router as auth_router
from app.chat.routes import router as chat_router
from app.chat.service import ErrorSeries
from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.jobs import make_scheduler
from app.llm.openrouter import OpenRouterClient
from app.notify.base import LogNotifier
from app.security.body_limit import BodyLimitMiddleware
from app.security.headers import OriginCheckMiddleware, SecurityHeadersMiddleware, is_private
from app.security.ratelimit import SlidingWindowLimiter
from app.telegram.bot import BotDeps, run_polling
from app.telegram.client import TelegramClient
from app.telegram.notifier import TelegramNotifier

WEB_DIR = Path(__file__).parent / "web"
SHUTDOWN_GRACE_S = 10


class _AppNotifier:
    """Delegates to whatever notifier is currently on app.state (tests swap it)."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def alert(self, text: str) -> None:
        await self._app.state.notifier.alert(text)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # Telegram API URLs contain the bot token: keep httpx request logging off.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    engine = make_engine(settings.database_url)
    http = httpx.AsyncClient()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = make_scheduler(app) if settings.enable_jobs else None
        if scheduler is not None:
            scheduler.start()
        stop_polling = asyncio.Event()
        if settings.enable_jobs and app.state.telegram is not None:
            deps = BotDeps(
                app.state.sessionmaker, app.state.notifier, settings, settings.telegram_owner_chat_id
            )
            app.state.telegram_polling = asyncio.create_task(
                run_polling(app.state.telegram, deps, stop_polling)
            )
        yield
        polling = app.state.telegram_polling
        if polling is not None:
            # Stop polling before the notifier drains and the shared httpx client closes.
            stop_polling.set()
            polling.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await polling
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if app.state.background_tasks:
            # Let messages/usage after streams and titles finish writing.
            await asyncio.wait(list(app.state.background_tasks), timeout=SHUTDOWN_GRACE_S)
        drain = getattr(app.state.notifier, "drain", None)
        if drain is not None:
            try:
                await asyncio.wait_for(drain(), 20)
            except asyncio.TimeoutError:
                logging.getLogger(__name__).warning("notifier drain timed out at shutdown")
        await http.aclose()
        await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.notifier = LogNotifier()
    app.state.telegram = None
    app.state.telegram_polling = None
    if settings.telegram_configured:
        app.state.telegram = TelegramClient(settings.telegram_bot_token, http)
        app.state.notifier = TelegramNotifier(app.state.telegram, settings.telegram_owner_chat_id)
    app.state.login_limiter = SlidingWindowLimiter(20, 600)
    app.state.chat_limiter = SlidingWindowLimiter(settings.chat_rate_per_min, 60)
    app.state.llm_errors = ErrorSeries(3, 600)
    app.state.background_tasks = set()
    app.state.llm = OpenRouterClient(settings, http, _AppNotifier(app))
    app.dependency_overrides[get_settings] = lambda: settings

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    app.state.templates = templates

    def not_found(scope) -> Response:
        return templates.TemplateResponse(Request(scope), "notfound.html", status_code=404)

    # The last one added is the outermost: headers also land on 403/404/413 from the checks below;
    # the body limit fires before any form parsing.
    app.add_middleware(OriginCheckMiddleware, base_url=settings.base_url)
    app.add_middleware(BodyLimitMiddleware, not_found=not_found)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_exc(request: Request, exc: RequestValidationError):
        if is_private(request.url.path):
            # No 422 echoing input on private paths.
            return templates.TemplateResponse(request, "notfound.html", status_code=404)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return templates.TemplateResponse(request, "notfound.html", status_code=404)
        if exc.status_code == 405:
            # Neither FastAPI's JSON on the facade nor the existence of private routes is revealed.
            return templates.TemplateResponse(request, "notfound.html", status_code=404)
        return await http_exception_handler(request, exc)

    app.include_router(auth_router)
    app.include_router(account_router)
    app.include_router(admin_router)
    app.include_router(chat_router)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def decoy(request: Request):
        return templates.TemplateResponse(
            request,
            "decoy.html",
            {"login_path_token": request.app.state.settings.login_path_token},
        )

    @app.get("/robots.txt", include_in_schema=False)
    async def robots():
        return FileResponse(WEB_DIR / "static" / "robots.txt", media_type="text/plain")

    return app
