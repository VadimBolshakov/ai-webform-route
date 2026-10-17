import asyncio
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
    engine = make_engine(settings.database_url)
    http = httpx.AsyncClient()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = make_scheduler(app) if settings.enable_jobs else None
        if scheduler is not None:
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if app.state.background_tasks:
            # Дать дописать сообщения/usage после стримов и заголовки.
            await asyncio.wait(list(app.state.background_tasks), timeout=SHUTDOWN_GRACE_S)
        await http.aclose()
        await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.notifier = LogNotifier()
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

    # Последний добавленный — внешний: заголовки попадают и на 403/404/413 от проверок ниже;
    # лимит тела срабатывает до любого разбора формы.
    app.add_middleware(OriginCheckMiddleware, base_url=settings.base_url)
    app.add_middleware(BodyLimitMiddleware, not_found=not_found)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_exc(request: Request, exc: RequestValidationError):
        if is_private(request.url.path):
            # Никакого 422 с эхом ввода на приватных путях.
            return templates.TemplateResponse(request, "notfound.html", status_code=404)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return templates.TemplateResponse(request, "notfound.html", status_code=404)
        if exc.status_code == 405 and is_private(request.url.path):
            # Не раскрываем существование приватных маршрутов.
            return templates.TemplateResponse(request, "notfound.html", status_code=404)
        return await http_exception_handler(request, exc)

    app.include_router(auth_router)
    app.include_router(account_router)
    app.include_router(admin_router)
    app.include_router(chat_router)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
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
