from __future__ import annotations

import uuid
from typing import Optional, cast

from datetime import date, datetime

from fastapi import HTTPException, status

from app.schemas.client import Environment


_ENVIRONMENT_ALIASES = {
    "sandbox": "sandbox",
    "prod": "prod",
    "production": "prod",
}


def parse_uuid(value: str, field_name: str = "identifier") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} format",
        ) from exc


def resolve_environment(
    value: Optional[str],
    default_env: Environment,
) -> Environment:
    if value is None:
        return default_env
    return _normalize_environment(value)


def resolve_environment_optional(value: Optional[str]) -> Optional[Environment]:
    if value is None:
        return None
    return _normalize_environment(value)


def _normalize_environment(value: str) -> Environment:
    normalized = value.lower()
    resolved = _ENVIRONMENT_ALIASES.get(normalized)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid environment value",
        )
    return cast(Environment, resolved)


def normalize_start_position(value: Optional[int]) -> int:
    if value is None or value < 1:
        return 1
    return value


def normalize_max_results(value: Optional[int], *, default: int = 100, limit: int = 1000) -> int:
    if value is None:
        return default
    if value < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="maxresults must be >= 1",
        )
    if value > limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"maxresults cannot exceed {limit}",
        )
    return value


def normalize_date(value: Optional[date]) -> Optional[date]:
    return value


def normalize_datetime(value: Optional[datetime]) -> Optional[datetime]:
    return value
