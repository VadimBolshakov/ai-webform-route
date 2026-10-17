from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.account.routes import router as account_router
from app.auth.routes import router as auth_router
from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.notify.base import LogNotifier
from app.security.headers import OriginCheckMiddleware, SecurityHeadersMiddleware, is_private
from app.security.ratelimit import SlidingWindowLimiter

WEB_DIR = Path(__file__).parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.notifier = LogNotifier()
    app.state.login_limiter = SlidingWindowLimiter(20, 600)
    app.dependency_overrides[get_settings] = lambda: settings

    # Последний добавленный — внешний: заголовки попадают и на 403 от проверки Origin.
    app.add_middleware(OriginCheckMiddleware, base_url=settings.base_url)
    app.add_middleware(SecurityHeadersMiddleware)

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    app.state.templates = templates
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
