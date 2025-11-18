from __future__ import annotations

import json
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Tuple

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotencyKeys
from app.utils.hashing import sha256_hex


async def register_idempotency_key(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    key: str,
    request_payload: Any,
    resource_type: str,
    fingerprint: str | None = None,
) -> Tuple[IdempotencyKeys, bool]:
    serialized_payload = (
        fingerprint
        if fingerprint is not None
        else json.dumps(
            request_payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )
    hashed_payload = sha256_hex(serialized_payload)
    result = await session.execute(
        select(IdempotencyKeys).where(IdempotencyKeys.key == key)
    )
    existing = result.scalar_one_or_none()
    if existing:
        if existing.request_hash != hashed_payload:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key conflict",
            )
        if existing.response_body is None and existing.client_id != client_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key is currently in use",
            )
        return existing, True

    record = IdempotencyKeys(
        client_id=None,
        key=key,
        resource_type=resource_type,
        request_hash=hashed_payload,
        response_body=None,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        result = await session.execute(
            select(IdempotencyKeys).where(IdempotencyKeys.key == key)
        )
        existing = result.scalar_one()
        if existing.request_hash != hashed_payload:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key conflict",
            )
        if existing.response_body is None and existing.client_id != client_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key is currently in use",
            )
        return existing, True
    return record, False


async def store_idempotent_response(
    session: AsyncSession,
    record: IdempotencyKeys,
    response_body: Any,
) -> None:
    record.response_body = response_body
    await session.flush()


def _normalize_amount(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def build_fingerprint(*parts: Any) -> str:
    normalized: list[str] = []
    for part in parts:
        if isinstance(part, Decimal):
            normalized.append(_normalize_amount(part))
        elif part is None:
            normalized.append("")
        else:
            normalized.append(str(part))
    return "|".join(normalized)
