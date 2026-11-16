import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.v1 import (
    audit,
    auth,
    cohorts,
    communication,
    dashboard,
    deliverables,
    engagement,
    exports,
    imports,
    interns,
    jobs,
    organizations,
    tasks,
    users,
)
from app.core import ratelimit
from app.core.config import get_settings
from app.core.errors import error_body, install_error_handlers
from app.core.logging import configure_logging, get_logger, request_id_var
from app.core.redis import close_redis, get_async_redis
from app.db.session import get_session

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
log = get_logger("http")

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_\-.]{1,64}$")


class RequestContextMiddleware:
    """Request id, structured access log, security headers and a global API rate limit.

    Pure ASGI (not BaseHTTPMiddleware) so streaming downloads are not buffered and the
    request-id context variable is visible inside endpoints and background work.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        incoming = headers.get(REQUEST_ID_HEADER.lower(), "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status_holder = {"status": 500}
        path: str = scope.get("path", "")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                extra = [
                    (REQUEST_ID_HEADER.encode(), request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                ]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        try:
            if path.startswith("/api/") and settings.rate_limit_enabled:
                client = scope.get("client")
                ip = client[0] if client else "unknown"
                limit = f"{settings.rate_limit_requests}/{settings.rate_limit_window_seconds} second"
                try:
                    await ratelimit.check("global", limit, ip)
                except HTTPException as exc:  # 429 from the limiter
                    retry = exc.headers or {}
                    response = JSONResponse(
                        status_code=429,
                        content=error_body("Too many requests. Please wait and try again.", "rate_limit", []),
                        headers=retry,
                    )
                    await response(scope, receive, send_wrapper)
                    return
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled_error", method=scope.get("method"), path=path)
            response = JSONResponse(
                status_code=500,
                content=error_body("Internal server error", "server_error", []),
            )
            await response(scope, receive, send_wrapper)
        finally:
            log.info(
                "request",
                method=scope.get("method"),
                path=path,
                status=status_holder["status"],
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
            )
            request_id_var.reset(token)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("startup", environment=settings.environment, job_backend=settings.effective_job_backend)
    yield
    await close_redis()


app = FastAPI(
    title=settings.app_name,
    version="1.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)

install_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[REQUEST_ID_HEADER, "Idempotent-Replayed", "Retry-After", "Content-Disposition"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
app.add_middleware(RequestContextMiddleware)

for module in (
    auth,
    organizations,
    cohorts,
    interns,
    tasks,
    engagement,
    communication,
    deliverables,
    exports,
    imports,
    jobs,
    users,
    audit,
    dashboard,
):
    app.include_router(module.router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up and serving. Deliberately does not touch
    dependencies, so a database blip does not make the orchestrator kill healthy
    containers. Use /ready for dependency checks."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Readiness: verifies PostgreSQL, Redis (when configured) and file storage."""
    checks: dict[str, Any] = {}
    healthy = True
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        checks["database"] = f"error: {exc.__class__.__name__}"
        healthy = False
    client = get_async_redis()
    if client is None:
        checks["redis"] = "disabled"
    else:
        try:
            await client.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {exc.__class__.__name__}"
            healthy = False
    try:
        from app.utils.storage import get_storage

        await get_storage().healthcheck()
        checks["storage"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["storage"] = f"error: {exc.__class__.__name__}"
        healthy = False
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "unavailable", "checks": checks},
    )
