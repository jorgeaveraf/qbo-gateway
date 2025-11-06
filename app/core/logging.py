from __future__ import annotations

import logging
import logging.config
from contextvars import ContextVar
from typing import Optional

from pythonjsonlogger import jsonlogger


request_id_ctx: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
client_id_ctx: ContextVar[Optional[str]] = ContextVar("client_id", default=None)
realm_id_ctx: ContextVar[Optional[str]] = ContextVar("realm_id", default=None)


class RequestContextFilter(logging.Filter):
    """Injects request scoped context variables into each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        record.client_id = client_id_ctx.get()
        record.realm_id = realm_id_ctx.get()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Configure JSON structured logging."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "request_context": {
                    "()": RequestContextFilter,
                }
            },
            "formatters": {
                "json": {
                    "()": jsonlogger.JsonFormatter,
                    "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "level": level,
                    "formatter": "json",
                    "filters": ["request_context"],
                }
            },
            "loggers": {
                "": {
                    "handlers": ["default"],
                    "level": level,
                }
            },
        }
    )


def set_request_context(
    request_id: Optional[str] = None,
    client_id: Optional[str] = None,
    realm_id: Optional[str] = None,
) -> None:
    if request_id is not None:
        request_id_ctx.set(request_id)
    if client_id is not None:
        client_id_ctx.set(client_id)
    if realm_id is not None:
        realm_id_ctx.set(realm_id)


def clear_request_context() -> None:
    request_id_ctx.set(None)
    client_id_ctx.set(None)
    realm_id_ctx.set(None)
