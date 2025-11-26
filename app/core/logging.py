from __future__ import annotations

import logging
import logging.config
from contextvars import ContextVar
from typing import Any, Optional

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


def _redact_value(value: Any) -> str:
    if value is None:
        return ""
    return "***redacted***"


def sanitize_payload(payload: Any) -> Any:
    """Remove obvious secrets from a payload while keeping business fields."""

    sensitive_keys = {
        "authorization",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "password",
    }

    def _sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, val in value.items():
                key_lower = str(key).lower()
                if any(token in key_lower for token in sensitive_keys):
                    sanitized[key] = _redact_value(val)
                else:
                    sanitized[key] = _sanitize(val)
            return sanitized
        if isinstance(value, list):
            return [_sanitize(item) for item in value]
        return value

    return _sanitize(payload)


def _base_txn_log_extra(
    *,
    event: str,
    client_id: Optional[str],
    realm_id: Optional[str],
    environment: Optional[str],
    txn_type: Optional[str],
    txn_id: Optional[str],
    doc_number: Optional[str],
    idempotency_key: Optional[str],
    payload: Any = None,
) -> dict[str, Any]:
    return {
        "event": event,
        "request_id": request_id_ctx.get(),
        "client_id": client_id,
        "realm_id": realm_id,
        "environment": environment,
        "txn_type": txn_type,
        "txn_id": txn_id,
        "doc_number": doc_number,
        "idempotency_key": idempotency_key,
        "payload": payload,
    }


def log_qbo_txn_started(
    *,
    client_id: Optional[str],
    realm_id: Optional[str],
    environment: Optional[str],
    txn_type: Optional[str],
    txn_id: Optional[str],
    doc_number: Optional[str],
    idempotency_key: Optional[str],
    payload: Any,
) -> None:
    logger = logging.getLogger("app.qbo.txn")
    logger.info(
        "qbo_txn_attempt_started",
        extra=_base_txn_log_extra(
            event="qbo_txn_attempt_started",
            client_id=client_id,
            realm_id=realm_id,
            environment=environment,
            txn_type=txn_type,
            txn_id=txn_id,
            doc_number=doc_number,
            idempotency_key=idempotency_key,
            payload=sanitize_payload(payload),
        ),
    )


def log_qbo_txn_finished(
    *,
    client_id: Optional[str],
    realm_id: Optional[str],
    environment: Optional[str],
    txn_type: Optional[str],
    txn_id: Optional[str],
    doc_number: Optional[str],
    idempotency_key: Optional[str],
    gateway_status_code: Optional[int],
    qbo_status_code: Optional[int],
    latency_ms: Optional[float],
    result: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    qbo_error_details: Optional[str] = None,
    idempotent_reuse: bool = False,
) -> None:
    logger = logging.getLogger("app.qbo.txn")
    logger.info(
        "qbo_txn_attempt_finished",
        extra={
            **_base_txn_log_extra(
                event="qbo_txn_attempt_finished",
                client_id=client_id,
                realm_id=realm_id,
                environment=environment,
                txn_type=txn_type,
                txn_id=txn_id,
                doc_number=doc_number,
                idempotency_key=idempotency_key,
            ),
            "gateway_status_code": gateway_status_code,
            "qbo_status_code": qbo_status_code,
            "latency_ms": None if latency_ms is None else round(latency_ms, 2),
            "result": result,
            "error_code": error_code,
            "error_message": error_message,
            "qbo_error_details": qbo_error_details,
            "idempotent_reuse": idempotent_reuse,
        },
    )


def log_unhandled_exception(event: str, *, path: str, method: str, client_id: Optional[str] = None) -> None:
    logger = logging.getLogger("app.errors")
    logger.exception(
        event,
        extra={
            "path": path,
            "method": method,
            "request_id": request_id_ctx.get(),
            "client_id": client_id,
        },
    )
