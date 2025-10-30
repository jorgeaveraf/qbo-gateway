from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException, status

from app.schemas.client import Environment


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
    if value not in ("sandbox", "prod"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid environment value",
        )
    return value  # type: ignore[return-value]
