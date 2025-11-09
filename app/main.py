from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Awaitable, Callable
from uuid import uuid4

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.api import routes_auth, routes_clients, routes_qbo
from app.core.config import Settings, get_settings
from app.core import logging as logging_utils
from app.db.session import get_engine

RequestHandler = Callable[[Request], Awaitable[Response]]


async def enforce_api_key(
    api_key_header: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    if api_key_header is None or api_key_header != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    logging_utils.configure_logging()
    logger = logging.getLogger("app.lifespan")
    logger.info(
        "application_startup",
        extra={"environment": settings.environment},
    )
    try:
        yield
    finally:
        engine = get_engine()
        await engine.dispose()
        logger.info("application_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="QBO Gateway",
        version=get_settings().app_version,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next: RequestHandler):
        request_id = request.headers.get("X-Request-Id") or str(uuid4())
        request.state.request_id = request_id
        logging_utils.set_request_context(request_id=request_id)
        start = perf_counter()
        logger = logging.getLogger("app.request")
        request.state.response_status = None
        try:
            response = await call_next(request)
            request.state.response_status = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        except Exception:
            request.state.response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
            raise
        finally:
            duration_ms = (perf_counter() - start) * 1000
            logger.info(
                "request_completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": request.state.response_status,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            logging_utils.clear_request_context()

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        message: str
        details = None
        if isinstance(exc.detail, str):
            message = exc.detail
        else:
            message = "Request failed"
            details = exc.detail
        payload = {
            "code": exc.status_code,
            "message": message,
            "details": details,
            "correlation_id": request_id,
        }
        response = JSONResponse(status_code=exc.status_code, content=payload)
        if request_id:
            response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        payload = {
            "code": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "message": "Validation error",
            "details": exc.errors(),
            "correlation_id": request_id,
        }
        response = JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=payload)
        if request_id:
            response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger = logging.getLogger("app.errors")
        logger.exception(
            "unhandled_error",
            extra={"correlation_id": request_id},
        )
        payload = {
            "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
            "message": "Internal server error",
            "details": None,
            "correlation_id": request_id,
        }
        response = JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=payload)
        if request_id:
            response.headers["X-Request-Id"] = request_id
        return response

    settings = get_settings()
    if not settings.allow_docs_without_auth:
        app.dependencies.append(Depends(enforce_api_key))

    protected_router = APIRouter(dependencies=[Depends(enforce_api_key)])
    protected_router.include_router(routes_auth.router)
    protected_router.include_router(routes_clients.router)
    protected_router.include_router(routes_qbo.router)
    app.include_router(protected_router)
    app.include_router(routes_auth.public_router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    return app


app = create_app()


def run() -> None:
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        factory=False,
    )


if __name__ == "__main__":
    run()
